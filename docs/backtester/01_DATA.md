# Phase 1 — Data layer

*Prerequisite for everything else. No engine can be trusted on data we haven't
verified.*

---

## 1. What Kalshi gives us

| Source | Endpoint | Contents | Use |
|---|---|---|---|
| Candlesticks | `GET /series/{s}/markets/{t}/candlesticks` | `end_period_ts`, `yes_bid`, `yes_ask`, `price` (open/high/low/close/mean/previous), `volume`, `open_interest` | Primary backtest input |
| Trades | trades endpoint | Individual prints — price, size, taker side, ts | Microstructure, whale/flow signals |
| Archived markets | `/historical/*` | Same shape, for markets past the live cutoff | Depth of history |
| Market metadata | markets endpoint | `close_time`, `status`, `result`, series, title | Resolution truth + `days_to_resolution` |

**What Kalshi does not give us: L2 order-book history.** Only vendors sell that
(LycheeData, DepthFeed, Allium). This is the single biggest limit on fill
realism — see §5.

Rate limits are tiered and token-costed per endpoint; `GET /account/api-limits`
reports the current tier. The fetcher must respect them, not discover them.

---

## 2. Store

SQLite, one file, in `data/market_history.db`. Not Postgres: single writer,
single machine, and it must be trivially copyable off the droplet.

```sql
CREATE TABLE candles (
  ticker        TEXT NOT NULL,
  ts            INTEGER NOT NULL,      -- end_period_ts, UTC epoch seconds
  yes_bid       INTEGER,               -- cents
  yes_ask       INTEGER,               -- cents
  open          INTEGER, high INTEGER, low INTEGER, close INTEGER,
  volume        INTEGER,
  open_interest INTEGER,
  PRIMARY KEY (ticker, ts)
);

CREATE TABLE markets (
  ticker      TEXT PRIMARY KEY,
  series      TEXT NOT NULL,
  title       TEXT,
  close_time  INTEGER,                 -- UTC epoch seconds
  status      TEXT,                    -- active | closed | settled
  result      TEXT,                    -- yes | no | NULL while open
  first_seen  INTEGER NOT NULL,        -- guards against survivorship bias
  last_seen   INTEGER NOT NULL
);

CREATE TABLE trades (
  trade_id   TEXT PRIMARY KEY,
  ticker     TEXT NOT NULL,
  ts         INTEGER NOT NULL,
  price      INTEGER,                  -- cents
  count      INTEGER,
  taker_side TEXT
);

CREATE INDEX idx_candles_ts  ON candles(ts);
CREATE INDEX idx_trades_tick ON trades(ticker, ts);
CREATE INDEX idx_markets_close ON markets(close_time);
```

**All prices are integer cents. All timestamps are UTC epoch seconds.** No
floats for money, no local time, anywhere. Float cents and naive datetimes are
the two classic sources of silent backtest error.

`first_seen` exists specifically to defeat survivorship bias: we must be able to
ask "what markets existed on date D", not just "what markets resolved".

---

## 3. Two ingest paths

**Backfill** (`wc/backtest/data.py`) — pull history that already exists.
Idempotent: re-running must never duplicate or corrupt rows (`INSERT OR REPLACE`
on the primary key). Resumable: it records progress per ticker so an interrupted
run continues rather than restarts.

**Forward collector** (`wc/backtest/collect.py`) — cron on the droplet, snapshots
open markets on an interval. This is what accumulates the data Kalshi will not
sell us.

> **Start the collector in week 1, before the engine exists.** Every day it is
> not running is a day of history we can never recover. This is the single
> highest-value/lowest-effort item in the whole plan.

---

## 4. Quality checks

A backtest on bad data is worse than no backtest — it produces a confident wrong
number. `wc/backtest/quality.py` must run before any result is believed, and
report:

- **Gap detection** — missing candles inside a market's active window
- **Settlement coverage** — % of closed markets with a known `result`
- **Price sanity** — `0 <= bid <= ask <= 100`; flag crossed or absurd books
- **Volume sanity** — nonzero volume on markets we claim were tradeable
- **Survivorship** — count markets seen but never resolved, and why
- **Clock sanity** — candles after `close_time`, or before `first_seen`

Output is a report, not an exception. We want to know the data is 92% complete
and where the 8% is, not to have the run abort.

---

## 5. The fill-realism problem

Candlesticks tell us a market traded between `low` and `high` in a period. They
do **not** tell us there was size at our price when we wanted it.

Phase 2 therefore assumes:

- We can only take, never make (pessimistic, and simpler)
- Fills happen at `yes_ask` for buys and `yes_bid` for sells — never at `close`
- A configurable slippage term on top
- Volume-capped: never fill more than a set fraction of the period's volume

Every one of these is optimistic in *some* regime, so backtest PnL must always
be reported as an upper bound, and paper trading is the real check. This is
written into the metrics output, not left as folklore.

---

## 6. Definition of done

- [ ] Backfill pulls a known market's full history and matches the Kalshi UI
      — **not built.** Deferred deliberately: archived history will still be
      there next month, whereas un-collected forward history is gone forever
- [x] Re-running an ingest changes zero rows (idempotent) — snapshot timestamps
      are floored to the interval, so even a double-fired cron overwrites
- [ ] Collector runs on the droplet under cron and survives reboot
      — built and verified locally; cron not yet installed (see DEPLOY.md)
- [x] Quality report, with gaps explained — `wc/backtest/quality.py`
- [x] `days_to_resolution` derivable for every candle — `close_time` stored as
      epoch seconds, computed in `engine.BarView.days_to_resolution`
- [ ] Documented answer to: how far back does the archive actually go?
      — open, and only relevant once backfill is written

### Measured, 2026-09-07

Kalshi lists **~1400 soccer series** (tag-discovered) and **61,000+ open markets
overall**, so neither "probe every soccer series" nor "sweep all open markets"
fits in a cron interval. The collector narrows by suffix instead:
`config/collector.json` defaults to `GAME` (match winner) — **139 series, ~600
open markets, ~50s per cycle**. Widening to game_lines is ~1000 series and
~6 min, which needs `interval_seconds` raised to 900 first.
