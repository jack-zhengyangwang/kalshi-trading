"""Data store: idempotency, ordering, and survivorship-bias guards."""
import pytest

from wc.backtest import data


@pytest.fixture
def con(tmp_path):
    return data.connect(str(tmp_path / "t.db"))


def mkt(ticker="M1", series="S", close_time=86400, result=None, status="active"):
    return {"ticker": ticker, "series": series, "title": "t",
            "close_time": close_time, "status": status, "result": result}


def candle(ts=0, **kw):
    row = {"ts": ts, "yes_bid": 9, "yes_ask": 11, "open": 10, "high": 10,
           "low": 10, "close": 10, "volume": 100, "open_interest": 50}
    row.update(kw)
    return row


def test_backfill_is_idempotent(con):
    """Re-running a backfill must never duplicate or corrupt rows."""
    data.upsert_market(con, mkt())
    rows = [candle(ts=i * 60) for i in range(10)]
    data.upsert_candles(con, "M1", rows)
    assert data.candle_count(con) == 10

    data.upsert_candles(con, "M1", rows)          # again
    assert data.candle_count(con) == 10


def test_upsert_candles_updates_in_place(con):
    data.upsert_market(con, mkt())
    data.upsert_candles(con, "M1", [candle(ts=0, close=10)])
    data.upsert_candles(con, "M1", [candle(ts=0, close=42)])
    row = con.execute("SELECT close FROM candles WHERE ticker='M1' AND ts=0").fetchone()
    assert row["close"] == 42


def test_first_seen_survives_updates(con):
    """first_seen is what lets us ask 'what existed on date D' — the guard
    against survivorship bias. An update must never move it."""
    data.upsert_market(con, mkt(), now=1000)
    data.upsert_market(con, mkt(status="settled", result="yes"), now=9999)
    row = con.execute("SELECT first_seen, last_seen, result FROM markets").fetchone()
    assert row["first_seen"] == 1000
    assert row["last_seen"] == 9999
    assert row["result"] == "yes"


def test_bars_are_chronological_across_markets(con):
    """Load-bearing: the engine must see markets interleaved in time, or
    portfolio caps can never bind."""
    for t in ("A", "B", "C"):
        data.upsert_market(con, mkt(ticker=t))
    data.upsert_candles(con, "A", [candle(ts=30), candle(ts=0)])
    data.upsert_candles(con, "B", [candle(ts=10)])
    data.upsert_candles(con, "C", [candle(ts=20)])

    bars = data.load_bars(con)
    assert [b["ts"] for b in bars] == [0, 10, 20, 30]
    assert [b["ticker"] for b in bars] == ["A", "B", "C", "A"]


def test_load_bars_joins_market_metadata(con):
    data.upsert_market(con, mkt(result="yes", close_time=500))
    data.upsert_candles(con, "M1", [candle(ts=0)])
    b = data.load_bars(con)[0]
    assert b["series"] == "S"
    assert b["close_time"] == 500
    assert b["result"] == "yes"


def test_load_bars_window_and_series_filters(con):
    data.upsert_market(con, mkt(ticker="A", series="S1"))
    data.upsert_market(con, mkt(ticker="B", series="S2"))
    data.upsert_candles(con, "A", [candle(ts=i) for i in (0, 100, 200)])
    data.upsert_candles(con, "B", [candle(ts=i) for i in (0, 100, 200)])

    assert len(data.load_bars(con, start_ts=100)) == 4
    assert len(data.load_bars(con, end_ts=100)) == 4
    assert len(data.load_bars(con, series=["S1"])) == 3
    assert len(data.load_bars(con, tickers=["B"])) == 3


def test_progress_is_resumable(con):
    assert data.get_progress(con, "M1") is None
    data.set_progress(con, "M1", 12345)
    assert data.get_progress(con, "M1") == 12345
    data.set_progress(con, "M1", 99999)
    assert data.get_progress(con, "M1") == 99999


def test_trades_idempotent(con):
    rows = [{"trade_id": "t1", "ticker": "M1", "ts": 0, "price": 10,
             "count": 5, "taker_side": "yes"}]
    data.upsert_trades(con, rows)
    data.upsert_trades(con, rows)
    assert con.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"] == 1


def test_settled_result_is_never_overwritten_by_a_later_listing(con):
    """A settlement outcome cannot be recomputed after the fact. An open-market
    listing carries result=NULL, so upsert must COALESCE rather than assign —
    otherwise routine re-listing erases truth, silently and undetectably."""
    data.upsert_market(con, {"ticker": "T", "series": "S", "close_time": 100,
                             "status": "active", "result": None})
    data.set_result(con, "T", "settled", "yes")
    data.upsert_market(con, {"ticker": "T", "series": "S", "close_time": 100,
                             "status": "active", "result": None})
    assert con.execute("SELECT result FROM markets").fetchone()["result"] == "yes"
