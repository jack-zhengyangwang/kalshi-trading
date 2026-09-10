"""Soccer knowledge derived from settled markets — point-in-time, by construction.

The firm keeps two databases, deliberately:

    data/market_history.db   FINANCIAL. Kalshi candles, prices, outcomes.
    data/soccer.db           KNOWLEDGE. Elo, head-to-head, home/away rates,
                             recent form, news.

They are separate because they answer different questions, decay at different
rates, and carry different risks. Mixing them in one file invites a join that
quietly reads a fact from after the bar it is pricing.

WHY THIS FETCHER MATTERS: everything here is rebuilt by walking finished matches
FORWARD, so a fact dated day D reflects only matches that had finished by day D.
No cheat is required for any of it — Elo, head-to-head, home and away win rates,
and recent form are all reconstructible from results we already hold. That
matters because a backtest using these is genuine promotion evidence, whereas
one using look-forward facts is only ranking evidence.

The exception is news and anything else nobody recorded at the time. Those come
from `espn.py`, accrue forward only, and are the sole reason the look-forward
flag exists at all.

A team the data has never seen simply has no facts. That is correct: a PM
entering a league it knows nothing about should know nothing about it, and
should accumulate a record as matches settle — which is exactly what happens
here, one day at a time.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from wc.backtest import data as market_data
from wc.firm import facts as F

ELO_BASE = 1500.0
ELO_K = 24.0
ELO_HOME = 60.0
FORM_WINDOW = 5


def merged_matches(con):
    """[(close_ts, match_id, home, away, outcome)] from the MERGED table.

    Preferred over `settled_matches`, which sees only what Kalshi traded in the
    last two months. The merged table chains three feeds into continuous
    coverage back to 2023, so Elo, form and head-to-head start warm instead of
    cold — which is the difference between KnowledgeView refusing to price a
    fixture and having an opinion about it.
    """
    rows = con.execute("""
        SELECT match_id, league, home, away, home_goals, away_goals, known_at
        FROM matches
        WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL
        ORDER BY known_at ASC""").fetchall()
    out = []
    for r in rows:
        hg, ag = r["home_goals"], r["away_goals"]
        outcome = "home" if hg > ag else ("away" if ag > hg else "draw")
        out.append((int(r["known_at"]), r["match_id"], r["home"], r["away"],
                    outcome))
    return out


def settled_matches(con):
    """[(close_ts, event, home, away, outcome)] in chronological order.

    `outcome` is 'home' | 'away' | 'draw'. A match contributes a fact only once
    it has RESOLVED, and the fact is dated to its close time — the moment the
    result became knowable.
    """
    rows = con.execute("""
        SELECT event_ticker, ticker, home, away, sub_title, result, close_time
        FROM markets
        WHERE result IN ('yes','no') AND home IS NOT NULL AND away IS NOT NULL
          AND close_time IS NOT NULL AND event_ticker IS NOT NULL
        ORDER BY close_time ASC""").fetchall()

    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event_ticker"]].append(r)

    out = []
    for event, legs in by_event.items():
        winner = next((l for l in legs if l["result"] == "yes"), None)
        if winner is None:
            continue                       # every leg lost: not a resolved game
        home, away = winner["home"], winner["away"]
        sub = winner["sub_title"] or ""
        if _same(sub, home):
            outcome = "home"
        elif _same(sub, away):
            outcome = "away"
        else:
            outcome = "draw"
        out.append((int(winner["close_time"]), event, home, away, outcome))
    out.sort(key=lambda x: x[0])
    return out


def _same(a, b):
    na = "".join(c for c in (a or "").lower() if c.isalnum())
    nb = "".join(c for c in (b or "").lower() if c.isalnum())
    return bool(na) and bool(nb) and (na in nb or nb in na)


def _pair(a, b):
    return "|".join(sorted([a, b]))


def build(source_con, facts_con, k=ELO_K, home_bonus=ELO_HOME, use_merged=True):
    """Walk every settled match forward, emitting facts as they become true.

    The loop reads state BEFORE updating it, so a fact stamped at match M
    reflects matches strictly before M. Reversing those two lines would leak
    every result into its own prediction — the single easiest way to build a
    backtest that looks brilliant and means nothing.
    """
    matches = []
    if use_merged:
        try:
            matches = merged_matches(source_con)
        except Exception:
            matches = []                   # no merged table yet
    if not matches:
        matches = settled_matches(source_con)
    if not matches:
        return {"matches": 0, "facts": 0}

    elo = defaultdict(lambda: ELO_BASE)
    home_played = defaultdict(int); home_won = defaultdict(int)
    away_played = defaultdict(int); away_won = defaultdict(int)
    form = defaultdict(list)                    # team -> recent points
    h2h = defaultdict(lambda: [0, 0])           # pair -> [a_wins, played]
    seen = defaultdict(int)

    rows = []
    for ts, _event, home, away, outcome in matches:
        # ── emit what was already true BEFORE this match ─────────────────────
        for team in (home, away):
            if seen[team]:
                rows.append({"entity": team, "feature": "elo",
                             "value": elo[team], "as_of": ts})
                rows.append({"entity": team, "feature": "matches_seen",
                             "value": seen[team], "as_of": ts})
                if form[team]:
                    rows.append({"entity": team, "feature": "form_ppg",
                                 "value": sum(form[team]) / len(form[team]),
                                 "as_of": ts})
        if home_played[home]:
            rows.append({"entity": home, "feature": "home_win_rate",
                         "value": home_won[home] / home_played[home], "as_of": ts})
        if away_played[away]:
            rows.append({"entity": away, "feature": "away_win_rate",
                         "value": away_won[away] / away_played[away], "as_of": ts})
        pair = _pair(home, away)
        if h2h[pair][1]:
            rows.append({"entity": pair, "feature": "h2h_games",
                         "value": h2h[pair][1], "as_of": ts})
            rows.append({"entity": pair, "feature": "h2h_first_win_rate",
                         "value": h2h[pair][0] / h2h[pair][1], "as_of": ts})

        # ── then learn from it ───────────────────────────────────────────────
        exp_home = 1.0 / (1.0 + 10 ** (-((elo[home] + home_bonus) - elo[away]) / 400.0))
        score = 1.0 if outcome == "home" else (0.5 if outcome == "draw" else 0.0)
        delta = k * (score - exp_home)
        elo[home] += delta
        elo[away] -= delta

        home_played[home] += 1
        away_played[away] += 1
        if outcome == "home":
            home_won[home] += 1
        elif outcome == "away":
            away_won[away] += 1

        pts_h = 3 if outcome == "home" else (1 if outcome == "draw" else 0)
        pts_a = 3 if outcome == "away" else (1 if outcome == "draw" else 0)
        for team, pts in ((home, pts_h), (away, pts_a)):
            form[team].append(pts)
            if len(form[team]) > FORM_WINDOW:
                form[team].pop(0)
            seen[team] += 1

        first = sorted([home, away])[0]
        won_first = (outcome == "home" and home == first) or \
                    (outcome == "away" and away == first)
        h2h[pair][0] += 1 if won_first else 0
        h2h[pair][1] += 1

    written = F.put_many(facts_con, rows, source="derived")
    return {"matches": len(matches), "facts": written,
            "teams": len(seen), "pairs": len(h2h)}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rebuild point-in-time soccer knowledge from settled markets.")
    ap.add_argument("--source-db", default=None,
                    help="where matches come from (default: the merged table "
                         "in the soccer DB, falling back to Kalshi settlements)")
    ap.add_argument("--market-db", default=market_data.DB_PATH)
    ap.add_argument("--facts-db", default=F.DB_PATH)
    ap.add_argument("--k", type=float, default=ELO_K)
    args = ap.parse_args(argv)

    fcon = F.connect(args.facts_db)
    # The merged table lives in the soccer DB alongside the facts it feeds.
    src = args.source_db or args.facts_db
    scon = market_data.connect(src) if args.source_db else fcon
    from wc.firm import matches as M
    M.connect(args.facts_db).close()       # ensure the schema exists
    rep = build(scon, fcon, k=args.k)
    print(f"\n  {rep['matches']:,} settled matches -> {rep['facts']:,} facts "
          f"({rep.get('teams', 0):,} teams, {rep.get('pairs', 0):,} pairings)")
    print(f"  -> {args.facts_db}\n")
    for r in F.coverage(fcon)[:10]:
        print(f"    {r['feature']:<22} n={r['n']:<6} entities={r['entities']:<5} "
              f"{r['source']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
