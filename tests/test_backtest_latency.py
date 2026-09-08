"""Fill latency.

Filling on the bar that triggered a strategy is not merely imprecise, it is
BIASED: the price that caused the signal is the price the strategy gets, and
trigger prices are the least likely to survive contact with a real book. The
error never averages out, so these tests are about the engine refusing to take
that gift.
"""
import pytest

from wc.backtest import engine, metrics
from tests.test_backtest_engine import bar, spec

FREE = dict(fee_rate=0.0, slippage_cents=0.0, max_volume_share=1.0)
DELAYED = engine.Costs(**FREE, fill_delay_bars=1)
IMMEDIATE = engine.Costs(**FREE, fill_delay_bars=0)


# ── the core behaviour ────────────────────────────────────────────────────────

def test_fill_uses_the_next_bars_price_not_the_deciding_bars():
    """The decision comes from bar t; the price comes from bar t+1."""
    bars = [bar(ts=0, bid=9, ask=11),              # triggers here
            bar(ts=60, bid=19, ask=21),            # fills HERE, worse
            bar(ts=86400, result="yes", close_time=86400)]
    trades, _ = engine.run(spec(), bars, 1000.0, DELAYED)
    assert trades[0]["entry_price"] == pytest.approx(0.21)


def test_immediate_mode_still_fills_on_the_deciding_bar():
    """delay=0 is the diagnostic mode, and must be exactly the old behaviour."""
    bars = [bar(ts=0, bid=9, ask=11),
            bar(ts=60, bid=19, ask=21),
            bar(ts=86400, result="yes", close_time=86400)]
    trades, _ = engine.run(spec(), bars, 1000.0, IMMEDIATE)
    assert trades[0]["entry_price"] == pytest.approx(0.11)


def test_a_strategy_cannot_capture_the_spike_that_triggered_it():
    """The whole point. A dip-buyer fills at the recovered price, not the dip."""
    s = spec(entry={"all": [{"signal": "price", "op": "lt", "value": 0.10}]})
    bars = [bar(ts=0, bid=49, ask=51),             # no trigger
            bar(ts=60, bid=4, ask=6),              # the dip — triggers
            bar(ts=120, bid=49, ask=51),           # recovered; fills HERE
            bar(ts=86400, result="yes", close_time=86400)]

    delayed, _ = engine.run(s, bars, 1000.0, DELAYED)
    immediate, _ = engine.run(s, bars, 1000.0, IMMEDIATE)

    assert immediate[0]["entry_price"] == pytest.approx(0.06)   # got the dip
    assert delayed[0]["entry_price"] == pytest.approx(0.51)     # did not
    assert delayed[0]["net_pnl"] < immediate[0]["net_pnl"]


def test_a_favourable_move_between_decision_and_fill_is_kept():
    """Not a haircut — an honest re-read of the book. The delay must cut both
    ways, or it becomes a different kind of fudge factor."""
    bars = [bar(ts=0, bid=49, ask=51),
            bar(ts=60, bid=9, ask=11),             # moved in our favour
            bar(ts=86400, result="yes", close_time=86400)]
    s = spec(entry={"all": [{"signal": "price", "op": "lt", "value": 0.60}]})
    trades, _ = engine.run(s, bars, 1000.0, DELAYED)
    assert trades[0]["entry_price"] == pytest.approx(0.11)


# ── exits are delayed symmetrically ───────────────────────────────────────────

def test_a_stop_can_slip_through_its_trigger_price():
    """A stop filled at its own trigger is the most common way a backtested
    stop-loss flatters itself. It must be able to slip."""
    s = spec(entry={"all": [{"signal": "price", "op": "lt", "value": 0.60}]},
             exit={"any": [{"signal": "unrealized_pnl_pct", "op": "lt",
                            "value": -0.20}]})
    bars = [bar(ticker="M", ts=0, bid=49, ask=51, close_time=90 * 86400),
            bar(ticker="M", ts=60, bid=49, ask=51, close_time=90 * 86400),
            bar(ticker="M", ts=120, bid=30, ask=32, close_time=90 * 86400),
            bar(ticker="M", ts=180, bid=10, ask=12, close_time=90 * 86400)]

    delayed, _ = engine.run(s, bars, 1000.0, DELAYED)
    immediate, _ = engine.run(s, bars, 1000.0, IMMEDIATE)

    assert delayed[0]["exit_price"] == pytest.approx(0.10)     # slipped to 10c
    assert immediate[0]["exit_price"] == pytest.approx(0.30)   # got the trigger
    assert delayed[0]["net_pnl"] < immediate[0]["net_pnl"]


