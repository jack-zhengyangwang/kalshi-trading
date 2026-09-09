"""Historical backfill: pull Kalshi's candlestick archive into the store.

This is the BACKTEST's data source. It answers "would this strategy have made
money", using true OHLC bars for markets that have already settled.

It is deliberately separate from the forward collector, which answers a
different question — "does this strategy make money, prospectively" — and whose
snapshots belong to forward testing. The two write different tables and a
backtest reads exactly one of them. See docs/backtester/01_DATA.md.

WHAT KALSHI GIVES US (measured 2026-09-09):
  • 1-minute candles, each carrying separate OHLC for yes_bid and yes_ask,
    plus volume and open interest — better resolution than the collector
  • coverage back to a market's creation
  • roughly 8,000 settled soccer markets listed, oldest close 2026-07-04

THE LIMIT, WHICH IS STRUCTURAL: backfill only sees markets Kalshi still lists.
It cannot answer "what existed on date D" — only "what still exists and closed
after D". Markets that were pruned, or that never resolved, are invisible, so
backfilled data carries survivorship bias by construction. The collector's
`first_seen` is what defeats that, which is why both exist.

Usage:
    python3 -m wc.backtest.backfill --days 60
    python3 -m wc.backtest.backfill --series KXEPLGAME --interval 1
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from wc.backtest import collect, data

# Kalshi rejects a request spanning too many candles, so each interval gets a
# window that stays comfortably under the limit. Measured: 2,719 one-minute
# candles came back fine over 3 days; the 400s were window size, not missing
# data.
CHUNK_DAYS = {1: 1, 60: 60, 1440: 1000}
DEFAULT_INTERVAL = 60


def _cents(block, field="close_dollars"):
    """Kalshi sends money as dollar STRINGS. Integer cents, always."""
    if not isinstance(block, dict):
        return None
    raw = block.get(field)
    if raw in (None, ""):
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def _int(raw):
    if raw in (None, ""):
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def parse_candle(c):
    """One archive candle -> a store row.

    `yes_bid`/`yes_ask` take the period CLOSE, matching what the collector
    records and what the engine fills against. The price OHLC comes from the
    trade block when there were trades, and is left NULL when there were none —
    a period with no trades has no open/high/low, and inventing one would give
    the engine a price to trade against that nobody ever paid.
    """
    price = c.get("price") or {}
    return {
        "ts": int(c["end_period_ts"]),
        "yes_bid": _cents(c.get("yes_bid")),
        "yes_ask": _cents(c.get("yes_ask")),
        "open": _cents(price, "open_dollars"),
        "high": _cents(price, "high_dollars"),
        "low": _cents(price, "low_dollars"),
        "close": _cents(price, "close_dollars"),
        "volume": _int(c.get("volume_fp")),
        "open_interest": _int(c.get("open_interest_fp")),
    }


def fetch_candles(client, series, ticker, start_ts, end_ts, interval_min):
    """Every candle for one market, in windows the API will accept."""
    chunk = CHUNK_DAYS.get(interval_min, 1) * 86400
    out, lo = [], int(start_ts)
    end_ts = int(end_ts)
    while lo < end_ts:
        hi = min(lo + chunk, end_ts)
        try:
            resp = client._get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                params={"start_ts": lo, "end_ts": hi,
                        "period_interval": interval_min})
            out.extend(resp.get("candlesticks") or [])
        except Exception as e:
            # One bad window must not lose the rest of the market's history.
            print(f"[backfill] {ticker} {lo}-{hi}: {e}", file=sys.stderr)
        lo = hi
    return out


def market_window(market, days):
    """(start_ts, end_ts) to pull for a market: its life, clipped to `days`."""
    close = collect.parse_close_time(market.get("close_time"))
    open_ts = collect.parse_close_time(market.get("open_time"))
    if close is None:
        return None, None
    start = open_ts or (close - days * 86400)
    return max(start, close - days * 86400), close


def backfill_series(con, client, series, days=60, interval_min=DEFAULT_INTERVAL,
                    limit=None, dry_run=False):
    """Pull every settled market in one series. Returns a small report."""
    try:
        markets = client.list_markets_by_series(series, status="settled")
    except Exception as e:
        return {"series": series, "error": str(e), "markets": 0, "candles": 0}

    cutoff = time.time() - days * 86400
    markets = [m for m in markets
               if (collect.parse_close_time(m.get("close_time")) or 0) >= cutoff]
    if limit:
        markets = markets[:limit]

    n_candles = n_markets = 0
    for m in markets:
        ticker = m.get("ticker")
        if not ticker:
            continue

        # The market row carries the outcome, without which the candles are
        # unusable: no settlement means no P&L, no Brier, no calibration.
        result, value = collect.settlement_outcome(m)
        home, away = collect.parse_fixture(m)
        if not dry_run:
            data.upsert_market(con, {
                "ticker": ticker, "series": series, "title": m.get("title"),
                "sub_title": m.get("yes_sub_title"),
                "event_ticker": m.get("event_ticker"), "home": home, "away": away,
                "close_time": collect.parse_close_time(m.get("close_time")),
                "status": m.get("status"), "result": result,
            })
            if result is not None:
                data.set_result(con, ticker, m.get("status"), result, value)

        start, end = market_window(m, days)
        if start is None:
            continue
        raw = fetch_candles(client, series, ticker, start, end, interval_min)
        rows = [parse_candle(c) for c in raw if c.get("end_period_ts")]
        if rows and not dry_run:
            n_candles += data.upsert_backfill_candles(con, ticker, interval_min, rows)
        elif rows:
            n_candles += len(rows)
        n_markets += 1

    return {"series": series, "markets": n_markets, "candles": n_candles}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backfill Kalshi's candle archive.")
    ap.add_argument("--db", default=data.DB_PATH)
    ap.add_argument("--days", type=int, default=60,
                    help="how far back to pull (default 60)")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    choices=sorted(CHUNK_DAYS),
                    help="candle resolution in minutes. 60 covers a whole "
                         "market in one request; 1 is ~60x the requests")
    ap.add_argument("--series", nargs="*",
                    help="series to pull (default: the collector's universe)")
    ap.add_argument("--max-series", type=int)
    ap.add_argument("--max-markets", type=int,
                    help="cap markets per series (for a quick probe)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from wc.kalshi.client_ext import KalshiClientV2
    client = KalshiClientV2()

    series_list = args.series
    if not series_list:
        cfg = collect.load_config()
        series_list = collect.soccer_series(client, suffixes=cfg.get("suffixes"))
    if args.max_series:
        series_list = series_list[:args.max_series]

    con = data.connect(args.db)
    t0 = time.time()
    total_m = total_c = 0
    for i, s in enumerate(series_list, 1):
        rep = backfill_series(con, client, s, days=args.days,
                              interval_min=args.interval,
                              limit=args.max_markets, dry_run=args.dry_run)
        total_m += rep["markets"]
        total_c += rep["candles"]
        if rep.get("error"):
            print(f"  [{i}/{len(series_list)}] {s}: {rep['error'][:80]}",
                  file=sys.stderr)
        elif rep["markets"]:
            print(f"  [{i}/{len(series_list)}] {s:<26} "
                  f"{rep['markets']:>4} markets  {rep['candles']:>7,} candles",
                  flush=True)

    print(f"\n  backfilled {total_m:,} markets / {total_c:,} candles "
          f"at {args.interval}m in {time.time() - t0:.0f}s"
          + ("  [DRY RUN]" if args.dry_run else ""))
    if not args.dry_run:
        print(f"  store now holds "
              f"{data.backfill_candle_count(con, args.interval):,} candles "
              f"at {args.interval}m\n")
        print(f"  next: python3 -m wc.backtest.run <spec> "
              f"--interval-min {args.interval}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
