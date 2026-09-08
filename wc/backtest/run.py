"""Backtest runner: walk-forward by default.

A single in-sample backtest is not evidence — any strategy can be tuned to win
on one window. The default mode splits the data into rolling windows, tests on
each unseen next window, and reports the concatenated OUT-OF-SAMPLE result as
the headline. In-sample requires an explicit flag and is labelled as such.

Usage:
    python3 -m wc.backtest.run strategies/sell-cheap-longshots.json
    python3 -m wc.backtest.run <spec> --in-sample --start 2026-01-01
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from wc import paths
from wc.backtest import data, engine, metrics, spec as spec_mod

RESULTS_DIR = os.path.join(paths.ROOT, "data", "backtests")


def _ts(datestr):
    return int(dt.datetime.strptime(datestr, "%Y-%m-%d")
               .replace(tzinfo=dt.timezone.utc).timestamp())


def walk_forward(spec, bars, n_windows=4, **kw):
    """Split chronologically into n_windows, test each after the first, and
    concatenate. Window 0 is the initial in-sample period and is never scored —
    it exists so a strategy has history before it is judged."""
    if not bars:
        return [], {}, []
    lo, hi = bars[0]["ts"], bars[-1]["ts"]
    if hi <= lo:
        return [], {}, []

    width = (hi - lo) / n_windows
    all_trades, all_rej, per_window = [], {}, []

    for i in range(1, n_windows):
        w_lo, w_hi = lo + width * i, lo + width * (i + 1)
        window = [b for b in bars if w_lo <= b["ts"] < w_hi]
        if not window:
            continue
        trades, rej = engine.run(spec, window, **kw)
        all_trades += trades
        for k, v in rej.items():
            all_rej[k] = all_rej.get(k, 0) + v
        per_window.append({
            "window": i,
            "start": int(w_lo), "end": int(w_hi),
            "n_trades": len(trades),
            "net_pnl": round(sum(t["net_pnl"] for t in trades), 4),
        })
    return all_trades, all_rej, per_window


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest a strategy spec.")
    ap.add_argument("spec", help="path to a strategy JSON file")
    ap.add_argument("--db", default=data.DB_PATH)
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--series", nargs="*", help="restrict to these series")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--windows", type=int, default=4)
    ap.add_argument("--in-sample", action="store_true",
                    help="single-pass over all data. NOT evidence — for debugging only.")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage-cents", type=float, default=1.0)
    ap.add_argument("--max-volume-share", type=float, default=0.10)
    ap.add_argument("--out", help="write metrics JSON here")
    args = ap.parse_args(argv)

    try:
        strategy = spec_mod.load(args.spec)
    except spec_mod.SpecError as e:
        print(f"[spec] {e}", file=sys.stderr)
        return 2

    if not os.path.exists(args.db):
        print(f"[data] no history DB at {args.db}. Run the backfill first "
              f"(wc/backtest/data.py) — see docs/backtester/01_DATA.md",
              file=sys.stderr)
        return 3

    con = data.connect(args.db)
    bars = data.load_bars(con,
                          start_ts=_ts(args.start) if args.start else None,
                          end_ts=_ts(args.end) if args.end else None,
                          series=args.series)
    if not bars:
        print("[data] no bars matched. Widen the window or backfill more history.",
              file=sys.stderr)
        return 4

    costs = engine.Costs(fee_rate=args.fee_rate,
                         slippage_cents=args.slippage_cents,
                         max_volume_share=args.max_volume_share)

    if args.in_sample:
        trades, rej = engine.run(strategy, bars, args.bankroll, costs)
        per_window, label = [], "in-sample (NOT evidence)"
    else:
        trades, rej, per_window = walk_forward(
            strategy, bars, args.windows,
            starting_bankroll=args.bankroll, costs=costs)
        label = f"out-of-sample ({args.windows} windows)"

    report = metrics.summarize(trades, args.bankroll, rej, label=label)

    # A window narrower than the strategy's holding period liquidates every
    # position at the window edge, which makes the result meaningless. Say so
    # loudly rather than reporting a confident wrong number.
    warnings = []
    n = report["n_trades"]
    if n and report["n_liquidated_at_end"] / n > 0.5:
        warnings.append(
            f"{report['n_liquidated_at_end']}/{n} trades were force-liquidated at a "
            f"window edge rather than settled. The walk-forward windows are likely "
            f"NARROWER than this strategy's holding period — widen the date range, "
            f"lower --windows, or shorten the strategy's horizon. Treat this P&L as "
            f"unreliable.")
    if n == 0:
        warnings.append("no trades placed — check the rejection counts below.")
    report["warnings"] = warnings
    report["strategy"] = strategy["name"]
    report["spec_path"] = args.spec
    report["bars"] = len(bars)
    report["windows"] = per_window
    report["costs"] = {"fee_rate": args.fee_rate,
                       "slippage_cents": args.slippage_cents,
                       "max_volume_share": args.max_volume_share}

    out = args.out or os.path.join(RESULTS_DIR, f"{strategy['name']}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n  {strategy['name']} — {label}")
    print(f"  bars {len(bars):,} | trades {report['n_trades']:,} "
          f"(settled {report['n_settled']:,}, liquidated {report['n_liquidated_at_end']:,})")
    print(f"  gross ${report['gross_pnl']:+,.2f} | fees ${report['fees']:,.2f} "
          f"| net ${report['net_pnl']:+,.2f}")
    print(f"  win rate {report['win_rate']} | max DD ${report['max_drawdown']:,.2f} "
          f"| Sharpe {report['sharpe']}")
    if report["brier"] is not None:
        print(f"  Brier {report['brier']:.4f} | log loss {report['log_loss']:.4f}")
    if rej:
        print(f"  rejections: {rej}")
    for w in warnings:
        print(f"  [WARN] {w}")
    print(f"  -> {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
