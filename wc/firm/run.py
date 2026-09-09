"""Backtest a portfolio manager: its view, its capital, its whole book.

The middle of the three stages. STRATEGY defines PMs in config/pms/; BACKTEST
(here) replays them over Kalshi's archive; FORWARD TEST runs the survivors in
the arena on the live book, and only then does promotion apply.

Every PM is priced by ITS OWN view and scored on ITS OWN bankroll, so the
leaderboard compares independent track records rather than one opinion sized
several ways. That is what makes the window-A/window-B selection test in
docs/backtester/07_PROMOTION.md worth running.

Usage:
    python3 -m wc.firm.run                       # every PM in config/pms/
    python3 -m wc.firm.run --pm structural-desk --interval-min 60
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from wc import paths
from wc.backtest import data, engine, metrics
from wc.firm import journal as journal_mod
from wc.firm import pm as pm_mod

RESULTS_DIR = os.path.join(paths.ROOT, "data", "backtests")


def _ts(datestr):
    return int(dt.datetime.strptime(datestr, "%Y-%m-%d")
               .replace(tzinfo=dt.timezone.utc).timestamp())


def backtest_pm(manager, bars, costs=None):
    """Replay one PM. Returns a report with per-agent breakdown."""
    probs = manager.view.price(bars)

    jrnl = journal_mod.Journal(manager.name) if manager.learns else None
    books = [engine.Book(a.name, a.spec, a.bankroll, journal=jrnl)
             for a in manager.agents]
    trades, rej = engine.run_book(books, bars, costs=costs, model_probs=probs,
                                 pm_caps=manager.caps)

    report = metrics.summarize(trades, manager.bankroll, rej,
                               label=f"PM {manager.name}")
    report["pm"] = manager.name
    report["strategy"] = manager.name          # the dashboard keys on this
    report["view"] = manager.view.describe()
    report["description"] = manager.description
    report["bars_priced"] = len(probs)
    report["bars"] = len(bars)

    # Per-agent P&L. A desk whose profit comes from one agent while the others
    # bleed is a different thing from a desk that is broadly right, and the
    # aggregate hides which one you have.
    report["agents"] = {}
    for a in manager.agents:
        own = [t for t in trades if t.get("agent") == a.name]
        report["agents"][a.name] = {
            "allocated": round(a.bankroll, 2),
            "n_trades": len(own),
            "net_pnl": round(sum(t["net_pnl"] for t in own), 4),
            "spec": a.spec["name"],
        }

    if jrnl is not None:
        report["journal"] = jrnl.summary()
        report["journal_entries"] = len(jrnl)

    if manager.carries_lookahead:
        report["lookahead_risk"] = (
            f"PM '{manager.name}' prices with {manager.view.describe()}, which "
            f"carries state fitted on outcomes a backtest bar could not have "
            f"seen. Ranking evidence only — not promotion evidence.")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest portfolio managers.")
    ap.add_argument("--pm", nargs="*", help="PM names (default: all in config/pms/)")
    ap.add_argument("--db", default=data.DB_PATH)
    ap.add_argument("--source", choices=sorted(data.SOURCES), default="backfill")
    ap.add_argument("--interval-min", type=int, default=60)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--series", nargs="*")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage-cents", type=float, default=1.0)
    ap.add_argument("--max-volume-share", type=float, default=0.10)
    ap.add_argument("--fill-delay-bars", type=int, default=1)
    ap.add_argument("--assume-fills", action="store_true")
    ap.add_argument("--fill-rate", type=float, default=0.99)
    ap.add_argument("--out-dir", default=RESULTS_DIR)
    args = ap.parse_args(argv)

    try:
        managers = pm_mod.load_all()
    except pm_mod.PMError as e:
        print(f"[pm] {e}", file=sys.stderr)
        return 2
    if args.pm:
        wanted = set(args.pm)
        managers = [m for m in managers if m.name in wanted]
    if not managers:
        print("[pm] no portfolio managers found in config/pms/", file=sys.stderr)
        return 2

    if not os.path.exists(args.db):
        print(f"[data] no history DB at {args.db}. Run the backfill first:\n"
              f"  python3 -m wc.backtest.backfill --days 60", file=sys.stderr)
        return 3

    con = data.connect(args.db)
    bars = data.load_bars(con,
                          start_ts=_ts(args.start) if args.start else None,
                          end_ts=_ts(args.end) if args.end else None,
                          series=args.series, source=args.source,
                          interval_min=args.interval_min)
    if not bars:
        print("[data] no bars matched.", file=sys.stderr)
        return 4

    costs = engine.Costs(fee_rate=args.fee_rate,
                         slippage_cents=args.slippage_cents,
                         max_volume_share=args.max_volume_share,
                         fill_delay_bars=args.fill_delay_bars,
                         assume_fills=args.assume_fills,
                         fill_rate=args.fill_rate)

    os.makedirs(args.out_dir, exist_ok=True)
    print(f"\n  {len(bars):,} bars at {args.interval_min}m from {args.source}\n")

    rows = []
    for m in managers:
        rep = backtest_pm(m, bars, costs=costs)
        rep["costs"] = {"fee_rate": args.fee_rate,
                        "slippage_cents": args.slippage_cents,
                        "max_volume_share": args.max_volume_share,
                        "fill_delay_bars": args.fill_delay_bars,
                        "assume_fills": args.assume_fills}
        rep["data_source"] = args.source
        with open(os.path.join(args.out_dir, f"pm-{m.name}.json"), "w") as f:
            json.dump(rep, f, indent=2)
        rows.append(rep)
        flag = "  [lookahead]" if m.carries_lookahead else ""
        print(f"  {m.name:<22} {m.view.describe():<26} "
              f"net ${rep['net_pnl']:>+9,.2f}  {rep['n_trades']:>4} trades"
              f"  Brier {rep['brier'] if rep['brier'] is not None else '—'}{flag}")
        for name, a in rep["agents"].items():
            print(f"      └─ {name:<20} ${a['net_pnl']:>+9,.2f}  "
                  f"{a['n_trades']:>4} trades  (allocated ${a['allocated']:,.0f})")

    # The firm's leaderboard. `market` is the do-nothing baseline from
    # promotion gate 2.5 — a PM that cannot beat it has variance, not edge.
    base = next((r for r in rows if r["view"].startswith("market")), None)
    print(f"\n  ── firm ──")
    for r in sorted(rows, key=lambda x: -x["net_pnl"]):
        mark = ""
        if base and r is not base:
            mark = "  beats baseline" if r["net_pnl"] > base["net_pnl"] else "  BELOW baseline"
        print(f"  {r['net_pnl']:>+10,.2f}  {r['pm']}{mark}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
