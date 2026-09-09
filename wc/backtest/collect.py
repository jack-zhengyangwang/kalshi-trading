"""Forward collector: snapshot open soccer markets on a cron interval.

This is the piece that accrues history Kalshi will not sell us. Every cycle it
misses is a gap that can never be backfilled, which is why it is deliberately
the dumbest component in the backtester: one pass, no cleverness, and no failure
mode that stops the next cycle from running.

WHY SOCCER: the universe is soccer because that is where our own forecasting
edge lives — the brains, the Elo index, the goals model. It is a MARKET
SELECTION, not a commitment to any one strategy. The engine that replays this
data stays market-agnostic, and the schema below is market-agnostic too, so
widening the universe later is a config change, not a rewrite.

WHAT A ROW IS: a snapshot, not an OHLC candle. Kalshi's `/markets` payload gives
top-of-book at the instant we ask. We store mid as `close` and leave
open/high/low NULL rather than fabricating a range we did not observe. The
engine reads bid/ask and falls back to close, so this is the honest shape.

Usage:
    python3 -m wc.backtest.collect --once
    python3 -m wc.backtest.collect --once --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import re
import time

from wc import paths
from wc.backtest import data

SETTLED_STATUSES = ("settled", "finalized")

# Kalshi reports a VOIDED market as result='scalar' with a settlement value
# strictly between 0 and 1 — the game was cancelled or postponed, so each leg
# pays out a fair price instead of resolving. About 13% of settled soccer
# markets land here, so it is a third outcome to record, not noise to skip.
VOID_RESULT = "scalar"


def settlement_outcome(market):
    """('yes'|'no'|'void'|None, settlement_value_dollars|None) for a market.

    Returns (None, None) when the market has not finished, so the caller leaves
    it in the sweep queue.
    """
    if market.get("status") not in SETTLED_STATUSES:
        return None, None

    raw = market.get("settlement_value_dollars")
    try:
        value = float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        value = None

    result = (market.get("result") or "").lower()
    if result in ("yes", "no"):
        return result, (1.0 if result == "yes" else 0.0) if value is None else value
    if result == VOID_RESULT:
        # A void with no value is not usable — recording it as 'void' with an
        # unknown payout would silently invent a P&L of zero.
        return ("void", value) if value is not None else (None, None)
    return None, None
LOG_PATH = os.path.join(paths.LOGS_DIR, "collect.jsonl")
CONFIG_PATH = os.path.join(paths.CONFIG_DIR, "collector.json")

# Snapshot timestamps are quantised to this bucket so that a double-fired cron
# — a real, previously observed failure on this droplet — overwrites the same
# row instead of writing two rows seconds apart and doubling the apparent bar
# count. Idempotence has to survive the scheduler misbehaving, not just us.
DEFAULT_BUCKET = 300

# Kalshi does not expose home/away as fields. The fixture appears only in the
# rules prose ("...refers to the Fulham vs Manchester United professional EPL
# soccer game..."), so it is extracted here, once, and stored — the rules text
# itself is far too large to keep per market and disappears when a market closes.
FIXTURE_RE = re.compile(r"(?:refers to|result of) the (.+?) vs (.+?) "
                        r"(?:professional|game|soccer)", re.I)


def parse_fixture(market):
    """(home, away) from a market's rules text, or (None, None)."""
    text = f"{market.get('rules_secondary') or ''} {market.get('rules_primary') or ''}"
    mo = FIXTURE_RE.search(text)
    if not mo:
        return None, None
    home, away = mo.group(1).strip(), mo.group(2).strip()
    # Guard against a runaway match swallowing a sentence.
    if len(home) > 60 or len(away) > 60:
        return None, None
    return home, away


def bucket_ts(now, bucket=DEFAULT_BUCKET):
    """Floor a timestamp to the collection interval."""
    return int(now) - (int(now) % int(bucket))


