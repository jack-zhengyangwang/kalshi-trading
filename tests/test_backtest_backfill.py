"""Historical backfill — the BACKTEST's data source.

Separate from the forward collector by design: different question, different
table, different biases. These tests are mostly about that separation holding,
and about Kalshi's dollar-string payload becoming integer cents exactly once.
"""
import pytest

from wc.backtest import backfill, data


def candle(ts=1000, bid="0.4000", ask="0.4400", o=None, h=None, l=None, c=None,
           vol="120.00", oi="500.00"):
    price = {}
    for k, v in (("open_dollars", o), ("high_dollars", h),
                 ("low_dollars", l), ("close_dollars", c)):
        if v is not None:
            price[k] = v
    return {"end_period_ts": ts,
            "yes_bid": {"close_dollars": bid} if bid else {},
            "yes_ask": {"close_dollars": ask} if ask else {},
            "price": price, "volume_fp": vol, "open_interest_fp": oi}


class FakeClient:
    def __init__(self, markets=None, candles=None, fail=False):
        self.markets = markets or []
        self.candles = candles if candles is not None else [candle()]
        self.fail = fail
        self.calls = []

    def list_markets_by_series(self, series, status="open", page=200):
        return self.markets

    def _get(self, path, params=None, auth=True):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise RuntimeError("archive unavailable")
        return {"candlesticks": self.candles}


def mkt(ticker="KXEPLGAME-26SEP06ARSCFC-ARS", close="2026-09-06T17:00:00Z",
        result="yes", value="1.0000"):
    return {"ticker": ticker, "close_time": close, "status": "finalized",
            "result": result, "settlement_value_dollars": value,
            "yes_sub_title": "Arsenal", "title": "Arsenal wins",
            "event_ticker": "KXEPLGAME-26SEP06ARSCFC",
            "rules_secondary": "refers to the Arsenal vs Chelsea professional EPL soccer game"}


@pytest.fixture
def con(tmp_path):
    return data.connect(str(tmp_path / "h.db"))


# ── parsing ───────────────────────────────────────────────────────────────────

def test_dollar_strings_become_integer_cents():
    """Kalshi sends money as dollar strings. Float cents is one of the two
    classic sources of silent backtest error."""
    r = backfill.parse_candle(candle(bid="0.4000", ask="0.4400"))
    assert r["yes_bid"] == 40 and r["yes_ask"] == 44
    assert isinstance(r["yes_bid"], int)


def test_a_period_with_no_trades_has_no_price_ohlc():
    """Inventing an open/high/low would give the engine a price to trade
    against that nobody ever paid."""
    r = backfill.parse_candle(candle())
    assert r["open"] is None and r["high"] is None and r["close"] is None
    assert r["yes_bid"] == 40, "the book is still known"


def test_trade_ohlc_is_carried_when_present():
    r = backfill.parse_candle(
        candle(o="0.4100", h="0.4600", l="0.3900", c="0.4200"))
    assert (r["open"], r["high"], r["low"], r["close"]) == (41, 46, 39, 42)


def test_volume_and_open_interest_survive_the_fp_strings():
    r = backfill.parse_candle(candle(vol="120.00", oi="500.75"))
    assert r["volume"] == 120 and r["open_interest"] == 500


def test_missing_book_is_none_not_zero():
    """Zero is a price; missing is not. Conflating them invents a free option."""
    r = backfill.parse_candle(candle(bid=None, ask=None))
    assert r["yes_bid"] is None and r["yes_ask"] is None


# ── windowing ─────────────────────────────────────────────────────────────────

def test_requests_are_chunked_to_a_window_the_api_accepts():
    """The 400s were window size, not missing data — 1-minute candles need a
    narrow window or the request is rejected."""
    c = FakeClient()
    backfill.fetch_candles(c, "S", "T", 0, 10 * 86400, 1)
    assert len(c.calls) == 10, "1-minute pulls chunk daily"
    c2 = FakeClient()
    backfill.fetch_candles(c2, "S", "T", 0, 10 * 86400, 60)
    assert len(c2.calls) == 1, "hourly covers the whole window in one request"


def test_a_failed_window_does_not_lose_the_rest():
    c = FakeClient(fail=True)
    assert backfill.fetch_candles(c, "S", "T", 0, 3 * 86400, 1) == []
    assert len(c.calls) == 3, "it kept going after each failure"


