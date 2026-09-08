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
  close_time INTEGER,
  status     TEXT,
  result     TEXT,
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
    """Open (creating if needed) the history DB with the schema applied."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


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
        INSERT INTO markets (ticker, series, title, close_time, status, result,
                             first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            title      = excluded.title,
            close_time = excluded.close_time,
            status     = excluded.status,
            result     = excluded.result,
            last_seen  = excluded.last_seen
    """, (m["ticker"], m.get("series") or "", m.get("title"),
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


def set_progress(con, ticker, last_ts):
    con.execute("INSERT OR REPLACE INTO ingest_state (ticker, last_ts, updated_at) "
                "VALUES (?,?,?)", (ticker, int(last_ts), int(time.time())))
    con.commit()


def get_progress(con, ticker):
    row = con.execute("SELECT last_ts FROM ingest_state WHERE ticker = ?",
                      (ticker,)).fetchone()
    return row["last_ts"] if row else None


# ── reads ─────────────────────────────────────────────────────────────────────

def load_bars(con, start_ts=None, end_ts=None, series=None, tickers=None):
    """Every candle in the window, joined to its market, in CHRONOLOGICAL order
    across all markets.

    The ordering is load-bearing: the engine must see markets interleaved in
    time, or portfolio-level caps (daily spend, total exposure, concurrent
    positions) can never bind, because it would never hold two markets at once.
    """
    q = ["""SELECT c.ticker, c.ts, c.yes_bid, c.yes_ask, c.open, c.high, c.low,
                   c.close, c.volume, c.open_interest,
                   m.series, m.title, m.close_time, m.status, m.result
            FROM candles c JOIN markets m ON m.ticker = c.ticker"""]
    where, args = [], []
    if start_ts is not None:
        where.append("c.ts >= ?"); args.append(int(start_ts))
    if end_ts is not None:
        where.append("c.ts <= ?"); args.append(int(end_ts))
    if series:
        where.append(f"m.series IN ({','.join('?' * len(series))})"); args += list(series)
    if tickers:
        where.append(f"c.ticker IN ({','.join('?' * len(tickers))})"); args += list(tickers)
    if where:
        q.append("WHERE " + " AND ".join(where))
    q.append("ORDER BY c.ts ASC, c.ticker ASC")     # ticker breaks ties deterministically
    return con.execute(" ".join(q), args).fetchall()


def market_count(con):
    return con.execute("SELECT COUNT(*) n FROM markets").fetchone()["n"]


def candle_count(con):
    return con.execute("SELECT COUNT(*) n FROM candles").fetchone()["n"]


def date_range(con):
    row = con.execute("SELECT MIN(ts) lo, MAX(ts) hi FROM candles").fetchone()
    return (row["lo"], row["hi"])