def parse_close_time(value):
    """Kalshi returns ISO-8601; the store keeps UTC epoch seconds. Returns None
    rather than raising — a market with an unparseable close_time is still worth
    recording, it just cannot contribute days_to_resolution."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def snapshot_rows(client, market, ts, series=None):
    """(market_row, candle_row) for one Kalshi market dict.

    Field extraction goes through the client's own helpers: the batch payload's
    `volume` is null and the real values live in the *_fp fields, which is
    exactly the sort of detail a second implementation gets wrong.
    """
    bid, ask = client.quote_cents(market)
    liq = client.liquidity(market)

    mid = None
    if bid is not None and ask is not None:
        mid = int(round((bid + ask) / 2))

    home, away = parse_fixture(market)
    market_row = {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "home": home,
        "away": away,
        "series": series or market.get("series_ticker") \
                  or market["ticker"].split("-")[0],
        "title": market.get("title"),
        # The leg descriptor ("Arsenal", "Over 2.5", "Tie"). The brains cannot
        # classify — and therefore cannot price — a market without it, so it is
        # stored in its own column rather than folded into title.
        "sub_title": market.get("yes_sub_title"),
        "close_time": parse_close_time(market.get("close_time")),
        "status": market.get("status"),
        "result": (market.get("result") or "").lower() or None,
    }
    candle_row = {
        "ts": ts,
        "yes_bid": bid,
        "yes_ask": ask,
        "open": None, "high": None, "low": None,      # a snapshot has no range
        "close": mid,
        "volume": int(liq["volume"]),
        "open_interest": int(liq["oi"]),
    }
    return market_row, candle_row


def collect_series(con, client, series, ts, dry_run=False):
    """Snapshot every open market in one series. Returns (markets, candles)."""
    markets = client.list_markets_by_series(series, status="open")
    n_candles = 0
    for m in markets:
        if not m.get("ticker"):
            continue
        # The series we asked for is authoritative: Kalshi's market payload
        # leaves `series_ticker` null on some endpoints, and inferring it from
        # the ticker prefix would quietly mislabel any series whose naming
        # breaks the pattern.
        market_row, candle_row = snapshot_rows(client, m, ts, series=series)
        if dry_run:
            n_candles += 1
            continue
        data.upsert_market(con, market_row, now=ts)
        n_candles += data.upsert_candles(con, m["ticker"], [candle_row])
    return len(markets), n_candles


def settle_open_markets(con, client, now, batch=200, max_batches=10,
                        dry_run=False):
    """Write the outcome for markets that have resolved since the last cycle.

    Without this the store is price history with no truth attached, and PnL,
    Brier, and calibration are all uncomputable. It is the half of collection
    that is easy to forget and fatal to omit, so it runs every cycle rather than
    on a separate schedule that can silently stop.

    Bounded twice, because this set only ever grows:
      - only markets past close_time are asked about (a market cannot settle
        before it closes, so anything else is guaranteed-useless API spend)
      - at most `max_batches` per cycle, oldest first, so a backlog drains over
        several cycles instead of starving the snapshots that cannot wait
    """
    tickers = [r["ticker"] for r in
               data.unresolved_markets(con, before_ts=now, limit=batch * max_batches)]
    if not tickers:
        return 0

    settled = 0
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            fetched = client.list_markets_by_tickers(chunk)
        except Exception as e:                        # one bad batch != lost cycle
            print(f"[collect] settle batch failed: {e}", file=sys.stderr)
            continue
        for m in fetched:
            result, value = settlement_outcome(m)
            if result is None:
                continue
            settled += 1
            if not dry_run:
                data.set_result(con, m["ticker"], m.get("status"), result, value)
    return settled


def run_once(con, client, series_list, now=None, bucket=DEFAULT_BUCKET,
             dry_run=False):
    """One collection cycle. Never raises on a single series' failure — a cycle
    that dies partway is a permanent hole in the history."""
    now = time.time() if now is None else now
    ts = bucket_ts(now, bucket)

    total_markets = total_candles = 0
    failures = []
    for series in series_list:
        try:
            n_m, n_c = collect_series(con, client, series, ts, dry_run=dry_run)
            total_markets += n_m
            total_candles += n_c
        except Exception as e:
            failures.append({"series": series, "error": str(e)})
            print(f"[collect] {series} failed: {e}", file=sys.stderr)

    settled = settle_open_markets(con, client, now=ts, dry_run=dry_run)

    return {
        "ts": ts,
        "iso": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(),
        "series_watched": len(series_list),
        "markets_seen": total_markets,
        "candles_written": total_candles,
        "newly_settled": settled,
        "failures": failures,
        "dry_run": dry_run,
    }


def load_config(path=CONFIG_PATH):
    """Universe config. Experiments are config, not new files."""
    with open(path) as f:
        return json.load(f)


def soccer_series(client, suffixes=None, refresh=False, ttl=3600):
    """The universe: soccer series Kalshi currently lists, narrowed by suffix.

    Discovery is the live scanner's own tag-based lookup rather than a hardcoded
    menu, so the collector and the trading path can never drift onto different
    market sets — and a new league starts being recorded the day Kalshi opens
    it.

    The suffix narrowing is not optional in practice: Kalshi lists ~1400 soccer
    series, and probing all of them takes longer than the cron interval, which
    would mean cycles overlapping and stacking up forever.
    """
    from wc.scanner import discover_soccer_series
    found = discover_soccer_series(client, refresh=refresh, ttl=ttl)
    if not suffixes:
        return sorted(found)
    return sorted(s for s in found if any(s.endswith(x) for x in suffixes))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Snapshot open soccer markets.")
    ap.add_argument("--once", action="store_true",
                    help="run a single cycle (the cron mode; currently the only mode)")
    ap.add_argument("--db", default=data.DB_PATH)
    ap.add_argument("--bucket", type=int, default=None,
                    help="quantise snapshot timestamps to this many seconds "
                         "(default: interval_seconds from config/collector.json)")
    ap.add_argument("--series", nargs="*",
                    help="override the discovered soccer series (testing)")
    ap.add_argument("--suffixes", nargs="*",
                    help="override config/collector.json suffixes")
    ap.add_argument("--refresh-series", action="store_true",
                    help="bypass the discovery cache")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    args = ap.parse_args(argv)

    cfg = load_config()
    bucket = args.bucket or cfg.get("interval_seconds", DEFAULT_BUCKET)

    from wc.kalshi.client_ext import KalshiClientV2
    client = KalshiClientV2()

    series_list = args.series or soccer_series(
        client,
        suffixes=args.suffixes or cfg.get("suffixes"),
        refresh=args.refresh_series,
        ttl=cfg.get("series_cache_ttl", 3600))
    if not series_list:
        print("[collect] no soccer series discovered — nothing to do",
              file=sys.stderr)
        return 1

    con = data.connect(args.db)
    report = run_once(con, client, series_list, bucket=bucket,
                      dry_run=args.dry_run)
    report["db"] = args.db
    report["candles_total"] = data.candle_count(con)
    report["markets_total"] = data.market_count(con)

    if not args.dry_run:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(report) + "\n")

    print(f"[collect] {report['iso']} | series {report['series_watched']} "
          f"| markets {report['markets_seen']} | candles +{report['candles_written']} "
          f"| settled +{report['newly_settled']} "
          f"| store {report['candles_total']:,} candles / "
          f"{report['markets_total']:,} markets"
          + (" | DRY RUN" if args.dry_run else ""))
    for f_ in report["failures"]:
        print(f"[collect] FAILED {f_['series']}: {f_['error']}", file=sys.stderr)

    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