def test_window_is_clipped_to_the_lookback():
    m = mkt(close="2026-09-06T00:00:00Z")
    m["open_time"] = "2020-01-01T00:00:00Z"
    start, end = backfill.market_window(m, days=10)
    assert end - start == 10 * 86400


# ── writing ───────────────────────────────────────────────────────────────────

def test_backfill_writes_candles_and_the_outcome(con):
    c = FakeClient(markets=[mkt()], candles=[candle(ts=1000), candle(ts=4600)])
    rep = backfill.backfill_series(con, c, "KXEPLGAME", days=30)
    assert rep["markets"] == 1 and rep["candles"] == 2
    m = con.execute("SELECT result, settlement_value, home, away, sub_title "
                    "FROM markets").fetchone()
    assert m["result"] == "yes" and m["settlement_value"] == pytest.approx(1.0)
    assert (m["home"], m["away"]) == ("Arsenal", "Chelsea")
    assert m["sub_title"] == "Arsenal"


def test_a_void_is_backfilled_as_a_void(con):
    c = FakeClient(markets=[mkt(result="scalar", value="0.3300")])
    backfill.backfill_series(con, c, "S", days=30)
    m = con.execute("SELECT result, settlement_value FROM markets").fetchone()
    assert m["result"] == "void" and m["settlement_value"] == pytest.approx(0.33)


def test_backfill_is_idempotent(con):
    c = FakeClient(markets=[mkt()], candles=[candle(ts=1000)])
    backfill.backfill_series(con, c, "S", days=30)
    backfill.backfill_series(con, c, "S", days=30)
    assert data.backfill_candle_count(con) == 1


def test_resolutions_coexist_without_overwriting(con):
    """Re-pulling at a finer resolution must not destroy the coarse bars."""
    c = FakeClient(markets=[mkt()], candles=[candle(ts=1000)])
    backfill.backfill_series(con, c, "S", days=30, interval_min=60)
    backfill.backfill_series(con, c, "S", days=30, interval_min=1)
    assert data.backfill_candle_count(con, 60) == 1
    assert data.backfill_candle_count(con, 1) == 1
    assert data.backfill_candle_count(con) == 2


def test_markets_older_than_the_lookback_are_skipped(con):
    c = FakeClient(markets=[mkt(close="2020-01-01T00:00:00Z")])
    assert backfill.backfill_series(con, c, "S", days=30)["markets"] == 0


def test_dry_run_writes_nothing(con):
    c = FakeClient(markets=[mkt()], candles=[candle()])
    rep = backfill.backfill_series(con, c, "S", days=30, dry_run=True)
    assert rep["candles"] == 1
    assert data.backfill_candle_count(con) == 0


# ── the separation that matters ───────────────────────────────────────────────

def test_backfill_and_collector_never_share_a_table(con):
    """A backtest reading one number over true OHLC and forward snapshots at
    once would be reporting across two different kinds of measurement."""
    data.upsert_market(con, {"ticker": "T", "series": "S", "title": "t",
                             "close_time": 9999, "status": "active",
                             "result": "yes"})
    data.upsert_candles(con, "T", [{"ts": 500, "yes_bid": 1, "yes_ask": 2,
                                    "close": 1, "volume": 9, "open_interest": 9}])
    data.upsert_backfill_candles(con, "T", 60, [
        {"ts": 500, "yes_bid": 40, "yes_ask": 44, "close": 42,
         "volume": 100, "open_interest": 50}])

    bf = data.load_bars(con, source="backfill")
    fw = data.load_bars(con, source="collector")
    assert len(bf) == 1 and len(fw) == 1
    assert bf[0]["yes_bid"] == 40 and fw[0]["yes_bid"] == 1


def test_an_unknown_source_is_refused(con):
    with pytest.raises(ValueError, match="source must be one of"):
        data.load_bars(con, source="whatever")


def test_interval_filter_isolates_a_resolution(con):
    data.upsert_market(con, {"ticker": "T", "series": "S", "title": "t",
                             "close_time": 9999, "status": "active", "result": "yes"})
    for iv in (1, 60):
        data.upsert_backfill_candles(con, "T", iv, [
            {"ts": 500, "yes_bid": 40, "yes_ask": 44, "close": 42,
             "volume": 1, "open_interest": 1}])
    assert len(data.load_bars(con, source="backfill", interval_min=60)) == 1
    assert len(data.load_bars(con, source="backfill")) == 2
