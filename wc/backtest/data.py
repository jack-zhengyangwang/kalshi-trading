"""Historical market data: SQLite store, backfill, and quality checks.

All prices are INTEGER CENTS. All timestamps are UTC EPOCH SECONDS. Float cents
and naive datetimes are the two classic sources of silent backtest error, so
neither appears in the schema.

See docs/backtester/01_DATA.md.
"""
from __future__ import annotations

import os
import sqlite3
import time

from wc import paths

DB_PATH = os.path.join(paths.ROOT, "data", "market_history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  ticker        TEXT    NOT NULL,
  ts            INTEGER NOT NULL,
  yes_bid       INTEGER,
  yes_ask       INTEGER,
  open          INTEGER,
  high          INTEGER,
  low           INTEGER,
  close         INTEGER,
  volume        INTEGER,
  open_interest INTEGER,
  PRIMARY KEY (ticker, ts)
);

CREATE TABLE IF NOT EXISTS markets (
  ticker     TEXT PRIMARY KEY,
  series     TEXT NOT NULL,
  title      TEXT,
  -- Kalshi's yes_sub_title ("Arsenal", "Over 2.5", "Tie"). Kept separate from
  -- title because markets.parse_market_v2 needs it to classify a leg, and the
  -- brains cannot price a market they cannot classify.
  sub_title  TEXT,
  -- The fixture, resolved ONCE at collection time. Kalshi's market payload does
  -- not carry home/away as fields — the only reliable source is the prose in
  -- rules_secondary — and re-deriving that at backtest time would mean either
  -- storing the full rules text forever or losing the fixture the moment the
  -- market closes. The brains cannot price a game whose teams we do not know.
  event_ticker TEXT,
  home       TEXT,
  away       TEXT,
  close_time INTEGER,
  status     TEXT,
  -- 'yes' | 'no' | 'void' | NULL-while-open.
  --
  -- VOID is a real, common third outcome, not an edge case: ~13% of settled
  -- soccer markets are games that were cancelled or postponed, and Kalshi
  -- settles every leg at a fair price summing to 1.00 rather than 0/1. It
  -- reports those as result='scalar' with a settlement_value_dollars between
  -- 0 and 1. Treating them as a loss would be wrong (you get most of your
  -- stake back); treating them as unresolved would re-sweep them forever.
  result     TEXT,
  settlement_value REAL,          -- dollars per contract: 1.0 yes, 0.0 no, else void
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
  trade_id   TEXT PRIMARY KEY,
  ticker     TEXT NOT NULL,
  ts         INTEGER NOT NULL,
  price      INTEGER,
  count      INTEGER,
  taker_side TEXT
);

-- Historical candlesticks pulled from Kalshi's archive. A SEPARATE TABLE from
-- `candles` on purpose: these are true OHLC bars at 1-60 minute resolution,
-- while `candles` holds point-in-time snapshots at the collector's interval.
-- Mixing them in one table would let a backtest silently report a single P&L
-- over two different kinds of measurement, and the reader would never know.
--
-- They also answer different questions. Backfill sees only markets Kalshi
-- still lists, so it carries survivorship bias by construction; the collector's
-- first_seen is what defeats that. Neither replaces the other.
CREATE TABLE IF NOT EXISTS backfill_candles (
  ticker        TEXT    NOT NULL,
  ts            INTEGER NOT NULL,      -- end_period_ts, UTC epoch seconds
  interval_min  INTEGER NOT NULL,      -- 1 | 60 | 1440
  yes_bid       INTEGER,               -- cents, period close
  yes_ask       INTEGER,
  open          INTEGER, high INTEGER, low INTEGER, close INTEGER,
  volume        INTEGER,
  open_interest INTEGER,
  PRIMARY KEY (ticker, ts, interval_min)
);

CREATE INDEX IF NOT EXISTS idx_backfill_ts ON backfill_candles(ts);

