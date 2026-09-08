"""Positions open when the data ends must be accounted for, not dropped."""
from wc.backtest import engine, metrics
from tests.test_backtest_engine import bar, spec, with_fill_bar, NO_COST


def test_open_position_is_liquidated_not_dropped():
    """Silently discarding an open position hides its P&L and flatters the
    result. It must appear as a trade, flagged."""
    bars = with_fill_bar([bar(ts=0),
                          bar(ts=86400, close_time=10 * 86400)])   # never settles
    trades, rej = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)

    assert len(trades) == 1
    assert trades[0]["liquidated_at_end"] is True
    assert trades[0]["settled"] is False
    assert rej.get("open_at_end") == 1


def test_metrics_separate_settled_from_liquidated():
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, close_time=10 * 86400)])
    trades, rej = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)
    r = metrics.summarize(trades, 1000.0, rej)
    assert r["n_liquidated_at_end"] == 1
    assert r["n_settled"] == 0


def test_settled_positions_are_not_flagged_as_liquidated():
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["settled"] is True
    assert "liquidated_at_end" not in trades[0]
