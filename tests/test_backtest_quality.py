"""The data quality report.

A backtest on bad data produces a confident wrong number, so these tests are
about the report telling the truth — especially that it flags the problems that
would otherwise be invisible, and does not abort on the ones that are normal.
"""
import time

import pytest

from wc.backtest import data, quality


@pytest.fixture
def con(tmp_path):
    return data.connect(str(tmp_path / "h.db"))


def add(con, ticker, ts_list, series="S", close_time=None, result=None,
        bid=40, ask=44, status="active"):
    data.upsert_market(con, {"ticker": ticker, "series": series, "title": ticker,
                             "close_time": close_time, "status": status,
                             "result": result},
                       now=(ts_list[0] if ts_list else int(time.time())))
    data.upsert_candles(con, ticker, [
        {"ts": t, "yes_bid": bid, "yes_ask": ask, "close": (bid + ask) // 2
         if (bid is not None and ask is not None) else None,
         "volume": 100, "open_interest": 50} for t in ts_list])


# ── it reports rather than raises ─────────────────────────────────────────────

def test_empty_store_reports_instead_of_crashing(con):
    r = quality.report(con)
    assert r["coverage"]["candles"] == 0
    assert "store is empty — the collector has never written a row" in r["blocking"]


def test_healthy_store_has_no_blocking_issues(con):
    now = int(time.time())
    add(con, "T1", [now - 900, now - 600, now - 300], close_time=now + 3600)
    add(con, "T2", [now - 900, now - 600, now - 300], close_time=now - 100,
        result="yes", status="settled")
    assert quality.report(con)["blocking"] == []


# ── the invisible failures ────────────────────────────────────────────────────

def test_crossed_book_is_blocking(con):
    """bid > ask is not a weird market, it is a field mapping bug."""
    add(con, "T1", [1000], bid=60, ask=40)
    r = quality.report(con)
    assert r["price_sanity"]["crossed_books"] == 1
    assert any("crossed books" in b for b in r["blocking"])


def test_candle_after_close_time_is_blocking(con):
    """A candle dated after settlement can leak the outcome into a bar the
    strategy is allowed to see."""
    add(con, "T1", [5000], close_time=4000)
    r = quality.report(con)
    assert r["clock_sanity"]["candles_after_close_time"] == 1
    assert any("outcome leak" in b for b in r["blocking"])


def test_orphan_candles_are_reported(con):
    """load_bars INNER JOINs markets, so an orphan candle is dropped from every
    backtest without an error anywhere."""
    data.upsert_candles(con, "GHOST", [{"ts": 1000, "yes_bid": 40, "yes_ask": 44,
                                        "close": 42, "volume": 1, "open_interest": 1}])
    r = quality.report(con)
    assert r["clock_sanity"]["orphan_candles_no_market_row"] == 1
    assert any("orphan" in b for b in r["blocking"])


def test_low_settlement_coverage_is_blocking(con):
    """Prices without outcomes cannot produce PnL, Brier, or calibration."""
    now = int(time.time())
    for i in range(9):
        add(con, f"U{i}", [now - 600], close_time=now - 100)      # closed, no result
    add(con, "R1", [now - 600], close_time=now - 100, result="yes", status="settled")
    r = quality.report(con)
    assert r["settlement"]["settlement_rate"] == 0.1
    assert any("settlement coverage" in b for b in r["blocking"])


# ── the normal things it must NOT flag ────────────────────────────────────────

def test_empty_books_are_counted_but_not_blocking(con):
    """Illiquid legs are recorded on purpose — that is the survivorship guard."""
    now = int(time.time())
    add(con, "T1", [now - 600], close_time=now - 100, result="no",
        status="settled", bid=None, ask=None)
    r = quality.report(con)
    assert r["price_sanity"]["empty_book_snapshots"] == 1
    assert r["blocking"] == []


def test_still_open_markets_are_not_counted_as_missing_settlement(con):
    now = int(time.time())
    add(con, "OPEN", [now - 600], close_time=now + 86400)
    add(con, "DONE", [now - 600], close_time=now - 100, result="yes",
        status="settled")
    r = quality.report(con)
    assert r["settlement"]["settlement_rate"] == 1.0
    assert r["survivorship"]["still_open"] == 1


# ── gaps ──────────────────────────────────────────────────────────────────────

def test_gap_detection_finds_the_missing_window(con):
    add(con, "T1", [0, 300, 600, 3000, 3300], close_time=99999)
    g = quality.report(con)["gaps"]
    assert g["markets_with_gaps"] == 1
    assert g["total_missing_snapshots"] == 7          # 600 -> 3000 at 300s
    assert g["worst"][0]["ticker"] == "T1"


def test_regular_sampling_has_no_gaps(con):
    add(con, "T1", [0, 300, 600, 900], close_time=99999)
    assert quality.report(con)["gaps"]["markets_with_gaps"] == 0


def test_gaps_do_not_span_across_tickers(con):
    """Two markets sampled at different times are not a gap in either."""
    add(con, "A", [0, 300], close_time=99999)
    add(con, "B", [90000, 90300], close_time=199999)
    assert quality.report(con)["gaps"]["markets_with_gaps"] == 0


def test_by_series_breaks_out_thin_leagues(con):
    add(con, "A1", [0, 300, 600], series="BIG", close_time=99999)
    add(con, "B1", [0], series="THIN", close_time=99999)
    rows = {r["series"]: r for r in quality.report(con)["by_series"]}
    assert rows["BIG"]["candles"] == 3
    assert rows["THIN"]["candles"] == 1
