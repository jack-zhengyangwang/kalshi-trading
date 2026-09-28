"""Batch 1 pre-registered strategy tests. The rules are in
docs/findings/05_PREREG_BATCH1.md; this module only executes them.

    python -m wc.research.batch1 three-way      # #4: no parameters, develop + test together

Every price is in integer cents, as the history DB stores it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
from collections import defaultdict

from wc import paths

H = 3600
MAX_SPREAD_C = 10
ESPN_BACKED = ("agree", "disagree", "espn_only")
RESULTS = os.path.join(paths.ROOT, "docs", "findings", "results")


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


# ── data ──────────────────────────────────────────────────────────────────────

def load_games(con, period):
    """Games whose every leg settled yes/no inside `period`, with an ESPN
    kickoff, and their hourly quotes before kickoff."""
    lo, hi = PERIODS[period]
    marks = ",".join("?" * len(ESPN_BACKED))
    rows = con.execute(
        "SELECT m.event_ticker, m.ticker, m.result, m.close_time, k.kickoff_ts "
        "FROM markets m JOIN kickoffs k USING(event_ticker) "
        f"WHERE m.series LIKE '%GAME' AND k.status IN ({marks})",
        ESPN_BACKED).fetchall()
    by_ev = defaultdict(list)
    for ev, t, res, close, ko in rows:
        by_ev[ev].append((t, res, close, ko))
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
                      "results": {t: res for t, res, _, _ in legs}})
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
    ap.add_argument("test", choices=["three-way"])
    ap.add_argument("--db", default=os.path.join(paths.ROOT, "data", "market_history.db"))
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
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


if __name__ == "__main__":
    main()
