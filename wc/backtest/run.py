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
from wc.backtest import data, engine, metrics, pricing, spec as spec_mod

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
    ap.add_argument("--source", choices=sorted(data.SOURCES), default="backfill",
                    help="'backfill' = true OHLC from Kalshi's archive (the "
                         "backtest source). 'collector' = forward snapshots, "
                         "which belong to forward testing, not backtesting")
    ap.add_argument("--interval-min", type=int,
                    help="backfill resolution to read (1, 60, 1440)")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--windows", type=int, default=4)
    ap.add_argument("--in-sample", action="store_true",
                    help="single-pass over all data. NOT evidence — for debugging only.")
    ap.add_argument("--brains", action="store_true",
                    help="price every bar with our own league brains, supplying the "
                         "model_prob / edge signals. REQUIRED by model-edge.json. "
                         "Carries model-fitted lookahead — see wc/backtest/pricing.py")
    ap.add_argument("--holdout-frac", type=float, default=0.25,
                    help="fraction of the most recent data reserved and scored "
                         "once, separately (0 disables)")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage-cents", type=float, default=1.0)
    ap.add_argument("--max-volume-share", type=float, default=0.10)
    ap.add_argument("--fill-delay-bars", type=int, default=1,
                    help="fill N bars after the deciding bar (default 1). "
                         "0 fills on the deciding bar — a DIAGNOSTIC, not a result")
    ap.add_argument("--no-latency-check", action="store_true",
                    help="skip the same-bar comparison run")
    ap.add_argument("--assume-fills", action="store_true",
                    help="assume liquidity: no volume cap, --fill-rate of each "
                         "order fills. An optimistic ASSUMPTION for answering "
                         "'is there an edge at all', not for sizing real money")
    ap.add_argument("--fill-rate", type=float, default=0.99,
                    help="fraction of a requested order that fills under "
                         "--assume-fills (default 0.99)")
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
                          series=args.series, source=args.source,
                          interval_min=args.interval_min)
    if not bars:
        print("[data] no bars matched. Widen the window or backfill more history.",
              file=sys.stderr)
        return 4

    costs = engine.Costs(fee_rate=args.fee_rate,
                         slippage_cents=args.slippage_cents,
                         max_volume_share=args.max_volume_share,
                         fill_delay_bars=args.fill_delay_bars,
                         assume_fills=args.assume_fills,
                         fill_rate=args.fill_rate)

    # ── the most recent period is held out entirely and touched ONCE ─────────
    # Walk-forward alone still tunes against every window it reports on. A
    # period never used for any decision is the only genuinely clean read, so
    # it is split off before anything else looks at the data.
    holdout = []
    if args.holdout_frac and 0 < args.holdout_frac < 1 and len(bars) > 1:
        lo, hi = bars[0]["ts"], bars[-1]["ts"]
        cut = hi - (hi - lo) * args.holdout_frac
        holdout = [b for b in bars if b["ts"] >= cut]
        bars = [b for b in bars if b["ts"] < cut]
        if not bars:                       # too little history to split
            bars, holdout = holdout, []

    model_probs, price_skips = None, {}
    if args.brains:
        model_probs, price_skips = pricing.build_model_probs(bars + holdout)
        print(f"[brains] priced {len(model_probs):,} of {len(bars) + len(holdout):,} "
              f"bars" + (f" | skipped {price_skips}" if price_skips else ""))

    run_kw = {"costs": costs, "model_probs": model_probs}

    if args.in_sample:
        trades, rej = engine.run(strategy, bars, args.bankroll, **run_kw)
        per_window, label = [], "in-sample (NOT evidence)"
    else:
        trades, rej, per_window = walk_forward(
            strategy, bars, args.windows,
            starting_bankroll=args.bankroll, **run_kw)
        label = f"out-of-sample ({args.windows} windows)"

    report = metrics.summarize(trades, args.bankroll, rej, label=label)

    # In-sample and out-of-sample are recorded in the SAME file, so nobody has
    # to go looking for the unflattering half. The dashboard shows OOS by
    # default; the comparison is what reveals over-fitting.
    if not args.in_sample:
        is_trades, is_rej = engine.run(strategy, bars, args.bankroll, **run_kw)
        report["in_sample"] = metrics.summarize(
            is_trades, args.bankroll, is_rej, label="in-sample (reference only)")

    if holdout:
        h_trades, h_rej = engine.run(strategy, holdout, args.bankroll, **run_kw)
        report["holdout"] = metrics.summarize(
            h_trades, args.bankroll, h_rej,
            label=f"HELD-OUT most recent {args.holdout_frac:.0%} — touched once")
        report["holdout"]["window"] = [holdout[0]["ts"], holdout[-1]["ts"]]

    # A window narrower than the strategy's holding period liquidates every
    # position at the window edge, which makes the result meaningless. Say so
    # loudly rather than reporting a confident wrong number.
    warnings = []
    n = report["n_trades"]
    if n and report["n_liquidated_at_end"] / n > 0.5:
        span_days = (bars[-1]["ts"] - bars[0]["ts"]) / 86400 if len(bars) > 1 else 0.0
        if span_days < 1:
            cause = (f"the data spans only {span_days * 24:.1f} hours, so no market "
                     f"had time to resolve. Collect more history before reading "
                     f"anything into this P&L")
        elif args.in_sample:
            cause = (f"the data spans {span_days:.1f} days, shorter than this "
                     f"strategy's holding period — widen the date range or shorten "
                     f"the strategy's horizon")
        else:
            cause = (f"the walk-forward windows ({span_days / args.windows:.1f} days "
                     f"each) are NARROWER than this strategy's holding period — "
                     f"widen the date range, lower --windows, or shorten the "
                     f"strategy's horizon")
        warnings.append(
            f"{report['n_liquidated_at_end']}/{n} trades were force-liquidated at a "
            f"window edge rather than settled: {cause}. Treat this P&L as unreliable.")
    if n == 0:
        warnings.append("no trades placed — check the rejection counts below.")
    if args.assume_fills:
        warnings.append(
            f"ASSUMED FILLS: no volume cap, {args.fill_rate:.0%} of each order "
            f"filled regardless of resting size. Optimistic by an unknown "
            f"amount — prediction-market books are thin, and the markets a "
            f"strategy most wants often have least size behind the quote. "
            f"Re-run without --assume-fills to see how much edge survives.")

    if args.brains:
        report["lookahead_risk"] = pricing.LOOKAHEAD_WARNING
        report["bars_priced_by_brains"] = len(model_probs or {})
        report["pricing_skips"] = price_skips
        warnings.append("LOOKAHEAD: " + pricing.LOOKAHEAD_WARNING)

    report["strategy"] = strategy["name"]
    report["spec_path"] = args.spec
    report["bars"] = len(bars)
    report["data_source"] = args.source
    report["windows"] = per_window
    report["costs"] = {"fee_rate": args.fee_rate,
                       "slippage_cents": args.slippage_cents,
                       "max_volume_share": args.max_volume_share,
                       "fill_delay_bars": args.fill_delay_bars,
                       "assume_fills": args.assume_fills,
                       "fill_rate": args.fill_rate if args.assume_fills else None}

    # ── latency sensitivity ──────────────────────────────────────────────────
    # The same strategy, filled on the bar that triggered it. That number is
    # never a result — it is the measurement of how much of the edge is the
    # strategy capturing its own trigger price. A ratio near 1 means the edge
    # survives execution; a large one means it will not survive paper trading.
    if not args.no_latency_check and args.fill_delay_bars > 0:
        same_bar_costs = engine.Costs(
            fee_rate=args.fee_rate, slippage_cents=args.slippage_cents,
            max_volume_share=args.max_volume_share, fill_delay_bars=0,
            assume_fills=args.assume_fills, fill_rate=args.fill_rate)
        sb_trades, sb_rej = engine.run(strategy, bars, args.bankroll,
                                       costs=same_bar_costs,
                                       model_probs=model_probs)
        sb = metrics.summarize(sb_trades, args.bankroll, sb_rej,
                               label="same-bar fill (DIAGNOSTIC, not a result)")
        report["same_bar_fill"] = sb
        net, sb_net = report["net_pnl"], sb["net_pnl"]
        report["latency_sensitivity"] = (
            round(sb_net / net, 3) if net > 0 and sb_net > 0 else None)
        if report["latency_sensitivity"] and report["latency_sensitivity"] > 2.0:
            warnings.append(
                f"LATENCY-SENSITIVE: filling on the deciding bar would have "
                f"returned ${sb_net:+,.2f} versus ${net:+,.2f} next-bar "
                f"({report['latency_sensitivity']}x). Most of this edge is the "
                f"strategy capturing its own trigger price, and it is unlikely "
                f"to survive live execution.")
        elif net <= 0 < sb_net:
            warnings.append(
                f"LATENCY-SENSITIVE: profitable only when filled on the "
                f"deciding bar (${sb_net:+,.2f}) and unprofitable next-bar "
                f"(${net:+,.2f}). This edge is an execution artefact.")

    report["warnings"] = warnings

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
    if "same_bar_fill" in report:
        sens = report["latency_sensitivity"]
        print(f"  same-bar   net ${report['same_bar_fill']['net_pnl']:+,.2f}"
              f"   (diagnostic"
              + (f", {sens}x)" if sens else ")"))
    for key, sub in (("in_sample", "in-sample "), ("holdout", "HELD-OUT  ")):
        if key in report:
            r = report[key]
            print(f"  {sub} trades {r['n_trades']:,} | net ${r['net_pnl']:+,.2f}"
                  + (f" | Brier {r['brier']:.4f}" if r["brier"] is not None else ""))
    if rej:
        print(f"  rejections: {rej}")
    for w in warnings:
        print(f"  [WARN] {w}")
    print(f"  -> {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
