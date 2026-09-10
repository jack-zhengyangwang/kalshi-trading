"""Build the merged match table from every source.

    python3 -m wc.firm.ingest                    # openfootball + HF + Kalshi
    python3 -m wc.firm.ingest --no-hf --seasons 2

Then rebuild the point-in-time knowledge that rests on it:

    python3 -m wc.firm.sources.derived

Order matters. `derived.py` walks this table forward to produce Elo, form,
home/away records and head-to-head, so the knowledge is only ever as good as
the merge — which is why the guards run here and why a blocking finding is
printed loudly rather than returned quietly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from wc.backtest import data as market_data
from wc.firm import matches as M
from wc.firm.sources import hf_datalake, openfootball, teams


def kalshi_rows(con):
    """Settled Kalshi fixtures, contributing the market linkage.

    Kalshi knows WHO won but not the scoreline, so it fills `kalshi_event` and
    nothing else. Its value here is the join back to a tradeable market.
    """
    from wc.firm.sources.derived import settled_matches
    out = []
    for ts, event, home, away, _outcome in settled_matches(con):
        out.append({"league": _league_of(event), "kickoff_ts": ts,
                    "home_raw": home, "away_raw": away,
                    "home_goals": None, "away_goals": None,
                    "kalshi_event": event, "source": "kalshi"})
    return out


_SERIES_TO_LEAGUE = None


def _league_of(event_ticker):
    global _SERIES_TO_LEAGUE
    if _SERIES_TO_LEAGUE is None:
        _SERIES_TO_LEAGUE = {}
        for name, cfg in teams.load_leagues().items():
            if cfg.get("kalshi"):
                _SERIES_TO_LEAGUE[cfg["kalshi"]] = name
    return _SERIES_TO_LEAGUE.get(str(event_ticker).split("-")[0], "Other")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Merge every soccer source.")
    ap.add_argument("--db", default=M.DB_PATH)
    ap.add_argument("--market-db", default=market_data.DB_PATH)
    ap.add_argument("--seasons", type=int, default=4,
                    help="openfootball seasons back")
    ap.add_argument("--since-year", type=int, default=2021,
                    help="HF datalake cutoff")
    ap.add_argument("--no-openfootball", action="store_true")
    ap.add_argument("--no-hf", action="store_true")
    ap.add_argument("--no-kalshi", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    leagues = teams.load_leagues()
    say = (lambda m: None) if args.json else (lambda m: print(m, flush=True))

    of_rows = hf_rows = k_rows = []
    if not args.no_openfootball:
        say("\n  openfootball")
        of_rows = openfootball.collect(leagues, back=args.seasons,
                                       on_progress=say)
    if not args.no_hf:
        say("\n  HF datalake")
        hf_rows = hf_datalake.collect(leagues, since_year=args.since_year,
                                      on_progress=say)
    if not args.no_kalshi:
        say("\n  Kalshi")
        try:
            k_rows = kalshi_rows(market_data.connect(args.market_db))
            say(f"  settled fixtures: {len(k_rows):,}")
        except Exception as e:
            say(f"  skipped: {str(e)[:80]}")

    # openfootball first: its names are fullest, so they become the canonical
    # display for every club that appears in more than one feed.
    registry = M.build_registry(of_rows, hf_rows, k_rows)
    merger = M.Merger(registry)
    for rows in (of_rows, hf_rows, k_rows):
        for r in rows:
            merger.add(r)

    merged = merger.finish()
    con = M.connect(args.db)
    M.write(con, merged)
    rep = M.report(con, registry=registry, conflicts=merger.conflicts)
    rep["ingest"] = merger.stats

    if args.json:
        print(json.dumps(rep, indent=2))
        return 0

    lo, hi = rep["span"]
    fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d") if t else "—"
    print(f"\n  {rep['matches']:,} merged fixtures   {fmt(lo)} -> {fmt(hi)}")
    print(f"  new {merger.stats['new']:,} | joined {merger.stats['merged']:,} "
          f"| unresolved {merger.stats['unresolved']:,} "
          f"| score conflicts {merger.stats['conflicts']:,}")

    print("\n  by source combination:")
    for k, v in rep["by_source"].items():
        print(f"    {k:<32} {v:>7,}")

    print("\n  column coverage (empty where a source lacks it):")
    for col, c in rep["column_coverage"].items():
        print(f"    {col:<20} {c['n']:>7,}  {c['pct']:.1%}")

    t = rep.get("teams") or {}
    print(f"\n  registry: {t.get('teams', 0):,} clubs across "
          f"{t.get('leagues', 0)} leagues, "
          f"{t.get('unresolved_count', 0):,} unresolved")

    if rep["conflict_examples"]:
        print("\n  score disagreements (logged, not silently reconciled):")
        for c in rep["conflict_examples"]:
            print(f"    {c['home']} v {c['away']}: {c['detail']}")

    if rep["blocking"]:
        print("\n  BLOCKING — do not build knowledge on this table:")
        for b in rep["blocking"]:
            print(f"    - {b}")
    else:
        print("\n  no blocking issues")
    print(f"\n  -> {args.db}\n  next: python3 -m wc.firm.sources.derived\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
