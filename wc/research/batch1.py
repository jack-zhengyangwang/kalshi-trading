"""Batch 1 pre-registered strategy tests. The rules are in
docs/findings/05_PREREG_BATCH1.md; this module only executes them.

    python -m wc.research.batch1 three-way      # #4: no parameters, develop + test together
    python -m wc.research.batch1 longshot develop   # #2 grid + #7 on develop
    python -m wc.research.batch1 longshot test      # frozen #2 + #7 on test, once

Every price is in integer cents, as the history DB stores it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sqlite3
import subprocess
from collections import defaultdict

from wc import paths

H = 3600
MAX_SPREAD_C = 10
ESPN_BACKED = ("agree", "disagree", "espn_only")
RESULTS = os.path.join(paths.ROOT, "docs", "findings", "results")
FROZEN = os.path.join(RESULTS, "batch1_frozen.json")
STALE_H = 6


def _day_end(y, m, d):
    return int(dt.datetime(y, m, d, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp())


# Games are assigned to a period by the close_time of their markets.
PERIODS = {
    "develop": (0, _day_end(2026, 8, 31)),
    "test": (_day_end(2026, 8, 31) + 1, _day_end(2026, 9, 14)),
}


def taker_fee(price_c):
    """Kalshi taker fee for one contract at `price_c` cents, rounded up to the
    cent: ceil(0.07 · p · (1 − p)), computed in integers so 1.75 is never 1.7499."""
    num = 7 * price_c * (100 - price_c)          # fee in units of 1/10,000 cent
    return -(-num // 10000)


def valid(quote):
    bid, ask = quote
    return bid is not None and ask is not None and bid > 0 and ask > 0 \
        and ask - bid <= MAX_SPREAD_C


# ── #4 three-way sum ──────────────────────────────────────────────────────────

def three_way_gaps(game):
    """Every pre-kickoff bar where all three legs are quoted and buying (or
    selling) the whole set clears 100c by at least 1c after taker fees.

    Buy set:  100 − (Σ asks + Σ fee(ask))   — pay under $1 for a sure $1
    Sell set: (Σ bids − Σ fee(bid)) − 100   — receive over $1 for a sure $1 liability
    """
    legs = game["legs"]
    if len(legs) != 3:
        return []
    ko = game["kickoff"]
    quoted = [{ts for ts, q in bars.items() if ts < ko and valid(q)} for bars in legs.values()]
    out = []
    for ts in sorted(set.intersection(*quoted)):
        qs = [bars[ts] for bars in legs.values()]
        buy = 100 - sum(a + taker_fee(a) for _, a in qs)
        sell = sum(b - taker_fee(b) for b, _ in qs) - 100
        for side, gap in (("buy", buy), ("sell", sell)):
            if gap >= 1:
                out.append({"ts": ts, "side": side, "gap_c": gap})
    return out


def runs(timestamps):
    """Lengths of runs of consecutive hourly bars: [t, t+1h, t+3h] -> [2, 1]."""
    out, prev = [], None
    for ts in sorted(set(timestamps)):
        if prev is not None and ts - prev == H:
            out[-1] += 1
        else:
            out.append(1)
        prev = ts
    return out


def common_bars(game):
    legs = game["legs"]
    if len(legs) != 3:
        return 0
    ko = game["kickoff"]
    return len(set.intersection(*[{ts for ts, q in b.items() if ts < ko and valid(q)}
                                  for b in legs.values()]))


# ── #2 vs #7 longshots ────────────────────────────────────────────────────────

def entry_quote(bars, kickoff, entry_h):
    """(bid, ask) of the last bar at or before kickoff − entry_h, at most
    STALE_H older than that, and valid; else None."""
    cut = kickoff - entry_h * H
    ok = [ts for ts, q in bars.items() if cut - STALE_H * H <= ts <= cut and valid(q)]
    return bars[max(ok)] if ok else None


def _trade(game, ticker, side, price, won):
    fee = taker_fee(price)
    return {"ev": game["ev"], "ticker": ticker, "side": side, "price": price, "fee": fee,
            "pnl": (100 if won else 0) - price - fee}


def longshot_no_trades(game, threshold, entry_h):
    """#2: YES mid <= threshold -> buy NO at its ask (100 − YES bid), hold."""
    out = []
    for t, bars in game["legs"].items():
        q = entry_quote(bars, game["kickoff"], entry_h)
        if q and (q[0] + q[1]) / 2 <= threshold:
            out.append(_trade(game, t, "no", 100 - q[0], game["results"][t] == "no"))
    return out


