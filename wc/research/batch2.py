"""Batch 2 pre-registered tests: does casual ("dumb") money leave a mark on the
price? The rules are in docs/findings/11_PREREG_BATCH2.md; this module only
executes them.

    python -m wc.research.batch2 names      # big-club spelling check (team names only)
    python -m wc.research.batch2 develop    # grids on develop -> batch2_frozen.json
    python -m wc.research.batch2 test       # frozen settings on test, once

Runs on the droplet snapshot copied to data/droplet/. Prices in integer cents.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict

from wc import paths
from wc.research import batch1 as b1
from wc.research.kickoff import parse_ts

H = 3600
DAY = 24 * H
DROPLET = os.path.join(paths.ROOT, "data", "droplet")
DB = os.path.join(DROPLET, "history_snapshot.db")
ESPN_DIR = os.path.join(DROPLET, "espn_cache")
RESULTS = b1.RESULTS
FROZEN = os.path.join(RESULTS, "batch2_frozen.json")
MIN_BETS = 50

PERIODS = {
    "develop": (0, b1._day_end(2026, 8, 31)),
    "test": (b1._day_end(2026, 8, 31) + 1, 2 ** 62),
}

BIG_CLUBS = {"Real Madrid", "Barcelona", "Manchester City", "Manchester United", "Liverpool",
             "Arsenal", "Chelsea", "Bayern Munich", "PSG", "Juventus", "Inter", "Milan"}

COMPLETED = {"STATUS_FULL_TIME", "STATUS_FINAL_PEN", "STATUS_FINAL_AET"}
NOT_90_MIN = {"STATUS_FINAL_PEN", "STATUS_FINAL_AET"}
LOOKBACK = 14 * DAY
KNOWN_AFTER = 3 * H


# ── one bet ───────────────────────────────────────────────────────────────────

def bet(game, ticker, side, entry_h):
    """1 contract on `side` of `ticker` at the entry bar, taker at the ask,
    held to settlement. `bias` = won − mid, the mispricing before costs."""
    q = b1.entry_quote(game["legs"].get(ticker, {}), game["kickoff"], entry_h)
    if not q:
        return None
    bid, ask = q
    mid = (bid + ask) / 2
    price, mid = (ask, mid) if side == "yes" else (100 - bid, 100 - mid)
    won = game["results"][ticker] == side
    fee = b1.taker_fee(price)
    return {"ev": game["ev"], "ticker": ticker, "side": side, "price": price, "fee": fee,
            "pnl": 100 * won - price - fee, "bias": 100 * won - mid}


def _keep(trades):
    return [t for t in trades if t]


# ── H1 big clubs ──────────────────────────────────────────────────────────────

def h1_trades(game, entry_h):
    return _keep(bet(game, t, "no", entry_h) for t, sub in game["subs"].items()
                 if game["roles"].get(t) in ("home", "away") and sub in BIG_CLUBS)


# ── H2 overreaction ───────────────────────────────────────────────────────────

def team_history(events):
    """{espn team id: sorted [(kickoff_ts, goal margin for that team)]} from
    completed ESPN events. Extra time / penalties count as margin 0, because
    Kalshi settles on 90 minutes."""
    hist = defaultdict(set)
    for e in events:
        comp = (e.get("competitions") or [{}])[0]
        status = ((comp.get("status") or {}).get("type") or {}).get("name")
        ts = parse_ts(e.get("date"))
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        if status not in COMPLETED or ts is None or set(sides) != {"home", "away"}:
            continue
        try:
            hs, as_ = int(sides["home"]["score"]), int(sides["away"]["score"])
        except (KeyError, TypeError, ValueError):
            continue
        m = 0 if status in NOT_90_MIN else hs - as_
        hist[sides["home"]["team"]["id"]].add((ts, m))
        hist[sides["away"]["team"]["id"]].add((ts, -m))
    return {k: sorted(v) for k, v in hist.items()}


def last_margin(hist, team, kickoff, entry_h):
    """Margin of the team's last match known by the entry time (kicked off at
    least 3h before it) and within 14 days of this game; else None."""
    entry = kickoff - entry_h * H
    prev = [(ts, m) for ts, m in hist.get(team, ())
            if ts + KNOWN_AFTER <= entry and ts >= kickoff - LOOKBACK]
    return max(prev)[1] if prev else None


def espn_sides(events):
    """{espn event id: {"home": team id, "away": team id}}."""
    out = {}
    for e in events:
        comp = (e.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c.get("team", {}).get("id")
                 for c in comp.get("competitors") or []}
        if sides.get("home") and sides.get("away"):
            out[e["id"]] = {"home": sides["home"], "away": sides["away"]}
    return out


def h2_trades(game, d, hist, espn, entry_h=24):
    sides = espn.get(game.get("espn_id"))
    if not sides:
        return []
    out = []
    for t, role in game["roles"].items():
        if role not in ("home", "away"):
            continue
        espn_role = role if not game.get("swapped") else ("away" if role == "home" else "home")
        m = last_margin(hist, sides[espn_role], game["kickoff"], entry_h)
        if m is not None and m >= d:
            out.append(bet(game, t, "no", entry_h))
        elif m is not None and m <= -d:
            out.append(bet(game, t, "yes", entry_h))
    return _keep(out)


# ── H3 draws ──────────────────────────────────────────────────────────────────

def h3_trades(game, entry_h, min_mid):
    out = []
    for t, role in game["roles"].items():
        if role != "tie":
            continue
        q = b1.entry_quote(game["legs"][t], game["kickoff"], entry_h)
        if q and (q[0] + q[1]) / 2 >= min_mid:
            out.append(bet(game, t, "yes", entry_h))
    return _keep(out)


# ── H4 busy games, fade the favourite ─────────────────────────────────────────

def game_volume(game, entry_h):
    cut = game["kickoff"] - entry_h * H
    return sum(v or 0 for bars in game["vol"].values() for ts, v in bars.items() if ts <= cut)


def favourite(game, entry_h):
    best = None
    for t, role in game["roles"].items():
        if role not in ("home", "away"):
            continue
        q = b1.entry_quote(game["legs"][t], game["kickoff"], entry_h)
        if q and (best is None or sum(q) > best[1]):
            best = (t, sum(q))
    return best[0] if best else None


def h4_trades(game, min_volume, entry_h=24):
    if game_volume(game, entry_h) < min_volume:
        return []
    fav = favourite(game, entry_h)
    return _keep([bet(game, fav, "no", entry_h)]) if fav else []


def percentile(values, top_share):
    """Smallest volume that puts a game in the top `top_share` of `values`."""
    v = sorted(values, reverse=True)
    k = max(1, int(round(len(v) * top_share)))
    return v[k - 1]


# ── scoring ───────────────────────────────────────────────────────────────────

def summarize(trades, by_ev, entry_h):
    r = b1._summ(trades, by_ev, entry_h)
    r["bias_before_costs"] = b1.bootstrap([{"ev": t["ev"], "pnl": t["bias"]} for t in trades])
    return r


def select(grid):
    ok = {k: v for k, v in grid.items() if v["n_trades"] >= MIN_BETS}
    return max(ok, key=lambda k: (ok[k]["mean"], ok[k]["n_trades"])) if ok else None


def grids(games, hist, espn, h4_cutoffs):
    """{test: {setting: (params, trades, entry_h)}} for every pre-registered setting."""
    def run(f):
        return [x for g in games for x in f(g)]
    g = {"h1": {}, "h2": {}, "h3": {}, "h4": {}}
    for h in (24, 6):
        g["h1"][f"entry{h}"] = ({"entry_h": h}, run(lambda x: h1_trades(x, h)), h)
    for d in (2, 3):
        g["h2"][f"D{d}"] = ({"D": d, "entry_h": 24}, run(lambda x: h2_trades(x, d, hist, espn)), 24)
    for h in (24, 6):
        for mm in (0, 25):
            g["h3"][f"entry{h}_mid{mm}"] = ({"entry_h": h, "min_mid": mm},
                                            run(lambda x: h3_trades(x, h, mm)), h)
    for share, cut in h4_cutoffs.items():
        g["h4"][f"top{int(share * 100)}"] = ({"top_share": share, "min_volume": cut,
                                              "entry_h": 24},
                                             run(lambda x: h4_trades(x, cut)), 24)
    return g


def trades_for(test, params, games, hist, espn):
    if test == "h1":
        return [x for g in games for x in h1_trades(g, params["entry_h"])]
    if test == "h2":
        return [x for g in games for x in h2_trades(g, params["D"], hist, espn)]
    if test == "h3":
        return [x for g in games for x in h3_trades(g, params["entry_h"], params["min_mid"])]
    return [x for g in games for x in h4_trades(g, params["min_volume"])]


def busy_vs_quiet(games, min_volume):
    """H4 diagnostic: favourite won − mid in busy vs quiet games."""
    out = {}
    for name, keep in (("busy", lambda v: v >= min_volume), ("quiet", lambda v: v < min_volume)):
        tr = []
        for g in games:
            fav = favourite(g, 24)
            if fav and keep(game_volume(g, 24)):
                tr.append(bet(g, fav, "yes", 24))
        tr = _keep(tr)
        out[name] = b1.bootstrap([{"ev": t["ev"], "pnl": t["bias"]} for t in tr])
    return out


# ── data ──────────────────────────────────────────────────────────────────────

REG_TIME = "Reg Time: "


def clean_roles(game, teams):
    """Cup series title legs "Reg Time: <team>"; strip it and redo the roles.
    `teams`: {ticker: (home, away)}."""
    for t, sub in game["subs"].items():
        if sub and sub.startswith(REG_TIME):
            sub = game["subs"][t] = sub[len(REG_TIME):]
            home, away = teams.get(t, (None, None))
            game["roles"][t] = ("tie" if sub == "Tie" else "home" if sub == home
                                else "away" if sub == away else None)


def load(con, period):
    games = b1.load_games(con, period, PERIODS)
    ko = {ev: (eid, sw) for ev, eid, sw in
          con.execute("SELECT event_ticker, espn_id, swapped FROM kickoffs").fetchall()}
    teams = {t: (h, a) for t, h, a in con.execute(
        "SELECT ticker, home, away FROM markets WHERE series LIKE '%GAME'").fetchall()}
    for g in games:
        clean_roles(g, teams)
        eid, sw = ko.get(g["ev"], (None, 0))
        g["espn_id"], g["swapped"] = eid, bool(sw)
        g["vol"] = {t: dict(con.execute(
            "SELECT ts, volume FROM backfill_candles WHERE ticker=? AND interval_min=60 AND ts<?",
            (t, g["kickoff"])).fetchall()) for t in g["legs"]}
    return games


def load_espn(espn_dir=ESPN_DIR):
    events = []
    for f in sorted(os.listdir(espn_dir)):
        if f.endswith(".json"):
            with open(os.path.join(espn_dir, f)) as fh:
                events += json.load(fh).get("events") or []
    return events


def main_names(con):
    rows = con.execute("SELECT DISTINCT sub_title FROM markets WHERE series LIKE '%GAME'").fetchall()
    keys = ("madrid", "barcelon", "manchester", "man ", "liverpool", "arsenal", "chelsea",
            "bayern", "paris", "psg", "saint-germain", "juventus", "inter", "milan")
    for (n,) in sorted(r for r in rows if r[0]):
        n = n[len(REG_TIME):] if n.startswith(REG_TIME) else n
        if any(k in n.lower() for k in keys):
            print(("BIG  " if n in BIG_CLUBS else "     ") + n)


def _write(name, obj):
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, name)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)
    print("wrote", os.path.relpath(path, paths.ROOT))


def _line(name, r):
    b = r["bias_before_costs"]
    print(f"  {name:16} n={r['n_trades']:5} games={r['n_games']:5} net={r['mean']} "
          f"ci99={r['ci99']}  bias={b['mean']} bias_ci99={b['ci99']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["names", "develop", "test"])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--espn", default=ESPN_DIR)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    if a.mode == "names":
        return main_names(con)
    events = load_espn(a.espn)
    hist, espn = team_history(events), espn_sides(events)

    if a.mode == "develop":
        games = load(con, "develop")
        by_ev = {g["ev"]: g for g in games}
        vols = [game_volume(g, 24) for g in games]
        cutoffs = {s: percentile(vols, s) for s in (0.25, 0.10)}
        out, frozen = {"games": len(games)}, {}
        for test, settings in grids(games, hist, espn, cutoffs).items():
            res = {k: {**p, **summarize(tr, by_ev, h)} for k, (p, tr, h) in settings.items()}
            pick = select(res)
            out[test] = {"grid": res, "selected": pick}
            frozen[test] = None if pick is None else settings[pick][0]
            print(test, "selected:", pick)
            for k, r in res.items():
                _line(k, r)
        _write("batch2_develop.json", out)
        _write("batch2_frozen.json", frozen)
        return

    b1.assert_committed(FROZEN)
    with open(FROZEN) as f:
        frozen = json.load(f)
    games = load(con, "test")
    by_ev = {g["ev"]: g for g in games}
    out = {"games": len(games)}
    for test, params in frozen.items():
        if params is None:
            out[test] = {"status": "too few bets on develop; not run"}
            print(test, "not run (too few bets on develop)")
            continue
        r = summarize(trades_for(test, params, games, hist, espn), by_ev, params["entry_h"])
        r["passes"] = r["n_trades"] >= MIN_BETS and r["ci99"][0] > 0
        out[test] = {**params, **r}
        _line(test, r)
        print("   passes:", r["passes"])
    if frozen.get("h4"):
        out["h4_busy_vs_quiet"] = busy_vs_quiet(games, frozen["h4"]["min_volume"])
    _write("batch2_test.json", out)


if __name__ == "__main__":
    main()
