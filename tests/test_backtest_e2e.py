"""End-to-end: a spec file, through the store, engine, and metrics."""
import pytest

from wc.backtest import data, engine, metrics
from wc.backtest.run import walk_forward
from wc.backtest.spec import load


def _make_db(tmp_path, name, n_markets, yes_every):
    """`n_markets` markets whose YES trades at 6/8c (7c mid), entered ~35 days
    out and settled on a rolling basis so position slots free up.

    `yes_every` sets how often YES actually wins. Buying NO at the 95c ask
    implies a 5% YES rate, so yes_every=20 is FAIR and anything rarer is the
    overpricing the favorite-longshot literature describes.
    """
    con = data.connect(str(tmp_path / name))
    day = 86400
    for i in range(n_markets):
        tk = f"KXTEST-{i}"
        entry_ts = i * day
        close = entry_ts + 35 * day
        data.upsert_market(con, {
            "ticker": tk, "series": "KXTEST", "title": "t",
            "close_time": close, "status": "settled",
            "result": "yes" if i % yes_every == 0 else "no"})
        bar = {"yes_bid": 6, "yes_ask": 8, "open": 7, "high": 7, "low": 7,
               "close": 7, "volume": 5000, "open_interest": 1000}
        # Three bars: the engine decides on one and fills on the NEXT (see
        # Costs.fill_delay_bars), so a decide/settle pair would never trade.
        data.upsert_candles(con, tk, [{"ts": entry_ts, **bar},
                                      {"ts": entry_ts + 60, **bar},
                                      {"ts": close, **bar}])
    return con


@pytest.fixture
def db(tmp_path):
    """Longshot OVERPRICED: YES at 7c wins 1-in-60 (1.7%) against a 5% implied
    rate — the documented favorite-longshot bias. Selling it should profit."""
    return _make_db(tmp_path, "overpriced.db", n_markets=60, yes_every=60)


def uncapped(spec):
    """The shipped caps let only ~20 of the 60 markets hold a slot at once, so
    WHICH markets trade depends on scheduling — and with a 5% YES rate the
    traded subset's win count is then a coin flip. These two tests are about
    the ECONOMICS (does the engine charge the spread?), not about position
    scheduling, so they let every market through and the outcome is exact.
    """
    s = dict(spec)
    s["caps"] = {"daily_spend_dollars": 1e6, "per_market_dollars": 5.0,
                 "total_exposure_dollars": 1e6}
    s["sizing"] = dict(spec["sizing"])
    s["sizing"].pop("max_concurrent_positions", None)
    return s


@pytest.fixture
def fair_db(tmp_path):
    """Longshot FAIRLY priced: YES wins 1-in-20, exactly the 5% that buying NO
    at 95c implies. No edge, so fees must make it a loser."""
    return _make_db(tmp_path, "fair.db", n_markets=60, yes_every=20)


def test_shipped_longshot_strategy_runs_end_to_end(db):
    spec = uncapped(load("strategies/sell-cheap-longshots.json"))
    bars = data.load_bars(db)
    assert bars

    trades, rej = engine.run(spec, bars, starting_bankroll=1000.0)
    assert trades, f"no trades placed; rejections={rej}"

    report = metrics.summarize(trades, 1000.0, rej)
    assert report["n_trades"] == len(trades)
    assert report["gross_pnl"] == pytest.approx(
        report["net_pnl"] + report["fees"], abs=1e-6)

    # selling an OVERPRICED longshot is the documented edge
    assert report["net_pnl"] > 0, "selling overpriced longshots should profit"


def test_metrics_report_has_every_required_section(db):
    spec = load("strategies/sell-cheap-longshots.json")
    trades, rej = engine.run(spec, data.load_bars(db), 1000.0)
    r = metrics.summarize(trades, 1000.0, rej)

    for key in ("net_pnl", "gross_pnl", "fees", "max_drawdown", "sharpe",
                "win_rate", "brier", "calibration", "by_days_to_resolution",
                "by_price", "by_series", "equity_curve", "caveat"):
        assert key in r, f"missing {key}"

    assert "UPPER BOUND" in r["caveat"], "the fill-optimism caveat must be stated"


def test_walk_forward_reports_out_of_sample(db):
    spec = load("strategies/sell-cheap-longshots.json")
    trades, rej, windows = walk_forward(spec, data.load_bars(db), n_windows=4,
                                        starting_bankroll=1000.0)
    assert windows, "walk-forward produced no windows"
    assert all("net_pnl" in w for w in windows)
    # window 0 is never scored — it is the initial history
    assert min(w["window"] for w in windows) >= 1


def test_calibration_and_brier_computed_on_settled_trades(db):
    spec = load("strategies/model-edge.json")
    probs = {(b["ticker"], b["ts"]): 0.30 for b in data.load_bars(db)}
    trades, _ = engine.run(spec, data.load_bars(db), 1000.0, model_probs=probs)
    if trades:
        r = metrics.summarize(trades, 1000.0)
        assert r["brier"] is not None
        assert 0.0 <= r["brier"] <= 1.0


def test_strategy_loses_when_the_longshot_is_fairly_priced(fair_db):
    """The other half of the claim, and the more important one: the edge comes
    from the mispricing, not from the strategy shape. If this ever passes with
    a profit, the engine is not charging the spread properly."""
    spec = uncapped(load("strategies/sell-cheap-longshots.json"))
    trades, rej = engine.run(spec, data.load_bars(fair_db), starting_bankroll=1000.0)
    assert trades, f"no trades placed; rejections={rej}"
    report = metrics.summarize(trades, 1000.0, rej)

    # Exactly fair: 57 NO wins x +$0.25 and 3 NO losses x -$4.75 cancel to zero
    # gross, so anything the engine charges must push it negative.
    assert report["gross_pnl"] == pytest.approx(0.0, abs=1e-6)
    assert report["fees"] > 0
    assert report["net_pnl"] < 0, "no edge means no profit, after spread and fees"