YES_LIMITS = {"home": 24, "away": 14}      # Angelini, De Angelis & Singleton 2022


def longshot_yes_trades(game, entry_h=24):
    """#7: home YES mid <= 24c or away YES mid <= 14c -> buy YES at the ask, hold."""
    out = []
    for t, bars in game["legs"].items():
        limit = YES_LIMITS.get(game["roles"].get(t))
        q = entry_quote(bars, game["kickoff"], entry_h)
        if limit is not None and q and (q[0] + q[1]) / 2 <= limit:
            out.append(_trade(game, t, "yes", q[1], game["results"][t] == "yes"))
    return out


def random_trades(games_by_ev, trades, entry_h, seed=0):
    """Benchmark: for each trade, the same game and entry bar, a random leg and
    side, bought at the ask."""
    rng = random.Random(seed)
    out = []
    for tr in trades:
        g = games_by_ev[tr["ev"]]
        options = []
        for t, bars in g["legs"].items():
            q = entry_quote(bars, g["kickoff"], entry_h)
            if q:
                options += [(t, "yes", q[1]), (t, "no", 100 - q[0])]
        t, side, price = rng.choice(options)
        out.append(_trade(g, t, side, price, g["results"][t] == side))
    return out


def bootstrap(trades, draws=2000, seed=0):
    """Mean net cents per trade with 95% / 99% intervals, resampling whole
    games (trades in one game are not independent)."""
    by_ev = defaultdict(list)
    for t in trades:
        by_ev[t["ev"]].append(t["pnl"])
    keys = sorted(by_ev)
    n = len(trades)
    if not n:
        return {"n_trades": 0, "n_games": 0, "mean": None, "ci95": None, "ci99": None}
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pick = [p for k in rng.choices(keys, k=len(keys)) for p in by_ev[k]]
        means.append(sum(pick) / len(pick))
    means.sort()

    def q(f):
        return round(means[min(draws - 1, int(f * draws))], 3)
    return {"n_trades": n, "n_games": len(keys), "mean": round(sum(t["pnl"] for t in trades) / n, 3),
            "ci95": [q(0.025), q(0.975)], "ci99": [q(0.005), q(0.995)]}


class NotFrozen(RuntimeError):
    pass


def load_frozen(path, key):
    """Frozen parameters for a test. Refuses unless the file exists, has the
    key, and is committed in git unchanged — the commit is the timestamp."""
    if not os.path.exists(path):
        raise NotFrozen(f"{path} does not exist; run develop and commit the frozen values")
    with open(path) as f:
        data = json.load(f)
    if key not in data:
        raise NotFrozen(f"{key} is not frozen in {path}")
    return data[key]


def assert_committed(path):
    rel = os.path.relpath(path, paths.ROOT)
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=paths.ROOT,
                             capture_output=True).returncode == 0
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel],
                           cwd=paths.ROOT).returncode == 0
    if not (tracked and clean):
        raise NotFrozen(f"{rel} must be committed (unchanged) before the test runs")


NO_GRID = [(t, h) for t in (5, 10, 15) for h in (24, 6)]


def _summ(trades, games_by_ev, entry_h):
    r = bootstrap(trades)
    r["random_benchmark"] = bootstrap(random_trades(games_by_ev, trades, entry_h)) \
        if trades else None
    r["capital_per_contract_c"] = round(sum(t["price"] for t in trades) / len(trades), 2) \
        if trades else None
    return r


def report_longshot(games, mode, frozen=None):
    by_ev = {g["ev"]: g for g in games}
    out = {"games": len(games)}
    if mode == "develop":
        grid = {}
        for t, h in NO_GRID:
            grid[f"T{t}_h{h}"] = {"threshold": t, "entry_h": h,
                                  **_summ([x for g in games for x in longshot_no_trades(g, t, h)],
                                          by_ev, h)}
        best = max((v for v in grid.values() if v["n_trades"]),
                   key=lambda v: (v["mean"], v["n_trades"]))
        out["longshot_no_grid"] = grid
        out["longshot_no_selected"] = {"threshold": best["threshold"], "entry_h": best["entry_h"]}
    else:
        t, h = frozen["threshold"], frozen["entry_h"]
        r = _summ([x for g in games for x in longshot_no_trades(g, t, h)], by_ev, h)
        r["passes"] = bool(r["n_trades"]) and r["ci99"][0] > 0
        out["longshot_no"] = {"threshold": t, "entry_h": h, **r}
    r7 = _summ([x for g in games for x in longshot_yes_trades(g)], by_ev, 24)
    if mode == "test":
        r7["passes"] = bool(r7["n_trades"]) and r7["ci99"][0] > 0
    out["longshot_yes"] = r7
    return out


