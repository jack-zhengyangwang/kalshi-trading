"""Data quality report.

A backtest on bad data is worse than no backtest: it produces a confident wrong
number. This module answers "is the history usable, and where isn't it" BEFORE
any result is believed.

It returns a REPORT, never raises. We want to know the data is 92% complete and
exactly where the 8% is missing — a run that aborts on the first anomaly tells
us nothing about the other 99% of the store.

Usage:
    python3 -m wc.backtest.quality
    python3 -m wc.backtest.quality --series KXEPLGAME --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

from wc.backtest import data

DAY = 86400


def _iso(ts):
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def coverage(con):
    """How much history exists, and over what span."""
    lo, hi = data.date_range(con)
    return {
        "candles": data.candle_count(con),
        "markets": data.market_count(con),
        "first_candle": _iso(lo),
        "last_candle": _iso(hi),
        "span_days": round((hi - lo) / DAY, 2) if (lo and hi) else 0.0,
    }


def settlement_coverage(con):
    """Fraction of closed markets whose outcome we actually know.

    This is the single most important number in the report. Price history with
    no outcome attached cannot produce PnL, Brier, or calibration — the store
    can look full and still be worthless for backtesting.
    """
    row = con.execute("""
        SELECT
          SUM(CASE WHEN result IN ('yes','no') THEN 1 ELSE 0 END) AS resolved,
          SUM(CASE WHEN result = 'void' THEN 1 ELSE 0 END) AS voided,
          SUM(CASE WHEN result IS NULL AND close_time IS NOT NULL
                    AND close_time < strftime('%s','now') THEN 1 ELSE 0 END) AS past_close_unresolved,
          COUNT(*) AS total
        FROM markets""").fetchone()
    resolved = row["resolved"] or 0
    voided = row["voided"] or 0
    pending = row["past_close_unresolved"] or 0
    # A void IS settled — the game was called off and Kalshi paid out a fair
    # price. Counting it as missing would report a permanent data problem that
    # no amount of collecting could ever fix.
    denom = resolved + voided + pending
    return {
        "resolved": resolved,
        "voided": voided,
        "past_close_but_unresolved": pending,
        "total_markets": row["total"] or 0,
        "settlement_rate": round((resolved + voided) / denom, 4) if denom else None,
        "void_rate": round(voided / (resolved + voided), 4) if (resolved + voided) else None,
        "note": ("past_close_but_unresolved should trend to ~0. A number that "
                 "grows every cycle means the settlement sweep is not running "
                 "or is failing silently. Voids (cancelled/postponed games, "
                 "~13% of soccer markets) are settled, not missing."),
    }


def price_sanity(con):
    """0 <= bid <= ask <= 100. Crossed or absurd books mean the ingest is
    mis-mapping fields, not that the market was weird."""
    crossed = con.execute(
        "SELECT COUNT(*) n FROM candles WHERE yes_bid IS NOT NULL "
        "AND yes_ask IS NOT NULL AND yes_bid > yes_ask").fetchone()["n"]
    out_of_range = con.execute(
        "SELECT COUNT(*) n FROM candles WHERE (yes_bid < 0 OR yes_bid > 100) "
        "OR (yes_ask < 0 OR yes_ask > 100)").fetchone()["n"]
    no_book = con.execute(
        "SELECT COUNT(*) n FROM candles WHERE yes_bid IS NULL OR yes_ask IS NULL"
    ).fetchone()["n"]
    total = data.candle_count(con)
    return {
        "crossed_books": crossed,
        "out_of_range_prices": out_of_range,
        "empty_book_snapshots": no_book,
        "empty_book_pct": round(no_book / total, 4) if total else None,
        "note": ("Empty books are normal for illiquid legs and are recorded on "
                 "purpose — the engine rejects them as 'no_price'. Crossed or "
                 "out-of-range prices are NOT normal and indicate a field "
                 "mapping bug."),
    }


def clock_sanity(con):
    """Timestamps that cannot be true. These are the quiet killers: a candle
    dated after its market closed silently leaks the outcome."""
    after_close = con.execute("""
        SELECT COUNT(*) n FROM candles c JOIN markets m ON m.ticker = c.ticker
        WHERE m.close_time IS NOT NULL AND c.ts > m.close_time""").fetchone()["n"]
    before_first_seen = con.execute("""
        SELECT COUNT(*) n FROM candles c JOIN markets m ON m.ticker = c.ticker
        WHERE c.ts < m.first_seen""").fetchone()["n"]
    orphans = con.execute("""
        SELECT COUNT(*) n FROM candles c
        LEFT JOIN markets m ON m.ticker = c.ticker WHERE m.ticker IS NULL"""
    ).fetchone()["n"]
    return {
        "candles_after_close_time": after_close,
        "candles_before_first_seen": before_first_seen,
        "orphan_candles_no_market_row": orphans,
        "note": ("Orphan candles are invisible to the engine — load_bars INNER "
                 "JOINs markets, so they are silently dropped from every "
                 "backtest rather than erroring."),
    }


def gaps(con, expected_interval=300, tolerance=2.0, limit=20):
    """Missing snapshots inside a market's observed window.

    A gap is a stretch where the collector should have sampled and didn't —
    a dead cron, a failed cycle, a droplet reboot. Reported per market, worst
    first, because one badly-sampled market is a local problem while a gap
    across every market at once is an outage.
    """
    threshold = expected_interval * tolerance
    rows = con.execute("""
        SELECT ticker, ts FROM candles ORDER BY ticker, ts""").fetchall()

    by_ticker, worst = {}, []
    prev_ticker = prev_ts = None
    for r in rows:
        if r["ticker"] == prev_ticker and prev_ts is not None:
            delta = r["ts"] - prev_ts
            if delta > threshold:
                d = by_ticker.setdefault(r["ticker"], {"n": 0, "missing": 0})
                d["n"] += 1
                d["missing"] += int(delta / expected_interval) - 1
                worst.append({"ticker": r["ticker"], "start": _iso(prev_ts),
                              "end": _iso(r["ts"]),
                              "missing_snapshots": int(delta / expected_interval) - 1})
        prev_ticker, prev_ts = r["ticker"], r["ts"]

    worst.sort(key=lambda g: -g["missing_snapshots"])
    return {
        "expected_interval_sec": expected_interval,
        "markets_with_gaps": len(by_ticker),
        "total_missing_snapshots": sum(d["missing"] for d in by_ticker.values()),
        "worst": worst[:limit],
    }


def survivorship(con):
    """Markets we saw but never resolved, and why.

    `first_seen` exists so we can ask "what existed on date D", not only "what
    resolved". A backtest built from resolved markets alone systematically
    excludes the ones that got cancelled or that we stopped watching — which
    flatters results in a way no amount of walk-forward will catch.
    """
    row = con.execute("""
        SELECT
          SUM(CASE WHEN result IS NULL AND (close_time IS NULL
                OR close_time >= strftime('%s','now')) THEN 1 ELSE 0 END) AS still_open,
          SUM(CASE WHEN result IS NULL AND close_time < strftime('%s','now')
                THEN 1 ELSE 0 END) AS closed_no_result,
          COUNT(*) AS total
        FROM markets""").fetchone()
    seen_never_resolved = (row["still_open"] or 0) + (row["closed_no_result"] or 0)
    return {
        "seen_never_resolved": seen_never_resolved,
        "still_open": row["still_open"] or 0,
        "closed_without_result": row["closed_no_result"] or 0,
        "pct_of_all_markets": round(seen_never_resolved / row["total"], 4)
                              if row["total"] else None,
    }


def by_series(con):
    """Per-series breakdown, so a thin league is visible rather than averaged
    away by a well-covered one."""
    rows = con.execute("""
        SELECT m.series,
               COUNT(DISTINCT m.ticker) AS markets,
               COUNT(c.ts)              AS candles,
               SUM(CASE WHEN m.result IN ('yes','no','void') THEN 1 ELSE 0 END) AS resolved
        FROM markets m LEFT JOIN candles c ON c.ticker = m.ticker
        GROUP BY m.series ORDER BY candles DESC""").fetchall()
    return [{"series": r["series"], "markets": r["markets"],
             "candles": r["candles"], "resolved_markets": r["resolved"]}
            for r in rows]


def report(con, expected_interval=300):
    """The full report. Never raises."""
    r = {
        "coverage": coverage(con),
        "settlement": settlement_coverage(con),
        "price_sanity": price_sanity(con),
        "clock_sanity": clock_sanity(con),
        "gaps": gaps(con, expected_interval=expected_interval),
        "survivorship": survivorship(con),
        "by_series": by_series(con),
    }
    r["blocking"] = blocking_issues(r)
    return r


def blocking_issues(r):
    """Problems that make a backtest on this data untrustworthy, as opposed to
    merely incomplete. Incompleteness is expected and fine; these are not."""
    out = []
    if r["price_sanity"]["crossed_books"]:
        out.append(f"{r['price_sanity']['crossed_books']} crossed books "
                   f"(bid > ask) — field mapping is wrong, not the market")
    if r["price_sanity"]["out_of_range_prices"]:
        out.append(f"{r['price_sanity']['out_of_range_prices']} prices outside "
                   f"0-100 cents")
    if r["clock_sanity"]["candles_after_close_time"]:
        out.append(f"{r['clock_sanity']['candles_after_close_time']} candles "
                   f"dated after their market closed — possible outcome leak")
    if r["clock_sanity"]["orphan_candles_no_market_row"]:
        out.append(f"{r['clock_sanity']['orphan_candles_no_market_row']} orphan "
                   f"candles with no market row — silently dropped by load_bars")
    rate = r["settlement"]["settlement_rate"]
    if rate is not None and rate < 0.90:
        out.append(f"settlement coverage {rate:.0%} — outcomes are missing, so "
                   f"PnL and calibration will be computed on a biased subset")
    if r["coverage"]["candles"] == 0:
        out.append("store is empty — the collector has never written a row")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Report on history quality.")
    ap.add_argument("--db", default=data.DB_PATH)
    ap.add_argument("--interval", type=int, default=300,
                    help="expected seconds between snapshots, for gap detection")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args(argv)

    con = data.connect(args.db)
    r = report(con, expected_interval=args.interval)

    if args.json:
        print(json.dumps(r, indent=2))
        return 0

    c, s = r["coverage"], r["settlement"]
    print(f"\n  store    {c['candles']:,} candles / {c['markets']:,} markets")
    print(f"  span     {c['first_candle']} -> {c['last_candle']} "
          f"({c['span_days']} days)")
    rate = s["settlement_rate"]
    print(f"  settled  {s['resolved']:,} resolved, {s.get('voided', 0):,} voided, "
          f"{s['past_close_but_unresolved']:,} past close awaiting result"
          + (f" ({rate:.1%} coverage)" if rate is not None else ""))
    print(f"  gaps     {r['gaps']['markets_with_gaps']} markets, "
          f"{r['gaps']['total_missing_snapshots']:,} missing snapshots")
    print(f"  books    {r['price_sanity']['empty_book_snapshots']:,} empty, "
          f"{r['price_sanity']['crossed_books']} crossed")

    if r["by_series"]:
        print("\n  by series:")
        for row in r["by_series"][:12]:
            print(f"    {row['series']:<28} {row['candles']:>8,} candles  "
                  f"{row['markets']:>4} markets  {row['resolved_markets']:>4} resolved")

    if r["blocking"]:
        print("\n  BLOCKING — do not trust a backtest on this data:")
        for b in r["blocking"]:
            print(f"    - {b}")
    else:
        print("\n  no blocking issues")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