-- backfill progress, so an interrupted run resumes instead of restarting
CREATE TABLE IF NOT EXISTS ingest_state (
  ticker      TEXT PRIMARY KEY,
  last_ts     INTEGER,
  updated_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_candles_ts    ON candles(ts);
CREATE INDEX IF NOT EXISTS idx_trades_tick   ON trades(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_markets_close ON markets(close_time);
CREATE INDEX IF NOT EXISTS idx_markets_series ON markets(series);
"""


def connect(path=DB_PATH):
    """Open (creating if needed) the history DB with the schema applied.

    WAL, because the collector writes every five minutes while a backtest may
    be reading the same file. Under the default `delete` journal a reader holds
    a lock the writer cannot take, and the collector's cycle dies with
    "database is locked" — losing forward history, the one thing here that
    cannot be re-fetched. WAL is a property of the file, so setting it once
    per connection is idempotent and survives a copy of the DB.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """Additive migrations for DBs created by an earlier schema.

    The history DB is the one artefact here that cannot be regenerated, so
    migrations only ever ADD columns — never drop, rename, or rewrite. A store
    collected under an older schema stays readable.
    """
    have = {r["name"] for r in con.execute("PRAGMA table_info(markets)")}
    for col in ("sub_title", "event_ticker", "home", "away"):
        if col not in have:
            con.execute(f"ALTER TABLE markets ADD COLUMN {col} TEXT")
    if "settlement_value" not in have:
        con.execute("ALTER TABLE markets ADD COLUMN settlement_value REAL")
    con.commit()


# ── writes (idempotent) ───────────────────────────────────────────────────────

def upsert_candles(con, ticker, rows):
    """Insert candles. Idempotent on (ticker, ts) — re-running a backfill must
    never duplicate or corrupt rows. Returns the number of rows written."""
    payload = [(ticker, int(r["ts"]),
                r.get("yes_bid"), r.get("yes_ask"),
                r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                r.get("volume"), r.get("open_interest"))
               for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO candles "
        "(ticker, ts, yes_bid, yes_ask, open, high, low, close, volume, open_interest) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", payload)
    con.commit()
    return len(payload)


def upsert_market(con, m, now=None):
    """Record a market. `first_seen` is preserved across updates — it is what
    lets us ask 'what existed on date D', defeating survivorship bias."""
    now = int(now if now is not None else time.time())
    con.execute("""
        INSERT INTO markets (ticker, series, title, sub_title, event_ticker,
                             home, away, close_time, status, result,
                             first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            title      = excluded.title,
            sub_title  = COALESCE(excluded.sub_title, markets.sub_title),
            event_ticker = COALESCE(excluded.event_ticker, markets.event_ticker),
            home       = COALESCE(excluded.home, markets.home),
            away       = COALESCE(excluded.away, markets.away),
            close_time = excluded.close_time,
            status     = excluded.status,
            -- COALESCE, not overwrite: a settlement outcome is the one fact in
            -- this store that cannot be recomputed later. An open-market
            -- listing carries result=NULL, so a plain assignment here would let
            -- any routine re-listing erase truth we already recorded, silently
            -- and undetectably. Once known, a result is permanent.
            result     = COALESCE(excluded.result, markets.result),
            last_seen  = excluded.last_seen
    """, (m["ticker"], m.get("series") or "", m.get("title"), m.get("sub_title"),
          m.get("event_ticker"), m.get("home"), m.get("away"),
          m.get("close_time"), m.get("status"), m.get("result"), now, now))
    con.commit()


def upsert_trades(con, rows):
    con.executemany(
        "INSERT OR REPLACE INTO trades (trade_id, ticker, ts, price, count, taker_side) "
        "VALUES (?,?,?,?,?,?)",
        [(r["trade_id"], r["ticker"], int(r["ts"]), r.get("price"),
          r.get("count"), r.get("taker_side")) for r in rows])
    con.commit()
    return len(rows)


def set_result(con, ticker, status, result, settlement_value=None):
    """Attach the outcome to a market once Kalshi has settled it.

    Separate from upsert_market because the settlement sweep re-fetches ONLY
    unresolved tickers — a market's result never changes once written, so
    re-reading settled markets forever would be pure API waste.

    `result` is 'yes', 'no', or 'void'. A void still gets written, precisely so
    it stops being re-swept; `settlement_value` carries what it actually paid.
    """
    con.execute("UPDATE markets SET status = ?, result = ?, "
                "settlement_value = ? WHERE ticker = ?",
                (status, result, settlement_value, ticker))
    con.commit()


def unresolved_markets(con, before_ts=None, limit=None):
    """Markets with no outcome yet — the settlement sweep's work list.

    `before_ts` restricts to markets whose close_time has already passed, and
    it is what keeps the sweep affordable. A market cannot settle before it
    closes, so re-asking about still-open markets is guaranteed-useless API
    spend — and the unresolved set grows every cycle the collector runs, so an
    unbounded sweep would consume more of the rate limit each day until it
    crowded out the snapshots themselves.

    NULL close_times are always included: we cannot prove they haven't settled.

    Ordered by close_time so the longest-closed are checked first.
    """
    q = ["SELECT ticker, close_time FROM markets WHERE result IS NULL"]
    args = []
    if before_ts is not None:
        q.append("AND (close_time IS NULL OR close_time <= ?)")
        args.append(int(before_ts))
    q.append("ORDER BY close_time IS NULL, close_time ASC")
    if limit:
        q.append(f"LIMIT {int(limit)}")
    return con.execute(" ".join(q), args).fetchall()


def set_progress(con, ticker, last_ts):
    con.execute("INSERT OR REPLACE INTO ingest_state (ticker, last_ts, updated_at) "
                "VALUES (?,?,?)", (ticker, int(last_ts), int(time.time())))
    con.commit()


def get_progress(con, ticker):
    row = con.execute("SELECT last_ts FROM ingest_state WHERE ticker = ?",
                      (ticker,)).fetchone()
    return row["last_ts"] if row else None


# ── reads ─────────────────────────────────────────────────────────────────────

SOURCES = {"collector": "candles", "backfill": "backfill_candles"}


def load_bars(con, start_ts=None, end_ts=None, series=None, tickers=None,
              source="backfill", interval_min=None):
    """Every candle in the window, joined to its market, in CHRONOLOGICAL order
    across all markets.

    The ordering is load-bearing: the engine must see markets interleaved in
    time, or portfolio-level caps (daily spend, total exposure, concurrent
    positions) can never bind, because it would never hold two markets at once.

    `source` picks ONE table and never unions them. 'backfill' is true OHLC from
    Kalshi's archive and is what a backtest should read; 'collector' is the
    forward snapshot record, which belongs to forward testing. A run that mixed
    them would report one number over two different kinds of measurement.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}, got {source!r}")
    table = SOURCES[source]

    q = [f"""SELECT c.ticker, c.ts, c.yes_bid, c.yes_ask, c.open, c.high, c.low,
                   c.close, c.volume, c.open_interest,
                   m.series, m.title, m.sub_title, m.event_ticker, m.home,
                   m.away, m.close_time, m.status, m.result, m.settlement_value
            FROM {table} c JOIN markets m ON m.ticker = c.ticker"""]
    where, args = [], []
    if start_ts is not None:
        where.append("c.ts >= ?"); args.append(int(start_ts))
    if end_ts is not None:
        where.append("c.ts <= ?"); args.append(int(end_ts))
    if series:
        where.append(f"m.series IN ({','.join('?' * len(series))})"); args += list(series)
    if tickers:
        where.append(f"c.ticker IN ({','.join('?' * len(tickers))})"); args += list(tickers)
    if interval_min is not None and source == "backfill":
        where.append("c.interval_min = ?"); args.append(int(interval_min))
    if where:
        q.append("WHERE " + " AND ".join(where))
    q.append("ORDER BY c.ts ASC, c.ticker ASC")     # ticker breaks ties deterministically
    return con.execute(" ".join(q), args).fetchall()


def upsert_backfill_candles(con, ticker, interval_min, rows):
    """Insert archive candles. Idempotent on (ticker, ts, interval_min), so the
    same window can be re-pulled at a different resolution without either
    overwriting the other."""
    payload = [(ticker, int(r["ts"]), int(interval_min),
                r.get("yes_bid"), r.get("yes_ask"),
                r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                r.get("volume"), r.get("open_interest")) for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO backfill_candles "
        "(ticker, ts, interval_min, yes_bid, yes_ask, open, high, low, close, "
        " volume, open_interest) VALUES (?,?,?,?,?,?,?,?,?,?,?)", payload)
    con.commit()
    return len(payload)


def backfill_candle_count(con, interval_min=None):
    if interval_min is None:
        return con.execute("SELECT COUNT(*) n FROM backfill_candles").fetchone()["n"]
    return con.execute("SELECT COUNT(*) n FROM backfill_candles WHERE interval_min = ?",
                       (int(interval_min),)).fetchone()["n"]


def market_count(con):
    return con.execute("SELECT COUNT(*) n FROM markets").fetchone()["n"]


def candle_count(con):
    return con.execute("SELECT COUNT(*) n FROM candles").fetchone()["n"]


def date_range(con):
    row = con.execute("SELECT MIN(ts) lo, MAX(ts) hi FROM candles").fetchone()
    return (row["lo"], row["hi"])