def test_settlement_beats_a_pending_exit():
    """A market that resolves before the exit fills settles — it does not exit
    at a stale price and it does not vanish."""
    s = spec(entry={"all": [{"signal": "price", "op": "lt", "value": 0.60}]},
             exit={"any": [{"signal": "days_held", "op": "gte", "value": 0}]})
    bars = [bar(ticker="M", ts=0, bid=49, ask=51, close_time=200),
            bar(ticker="M", ts=60, bid=49, ask=51, close_time=200),
            bar(ticker="M", ts=200, bid=49, ask=51, close_time=200, result="yes")]
    trades, _ = engine.run(s, bars, 1000.0, DELAYED)
    assert len(trades) == 1
    assert trades[0]["settled"] is True


# ── stale intents ─────────────────────────────────────────────────────────────

def test_an_intent_expires_when_the_fill_bar_is_too_far_away():
    """A market that went quiet for hours is not one you would still be sending
    that order into. Filling there is worse than not modelling latency at all."""
    costs = engine.Costs(**FREE, fill_delay_bars=1, max_fill_age_seconds=600)
    bars = [bar(ts=0), bar(ts=86400, close_time=90 * 86400)]     # 24h gap
    trades, rej = engine.run(spec(), bars, 1000.0, costs)
    assert trades == []
    assert rej["entry_expired_before_fill"] == 1


def test_an_expired_exit_leaves_the_position_open():
    """The exit did not happen, so the strategy still holds — which is what
    actually occurs when a stop cannot be hit."""
    costs = engine.Costs(**FREE, fill_delay_bars=1, max_fill_age_seconds=600)
    s = spec(entry={"all": [{"signal": "price", "op": "lt", "value": 0.60}]},
             exit={"any": [{"signal": "days_held", "op": "gte", "value": 0}]})
    bars = [bar(ticker="M", ts=0, bid=49, ask=51, close_time=900 * 86400),
            bar(ticker="M", ts=60, bid=49, ask=51, close_time=900 * 86400),
            bar(ticker="M", ts=120, bid=49, ask=51, close_time=900 * 86400),
            bar(ticker="M", ts=90000, bid=49, ask=51, close_time=900 * 86400)]
    trades, rej = engine.run(s, bars, 1000.0, costs)
    assert rej["exit_expired_before_fill"] >= 1
    assert any(t.get("liquidated_at_end") for t in trades), "position stayed open"


# ── caps must see committed-but-unfilled capital ──────────────────────────────

def test_committed_capital_counts_against_the_daily_cap():
    """Two intents raised before either fills must not each pass the cap check
    and then collectively breach it."""
    s = spec()
    s["sizing"] = {"method": "fixed", "dollars": 10.0, "max_bet_dollars": 10.0}
    s["caps"]["daily_spend_dollars"] = 25.0            # only 2 x $10 fit
    bars = [bar(ticker=f"M{i}", ts=0, close_time=90 * 86400) for i in range(10)]
    bars += [bar(ticker=f"M{i}", ts=60, close_time=90 * 86400) for i in range(10)]
    _, rej = engine.run(s, sorted(bars, key=lambda b: b["ts"]), 10_000.0, DELAYED)
    assert rej.get("daily_cap", 0) > 0


def test_committed_capital_counts_against_max_positions():
    s = spec()
    s["sizing"] = {"method": "fixed", "dollars": 10.0, "max_bet_dollars": 10.0,
                   "max_concurrent_positions": 2}
    bars = [bar(ticker=f"M{i}", ts=0, close_time=90 * 86400) for i in range(6)]
    _, rej = engine.run(s, bars, 10_000.0, DELAYED)
    assert rej.get("max_concurrent_positions", 0) > 0


# ── the diagnostic this exists to enable ──────────────────────────────────────

def test_latency_sensitivity_exposes_a_trigger_dependent_strategy():
    """The useful output: a strategy living off its own trigger price shows a
    large gap between the two fill modes, and one that isn't barely moves."""
    trigger = spec(entry={"all": [{"signal": "price", "op": "lt", "value": 0.10}]})
    bars = []
    for i in range(30):
        t = i * 300
        bars += [bar(ticker=f"M{i}", ts=t, bid=49, ask=51, close_time=90 * 86400),
                 bar(ticker=f"M{i}", ts=t + 60, bid=4, ask=6, close_time=90 * 86400),
                 bar(ticker=f"M{i}", ts=t + 120, bid=49, ask=51,
                     close_time=90 * 86400)]

    d, _ = engine.run(trigger, bars, 100_000.0, DELAYED)
    i_, _ = engine.run(trigger, bars, 100_000.0, IMMEDIATE)
    assert d and i_
    assert metrics.summarize(i_, 100_000.0)["net_pnl"] > \
           metrics.summarize(d, 100_000.0)["net_pnl"]
