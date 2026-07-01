#!/usr/bin/env python3
"""
leaderboard.py — rank the paper-tournament teams by realized PnL and risk.

Reads tournament/<name>_state.json (account) + logs/paper_<name>.jsonl (the
closed-PnL series from SETTLE and EXIT events). Computes per team:
  realized PnL, ROI, #closed bets, win rate, a Sharpe-like score
  (mean/std of per-bet PnL), and max drawdown of the cumulative curve.
Ranks by the risk-adjusted score. This is the input to promotion (Phase 5).

Usage: python3 leaderboard.py
"""
import glob
import json
import math
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "tournament")
LOG_DIR = os.path.join(BASE_DIR, "logs")


def _closed_pnls(name):
    """Per-bet realized PnLs (SETTLE + EXIT events), in order."""
    pnls = []
    path = os.path.join(LOG_DIR, f"paper_{name}.jsonl")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") in ("SETTLE", "EXIT") and r.get("pnl") is not None:
                pnls.append(r["pnl"])
    return pnls


def _max_drawdown(pnls):
    """Max peak-to-trough drop of the cumulative PnL curve."""
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd  # <= 0


def _metrics(acct, pnls):
    n = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    mean = sum(pnls) / n if n else 0.0
    if n >= 2:
        var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
        std = math.sqrt(var)
        sharpe = (mean / std) if std > 1e-9 else 0.0
    else:
        sharpe = 0.0
    starting = acct.get("starting_cash", 200.0)
    realized = acct.get("realized_pnl", 0.0)
    return {
        "realized": realized,
        "roi": realized / starting if starting else 0.0,
        "closed": n,
        "win_rate": (wins / n) if n else 0.0,
        "sharpe": sharpe,
        "max_dd": _max_drawdown(pnls),
        "open": len(acct.get("positions", {})),
        "cash": acct.get("cash", starting),
    }


def main():
    rows = []
    for path in sorted(glob.glob(os.path.join(STATE_DIR, "*_state.json"))):
        try:
            acct = json.load(open(path))
        except Exception:
            continue
        name = acct.get("name", os.path.basename(path))
        m = _metrics(acct, _closed_pnls(name))
        rows.append((name, m))

    if not rows:
        print("No tournament teams yet. Run agents/tournament.py first.")
        return

    # Rank: Sharpe primary (risk-adjusted), realized PnL as tiebreaker.
    rows.sort(key=lambda r: (r[1]["sharpe"], r[1]["realized"]), reverse=True)

    print(f"{'rank':<5}{'team':<24}{'realized':>10}{'ROI':>8}{'closed':>8}"
          f"{'win%':>7}{'sharpe':>8}{'maxDD':>9}{'open':>6}")
    print("-" * 84)
    for i, (name, m) in enumerate(rows, 1):
        print(f"{i:<5}{name:<24}{m['realized']:>+9.2f} {m['roi']:>+7.1%}"
              f"{m['closed']:>8}{m['win_rate']:>6.0%}{m['sharpe']:>8.2f}"
              f"{m['max_dd']:>+9.2f}{m['open']:>6}")

    best = rows[0]
    print(f"\nLeader (risk-adjusted): {best[0]} "
          f"(Sharpe {best[1]['sharpe']:.2f}, ${best[1]['realized']:+.2f} on "
          f"{best[1]['closed']} closed bets)")
    if best[1]["closed"] < 30:
        print("  → not yet promotable (need ≥30 closed bets for a stable read).")


if __name__ == "__main__":
    main()
