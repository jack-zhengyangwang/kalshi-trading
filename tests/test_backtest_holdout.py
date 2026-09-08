"""Walk-forward, the held-out period, and the in-sample/out-of-sample pairing.

A single in-sample number is not evidence. These tests are about the runner
making the honest number the one that is hard to avoid seeing.
"""
import json

import pytest

from wc.backtest import data, run


def seed(con, n_bars=40, tickers=("M1", "M2")):
    day = 86400
    for t in tickers:
        data.upsert_market(con, {"ticker": t, "series": "S", "title": t,
                                 "sub_title": t, "close_time": 500 * day,
                                 "status": "active", "result": "yes"}, now=0)
        data.upsert_candles(con, t, [
            {"ts": i * day, "yes_bid": 39, "yes_ask": 41, "close": 40,
             "volume": 10_000, "open_interest": 1000} for i in range(n_bars)])


@pytest.fixture
def db(tmp_path):
    con = data.connect(str(tmp_path / "h.db"))
    seed(con)
    return str(tmp_path / "h.db")


SPEC = {
    "name": "hold-test", "side": "yes",
    "universe": {"min_volume": 1},
    "entry": {"all": [{"signal": "price", "op": "lt", "value": 0.90}]},
    "sizing": {"method": "fixed", "dollars": 20.0, "max_bet_dollars": 20.0},
    "exit": {"any": [{"signal": "days_held", "op": "gt", "value": 2}]},
    "caps": {"daily_spend_dollars": 1e6, "per_market_dollars": 1e6,
             "total_exposure_dollars": 1e6},
}


@pytest.fixture
def spec_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SPEC))
    return str(p)


def load_report(tmp_path):
    return json.loads((tmp_path / "out.json").read_text())


def test_holdout_is_reported_separately(db, spec_file, tmp_path):
    """The most recent period is touched once, at the end, and reported on its
    own — walk-forward alone still tunes against every window it scores."""
    rc = run.main([spec_file, "--db", db, "--out", str(tmp_path / "out.json")])
    assert rc == 0
    r = load_report(tmp_path)
    assert "holdout" in r
    assert "HELD-OUT" in r["holdout"]["label"]
    assert r["holdout"]["window"][0] > r["windows"][0]["start"]


def test_in_sample_and_out_of_sample_live_in_the_same_file(db, spec_file, tmp_path):
    """Nobody should have to go looking for the unflattering half."""
    run.main([spec_file, "--db", db, "--out", str(tmp_path / "out.json")])
    r = load_report(tmp_path)
    assert "out-of-sample" in r["label"]
    assert "in-sample" in r["in_sample"]["label"]
    assert r["in_sample"]["n_trades"] >= r["n_trades"]     # IS sees all the bars


def test_holdout_can_be_disabled(db, spec_file, tmp_path):
    run.main([spec_file, "--db", db, "--holdout-frac", "0",
              "--out", str(tmp_path / "out.json")])
    assert "holdout" not in load_report(tmp_path)


def test_holdout_never_swallows_all_the_data(tmp_path):
    """With too little history to split, the runner must still score something
    rather than silently reporting on an empty training set."""
    con = data.connect(str(tmp_path / "tiny.db"))
    seed(con, n_bars=2, tickers=("M1",))
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SPEC))
    rc = run.main([str(p), "--db", str(tmp_path / "tiny.db"),
                   "--holdout-frac", "0.99", "--out", str(tmp_path / "out.json")])
    assert rc == 0
    assert load_report(tmp_path)["bars"] > 0


def test_in_sample_flag_skips_the_walk_forward(db, spec_file, tmp_path):
    run.main([spec_file, "--db", db, "--in-sample",
              "--out", str(tmp_path / "out.json")])
    r = load_report(tmp_path)
    assert "NOT evidence" in r["label"]
    assert "in_sample" not in r          # the run IS the in-sample run


def test_lookahead_stamp_absent_without_brains(db, spec_file, tmp_path):
    run.main([spec_file, "--db", db, "--out", str(tmp_path / "out.json")])
    assert "lookahead_risk" not in load_report(tmp_path)


def test_liquidation_warning_names_the_real_cause(tmp_path):
    """A run over hours of data cannot settle anything. Blaming walk-forward
    window width there sends you to fix the wrong thing."""
    con = data.connect(str(tmp_path / "short.db"))
    data.upsert_market(con, {"ticker": "M", "series": "S", "title": "M",
                             "close_time": 500 * 86400, "status": "active",
                             "result": "yes"}, now=0)
    data.upsert_candles(con, "M", [
        {"ts": t, "yes_bid": 39, "yes_ask": 41, "close": 40,
         "volume": 10_000, "open_interest": 1000} for t in (0, 300, 600)])
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SPEC))
    run.main([str(p), "--db", str(tmp_path / "short.db"), "--in-sample",
              "--out", str(tmp_path / "out.json")])
    warns = " ".join(load_report(tmp_path)["warnings"])
    assert "hours" in warns and "Collect more history" in warns
    assert "walk-forward" not in warns


def test_latency_sensitivity_is_reported(db, spec_file, tmp_path):
    """Every run measures how much of its edge is the strategy filling at its
    own trigger price."""
    run.main([spec_file, "--db", db, "--out", str(tmp_path / "out.json")])
    r = load_report(tmp_path)
    assert "same_bar_fill" in r
    assert "DIAGNOSTIC" in r["same_bar_fill"]["label"]
    assert r["costs"]["fill_delay_bars"] == 1


def test_latency_check_can_be_skipped(db, spec_file, tmp_path):
    run.main([spec_file, "--db", db, "--no-latency-check",
              "--out", str(tmp_path / "out.json")])
    assert "same_bar_fill" not in load_report(tmp_path)


def test_an_execution_artefact_is_called_out(tmp_path):
    """A strategy that only profits when filled at its trigger must be named as
    an artefact, not shipped as an edge."""
    con = data.connect(str(tmp_path / "spike.db"))
    day = 86400
    for i in range(40):
        tk = f"M{i}"
        data.upsert_market(con, {"ticker": tk, "series": "S", "title": tk,
                                 "close_time": 300 * day, "status": "active",
                                 "result": "no"}, now=0)
        t = i * 3600
        data.upsert_candles(con, tk, [
            {"ts": t, "yes_bid": 49, "yes_ask": 51, "close": 50,
             "volume": 10_000, "open_interest": 1000},
            {"ts": t + 60, "yes_bid": 4, "yes_ask": 6, "close": 5,
             "volume": 10_000, "open_interest": 1000},          # the spike
            {"ts": t + 120, "yes_bid": 49, "yes_ask": 51, "close": 50,
             "volume": 10_000, "open_interest": 1000}])

    s = dict(SPEC)
    s["name"] = "spike-chaser"
    s["entry"] = {"all": [{"signal": "price", "op": "lt", "value": 0.10}]}
    s["exit"] = {"any": [{"signal": "price", "op": "gt", "value": 0.40}]}
    p = tmp_path / "spike.json"
    p.write_text(json.dumps(s))

    run.main([str(p), "--db", str(tmp_path / "spike.db"), "--in-sample",
              "--holdout-frac", "0", "--out", str(tmp_path / "out.json")])
    r = load_report(tmp_path)
    assert r["same_bar_fill"]["net_pnl"] > r["net_pnl"]
    assert any("LATENCY-SENSITIVE" in w for w in r["warnings"])
