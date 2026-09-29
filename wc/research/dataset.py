"""v2 Phase 0: the dataset a prediction model learns from and is judged on.

    python -m wc.research.dataset

One model, two tables (docs/NEXT_CHAPTER_v2.md, "Phase 0 in detail"):

    ds_matches  every finished match in soccer.db (2021 →), features known
                before kickoff − 1h, target home/draw/away, bookmaker odds
                with the margin removed as the benchmark
    ds_kalshi   every Kalshi winner leg with an ESPN kickoff, joined to its
                soccer.db match, at 24h / 6h / 1h before kickoff: the same
                features as of that moment, the Kalshi bid/ask, and whether
                the leg won

WHY THE FEATURES ARE REBUILT HERE. soccer.db's `facts` table stamps each value
at the END of the match it was computed at (kickoff + 2h), holding the state
from BEFORE that match. A strict "known before the cutoff" read of it is
therefore always one match stale. Rebuilding in a single forward pass —
where a match updates the state only once its result is known — is
point-in-time by construction for any cutoff.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
from collections import defaultdict, deque

from wc import paths
from wc.research.batch1 import ESPN_BACKED, entry_quote
from wc.research.kickoff import names_match

ELO_BASE = 1500.0
ELO_K = 24.0
ELO_HOME = 60.0
FORM_N = 5
LEAGUE_DAYS = 365
JOIN_WINDOW_S = 36 * 3600
ENTRY_HOURS = (24, 6, 1)
DAY = 86400

SOCCER_DB = os.path.join(paths.ROOT, "data", "soccer.db")
HISTORY_DB = os.path.join(paths.ROOT, "data", "market_history.db")
OUT_DB = os.path.join(paths.ROOT, "data", "research", "v2_dataset.db")
RESULTS = os.path.join(paths.ROOT, "docs", "findings", "results")


def _ts(y, m, d):
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp())


# Splits by kickoff. Bookmaker odds end 2026-05-13, so the Kalshi period
# (from June) has no bookmaker benchmark and is judged against Kalshi instead.
SPLITS = [("train", _ts(2025, 1, 1)), ("tune", _ts(2026, 1, 1)),
          ("test", _ts(2026, 6, 1)), ("kalshi_period", 10 ** 12)]


def split_of(kickoff):
    for name, end in SPLITS:
        if kickoff < end:
            return name
    return SPLITS[-1][0]


# ── small pieces ──────────────────────────────────────────────────────────────

def outcome(hg, ag):
    return "home" if hg > ag else "away" if ag > hg else "draw"


def devig(oh, od, oa):
    """Decimal odds -> probabilities with the bookmaker margin removed
    proportionally. None if any price is missing or not a price (≤ 1)."""
    if any(o is None or o <= 1.0 for o in (oh, od, oa)):
        return None
    inv = [1 / oh, 1 / od, 1 / oa]
    s = sum(inv)
    return tuple(x / s for x in inv)


def leg_brier(pairs):
    """Mean (p − y)² over legs: the score every probability is judged by."""
    pairs = list(pairs)
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


# ── point-in-time features ────────────────────────────────────────────────────

class _State:
    def __init__(self):
        self.elo = defaultdict(lambda: ELO_BASE)
        self.seen = defaultdict(int)
        self.hist = defaultdict(lambda: deque(maxlen=FORM_N))   # (pts, gf, ga, shots)
        self.last_ko = {}
        self.home_rec = defaultdict(lambda: [0, 0])              # [wins, games] at home
        self.away_rec = defaultdict(lambda: [0, 0])              # [wins, games] away
        self.h2h = defaultdict(list)                             # pair -> winners (None = draw)
        self.league = defaultdict(deque)                         # league -> (kickoff, outcome)

    def update(self, m):
        h, a, res = m["home_key"], m["away_key"], outcome(m["hg"], m["ag"])
        eh, ea = self.elo[h], self.elo[a]
        exp_h = 1 / (1 + 10 ** ((ea - (eh + ELO_HOME)) / 400))
        s = {"home": 1.0, "draw": 0.5, "away": 0.0}[res]
        self.elo[h] = eh + ELO_K * (s - exp_h)
        self.elo[a] = ea - ELO_K * (s - exp_h)
        pts_h = {"home": 3, "draw": 1, "away": 0}[res]
        pts_a = {"home": 0, "draw": 1, "away": 3}[res]
        self.hist[h].append((pts_h, m["hg"], m["ag"], m.get("shots_h")))
        self.hist[a].append((pts_a, m["ag"], m["hg"], m.get("shots_a")))
        for t in (h, a):
            self.seen[t] += 1
            self.last_ko[t] = m["kickoff"]
        self.home_rec[h][0] += res == "home"
        self.home_rec[h][1] += 1
        self.away_rec[a][0] += res == "away"
        self.away_rec[a][1] += 1
        self.h2h[tuple(sorted((h, a)))].append(h if res == "home" else a if res == "away" else None)
        self.league[m["league"]].append((m["kickoff"], res))

    def query(self, q):
        h, a, cut = q["home_key"], q["away_key"], q["cutoff"]
        eh, ea = self.elo[h], self.elo[a]

        def avg(team, i):
            vals = [x[i] for x in self.hist[team] if x[i] is not None]
            return sum(vals) / len(vals) if vals else None

        def rest(team):
            return (cut - self.last_ko[team]) / DAY if team in self.last_ko else None

        def rate(rec):
            return (rec[0] / rec[1]) if rec[1] else None

        games = self.h2h.get(tuple(sorted((h, a))), [])
        lg = self.league[q["league"]]
        while lg and lg[0][0] < cut - LEAGUE_DAYS * DAY:
            lg.popleft()
        n = len(lg)
        return {
            "elo_h": eh, "elo_a": ea, "elo_diff": eh + ELO_HOME - ea,
            "p_elo_home": 1 / (1 + 10 ** ((ea - (eh + ELO_HOME)) / 400)),
            "seen_h": self.seen[h], "seen_a": self.seen[a],
            "form_h": avg(h, 0), "form_a": avg(a, 0),
            "gf_h": avg(h, 1), "ga_h": avg(h, 2), "gf_a": avg(a, 1), "ga_a": avg(a, 2),
            "sh_h": avg(h, 3), "sh_a": avg(a, 3),
            "rest_h": rest(h), "rest_a": rest(a),
            "home_wr_h": rate(self.home_rec[h]) if h in self.home_rec else None,
            "home_n_h": self.home_rec[h][1] if h in self.home_rec else 0,
            "away_wr_a": rate(self.away_rec[a]) if a in self.away_rec else None,
            "away_n_a": self.away_rec[a][1] if a in self.away_rec else 0,
            "h2h_n": len(games),
            "h2h_home_wr": (sum(1 for w in games if w == h) / len(games)) if games else None,
            "lg_n": n,
            "lg_draw": (sum(1 for _, r in lg if r == "draw") / n) if n else None,
            "lg_home": (sum(1 for _, r in lg if r == "home") / n) if n else None,
        }


def build_features(matches, queries):
    """{qid: features} with every match whose result was known strictly
    before the query's cutoff applied, and none other. Queries and updates are
    merged in time order; at an equal timestamp the query goes first, so a
    result known exactly at the cutoff is excluded."""
    events = [(m["known_at"], 1, m["kickoff"], str(m["id"]), m) for m in matches]
    events += [(q["cutoff"], 0, 0, str(q["qid"]), q) for q in queries]
    events.sort(key=lambda e: e[:4])
    st, out = _State(), {}
    for _, kind, _, _, obj in events:
        if kind == 1:
            st.update(obj)
        else:
            out[obj["qid"]] = st.query(obj)
    return out


def join_kalshi(home, away, kickoff, candidates, window=JOIN_WINDOW_S):
    """(match_id, swapped) of the one soccer.db match with both teams within
    `window` of the kickoff, or None if there are none or several. Straight
    (home = home) matches win over swapped ones."""
    straight, swapped = set(), set()
    for c in candidates:
        if abs(c["kickoff"] - kickoff) > window:
            continue
        if names_match(home, c["home"]) and names_match(away, c["away"]):
            straight.add(c["id"])
        elif names_match(home, c["away"]) and names_match(away, c["home"]):
            swapped.add(c["id"])
    hits, was_swapped = (straight, False) if straight else (swapped, True)
    return (next(iter(hits)), was_swapped) if len(hits) == 1 else None


# ── build ─────────────────────────────────────────────────────────────────────

FEATURES = list(_State().query({"home_key": "_", "away_key": "_", "league": "_",
                                "cutoff": 0}).keys())


def load_matches(scon):
    rows = scon.execute(
        "SELECT match_id, league, home, away, home_key, away_key, kickoff_ts, known_at, "
        "home_goals, away_goals, shots_home, shots_away, odds_home, odds_draw, odds_away "
        "FROM matches WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL").fetchall()
    return [{"id": r[0], "league": r[1], "home": r[2], "away": r[3], "home_key": r[4],
             "away_key": r[5], "kickoff": r[6], "known_at": r[7], "hg": r[8], "ag": r[9],
             "shots_h": r[10], "shots_a": r[11], "odds": (r[12], r[13], r[14])} for r in rows]


def load_kalshi_legs(hcon):
    """{event: {"kickoff", "home", "away", "legs": [(ticker, sub, result)]}}."""
    marks = ",".join("?" * len(ESPN_BACKED))
    rows = hcon.execute(
        "SELECT m.event_ticker, m.ticker, m.sub_title, m.home, m.away, m.result, k.kickoff_ts "
        "FROM markets m JOIN kickoffs k USING(event_ticker) WHERE m.series LIKE '%GAME' "
        f"AND m.result IN ('yes','no') AND k.status IN ({marks})", ESPN_BACKED).fetchall()
    games = {}
    for ev, t, sub, home, away, res, ko in rows:
        g = games.setdefault(ev, {"kickoff": ko, "home": home, "away": away, "legs": []})
        g["legs"].append((t, sub, res))
    return games


def build(scon, hcon, out_path=OUT_DB):
    matches = load_matches(scon)
    by_id = {m["id"]: m for m in matches}

    # Table A
    qa = [{"qid": m["id"], "home_key": m["home_key"], "away_key": m["away_key"],
           "league": m["league"], "cutoff": m["kickoff"] - 3600} for m in matches]

    # Table B: join Kalshi games to soccer.db matches
    games = load_kalshi_legs(hcon)
    by_day = defaultdict(list)
    for m in matches:
        by_day[m["kickoff"] // DAY].append(m)
    joined, unmatched = {}, []
    for ev, g in games.items():
        d = g["kickoff"] // DAY
        cands = [m for k in (d - 2, d - 1, d, d + 1, d + 2) for m in by_day.get(k, [])]
        hit = join_kalshi(g["home"], g["away"], g["kickoff"], cands)
        if hit:
            joined[ev] = hit
        else:
            unmatched.append(ev)
    qb = []
    for ev, (mid, _) in joined.items():
        m = by_id[mid]
        for h in ENTRY_HOURS:
            qb.append({"qid": f"{ev}|{h}", "home_key": m["home_key"], "away_key": m["away_key"],
                       "league": m["league"], "cutoff": games[ev]["kickoff"] - h * 3600})

    feats = build_features(matches, qa + qb)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    out = sqlite3.connect(out_path)
    fcols = ", ".join(f"{c} REAL" for c in FEATURES)
    out.execute(f"CREATE TABLE ds_matches (match_id TEXT PRIMARY KEY, league TEXT, kickoff INTEGER, "
                f"split TEXT, target TEXT, p_book_h REAL, p_book_d REAL, p_book_a REAL, {fcols})")
    out.execute(f"CREATE TABLE ds_kalshi (event_ticker TEXT, ticker TEXT, match_id TEXT, "
                f"league TEXT, kickoff INTEGER, entry_h INTEGER, side TEXT, bid INTEGER, "
                f"ask INTEGER, mid REAL, won INTEGER, {fcols}, PRIMARY KEY (ticker, entry_h))")
    ph = ", ".join("?" * (8 + len(FEATURES)))
    rows_a = []
    for m in matches:
        book = devig(*m["odds"]) or (None, None, None)
        f = feats[m["id"]]
        rows_a.append((m["id"], m["league"], m["kickoff"], split_of(m["kickoff"]),
                       outcome(m["hg"], m["ag"]), *book, *[f[c] for c in FEATURES]))
    out.executemany(f"INSERT INTO ds_matches VALUES ({ph})", rows_a)

    rows_b = []
    for ev, (mid, swapped) in joined.items():
        g, m = games[ev], by_id[mid]
        for t, sub, res in g["legs"]:
            if sub == "Tie":
                side = "draw"
            elif names_match(sub, g["home"]):
                side = "away" if swapped else "home"
            elif names_match(sub, g["away"]):
                side = "home" if swapped else "away"
            else:
                continue
            bars = {ts: (b, a) for ts, b, a in hcon.execute(
                "SELECT ts, yes_bid, yes_ask FROM backfill_candles "
                "WHERE ticker=? AND interval_min=60 AND ts<?", (t, g["kickoff"]))}
            for h in ENTRY_HOURS:
                q = entry_quote(bars, g["kickoff"], h)
                if not q:
                    continue
                f = feats[f"{ev}|{h}"]
                rows_b.append((ev, t, mid, m["league"], g["kickoff"], h, side, q[0], q[1],
                               (q[0] + q[1]) / 200, int(res == "yes"),
                               *[f[c] for c in FEATURES]))
    phb = ", ".join("?" * (11 + len(FEATURES)))
    out.executemany(f"INSERT INTO ds_kalshi VALUES ({phb})", rows_b)
    out.commit()
    return out, {"matches": len(matches), "kalshi_games": len(games),
                 "kalshi_joined": len(joined), "kalshi_unmatched": len(unmatched),
                 "kalshi_rows": len(rows_b), "unmatched_sample": sorted(unmatched)[:25]}


# ── first bars to beat ────────────────────────────────────────────────────────

def baselines(out):
    """Leg Brier of base rates and bookmakers (Table A), and of base rates and
    Kalshi's mid (Table B), each on the rows where both can be scored."""
    res = {}
    for split in ("train", "tune", "test"):
        rows = out.execute(
            "SELECT target, p_book_h, p_book_d, p_book_a, lg_home, lg_draw FROM ds_matches "
            "WHERE split=? AND p_book_h IS NOT NULL AND lg_home IS NOT NULL", (split,)).fetchall()
        book, base = [], []
        for tgt, bh, bd, ba, lh, ld in rows:
            for side, pb, pr in (("home", bh, lh), ("draw", bd, ld), ("away", ba, 1 - lh - ld)):
                y = int(tgt == side)
                book.append((pb, y))
                base.append((pr, y))
        res[f"A_{split}"] = {"matches": len(rows), "brier_bookmaker": leg_brier(book),
                             "brier_base_rate": leg_brier(base)}
    for h in ENTRY_HOURS:
        rows = out.execute("SELECT side, mid, won, lg_home, lg_draw FROM ds_kalshi "
                           "WHERE entry_h=? AND lg_home IS NOT NULL", (h,)).fetchall()
        kal = [(mid, won) for _, mid, won, _, _ in rows]
        base = [({"home": lh, "draw": ld, "away": 1 - lh - ld}[s], won)
                for s, _, won, lh, ld in rows]
        res[f"B_{h}h"] = {"legs": len(rows), "brier_kalshi_mid": leg_brier(kal),
                          "brier_base_rate": leg_brier(base)}
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--soccer-db", default=SOCCER_DB)
    ap.add_argument("--history-db", default=HISTORY_DB)
    a = ap.parse_args()
    out, info = build(sqlite3.connect(a.soccer_db), sqlite3.connect(a.history_db))
    info["splits"] = dict(out.execute("SELECT split, COUNT(*) FROM ds_matches GROUP BY split"))
    info["with_bookmaker_odds"] = out.execute(
        "SELECT COUNT(*) FROM ds_matches WHERE p_book_h IS NOT NULL").fetchone()[0]
    info["baselines"] = baselines(out)
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "v2_phase0.json"), "w") as f:
        json.dump(info, f, indent=1)
    print(json.dumps({k: v for k, v in info.items() if k != "unmatched_sample"}, indent=1))


if __name__ == "__main__":
    main()