# ── data ──────────────────────────────────────────────────────────────────────

def load_games(con, period):
    """Games whose every leg settled yes/no inside `period`, with an ESPN
    kickoff, and their hourly quotes before kickoff."""
    lo, hi = PERIODS[period]
    marks = ",".join("?" * len(ESPN_BACKED))
    rows = con.execute(
        "SELECT m.event_ticker, m.ticker, m.result, m.close_time, k.kickoff_ts, "
        "m.sub_title, m.home, m.away "
        "FROM markets m JOIN kickoffs k USING(event_ticker) "
        f"WHERE m.series LIKE '%GAME' AND k.status IN ({marks})",
        ESPN_BACKED).fetchall()
    by_ev = defaultdict(list)
    roles = {}
    for ev, t, res, close, ko, sub, home, away in rows:
        by_ev[ev].append((t, res, close, ko))
        roles[t] = ("tie" if sub == "Tie" else "home" if sub == home
                    else "away" if sub == away else None)
    games = []
    for ev, legs in by_ev.items():
        if any(res not in ("yes", "no") for _, res, _, _ in legs):
            continue
        if not all(lo <= close <= hi for _, _, close, _ in legs):
            continue
        ko = legs[0][3]
        bars = {}
        for t, _, _, _ in legs:
            bars[t] = {ts: (b, a) for ts, b, a in con.execute(
                "SELECT ts, yes_bid, yes_ask FROM backfill_candles "
                "WHERE ticker=? AND interval_min=60 AND ts<?", (t, ko))}
        games.append({"ev": ev, "kickoff": ko, "legs": bars,
                      "results": {t: res for t, res, _, _ in legs},
                      "roles": {t: roles[t] for t, _, _, _ in legs}})
    return games


# ── reports ───────────────────────────────────────────────────────────────────

def report_three_way(games):
    usable = [g for g in games if common_bars(g) > 0]
    per_game = []
    for g in usable:
        gaps = three_way_gaps(g)
        if gaps:
            per_game.append({
                "ev": g["ev"], "bars_with_gap": len({x["ts"] for x in gaps}),
                "max_gap_c": max(x["gap_c"] for x in gaps),
                "sides": sorted({x["side"] for x in gaps}),
                "longest_run_h": max(runs([x["ts"] for x in gaps])),
                "hours_before_kickoff": sorted({(g["kickoff"] - x["ts"]) // H for x in gaps}),
            })
    n = len(usable)
    return {
        "games_3_legs_quoted": n,
        "bars_checked": sum(common_bars(g) for g in usable),
        "games_with_gap": len(per_game),
        "share_with_gap": round(len(per_game) / n, 4) if n else None,
        "passes": bool(n) and len(per_game) / n >= 0.01,
        "gaps": sorted(per_game, key=lambda r: -r["max_gap_c"]),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("test", choices=["three-way", "longshot"])
    ap.add_argument("mode", nargs="?", choices=["develop", "test"])
    ap.add_argument("--db", default=os.path.join(paths.ROOT, "data", "market_history.db"))
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    if a.test == "longshot":
        return main_longshot(con, a.mode or "develop")
    out = {p: report_three_way(load_games(con, p)) for p in PERIODS}
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "batch1_threeway.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    for p, r in out.items():
        print(f"{p:8} games={r['games_3_legs_quoted']:5} bars={r['bars_checked']:6} "
              f"with_gap={r['games_with_gap']:4} share={r['share_with_gap']} "
              f"pass={r['passes']}")
    print("wrote", os.path.relpath(path, paths.ROOT))



def main_longshot(con, mode):
    frozen = None
    if mode == "test":
        assert_committed(FROZEN)
        frozen = load_frozen(FROZEN, "longshot_no")
    out = report_longshot(load_games(con, mode), mode, frozen)
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"batch1_longshot_{mode}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "longshot_no_grid"}, indent=1))
    if mode == "develop":
        for k, v in out["longshot_no_grid"].items():
            print(f"  #2 {k:8} n={v['n_trades']:4} mean={v['mean']} ci95={v['ci95']}")
    print("wrote", os.path.relpath(path, paths.ROOT))


if __name__ == "__main__":
    main()
