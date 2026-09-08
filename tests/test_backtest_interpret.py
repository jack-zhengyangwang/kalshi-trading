"""Interpreter: pure condition evaluation and sizing."""
import pytest

from wc.backtest import interpret


def test_all_is_and():
    c = {"all": [{"signal": "price", "op": "lt", "value": 0.5},
                 {"signal": "volume_24h", "op": "gt", "value": 100}]}
    assert interpret.evaluate(c, {"price": 0.3, "volume_24h": 500})
    assert not interpret.evaluate(c, {"price": 0.3, "volume_24h": 50})


def test_any_is_or():
    c = {"any": [{"signal": "price", "op": "lt", "value": 0.1},
                 {"signal": "days_to_resolution", "op": "lt", "value": 1}]}
    assert interpret.evaluate(c, {"price": 0.5, "days_to_resolution": 0.5})
    assert not interpret.evaluate(c, {"price": 0.5, "days_to_resolution": 5})


def test_nested_combinators():
    c = {"all": [{"signal": "price", "op": "lt", "value": 0.5},
                 {"any": [{"signal": "spread", "op": "lt", "value": 3},
                          {"signal": "volume_24h", "op": "gt", "value": 1000}]}]}
    assert interpret.evaluate(c, {"price": 0.2, "spread": 10, "volume_24h": 5000})
    assert not interpret.evaluate(c, {"price": 0.2, "spread": 10, "volume_24h": 10})


@pytest.mark.parametrize("op,val,target,expected", [
    ("lt", 1, 2, True), ("lte", 2, 2, True), ("gt", 3, 2, True),
    ("gte", 2, 2, True), ("eq", 2, 2, True),
    ("between", 5, [1, 10], True), ("between", 50, [1, 10], False),
])
def test_operators(op, val, target, expected):
    c = {"signal": "price", "op": op, "value": target}
    assert interpret.evaluate(c, {"price": val}) is expected


def test_missing_signal_fails_closed():
    """A strategy must not trade on a signal we could not compute — the same
    fail-closed reasoning as wc/lib/caps.py."""
    c = {"signal": "model_prob", "op": "gt", "value": 0.5}
    assert interpret.evaluate(c, {}) is False
    assert interpret.evaluate(c, {"model_prob": None}) is False


def test_should_exit_does_not_mutate_ctx():
    """The engine reuses ctx across strategies within a bar."""
    spec = {"exit": {"any": [{"signal": "days_held", "op": "gt", "value": 1}]}}
    ctx = {"price": 0.5}
    interpret.should_exit(spec, ctx, {"days_held": 5})
    assert ctx == {"price": 0.5}


def test_universe_bounds_use_the_market_price_not_ours():
    """Universe describes the MARKET, so it filters on yes_price. A "cheap
    longshot" is a 7c market whether we buy YES at 7c or NO at 93c."""
    spec = {"universe": {"max_yes_price_cents": 10}}
    assert interpret.passes_universe(spec, {"yes_price": 0.07, "price": 0.93})
    assert not interpret.passes_universe(spec, {"yes_price": 0.50, "price": 0.50})


def test_universe_missing_signal_rejects():
    spec = {"universe": {"min_volume": 100}}
    assert not interpret.passes_universe(spec, {"price": 0.05})


def test_universe_series_wildcard():
    assert interpret.passes_universe({"universe": {"series": ["*"]}}, {"series": "KXEPL"})
    assert not interpret.passes_universe({"universe": {"series": ["KXNFL"]}},
                                         {"series": "KXEPL"})


# ── sizing ────────────────────────────────────────────────────────────────────

def test_fixed_sizing_respects_cap():
    spec = {"sizing": {"method": "fixed", "dollars": 50.0, "max_bet_dollars": 10.0}}
    assert interpret.size(spec, {}, 1000.0) == 10.0


def test_fraction_of_bankroll():
    spec = {"sizing": {"method": "fraction_of_bankroll", "fraction": 0.02}}
    assert interpret.size(spec, {}, 1000.0) == pytest.approx(20.0)


def test_kelly_needs_model_prob_and_price():
    spec = {"sizing": {"method": "kelly", "fraction": 0.25}}
    assert interpret.size(spec, {"price": 0.5}, 1000.0) == 0.0
    assert interpret.size(spec, {"model_prob": 0.6}, 1000.0) == 0.0


def test_kelly_sizes_on_edge():
    spec = {"sizing": {"method": "kelly", "fraction": 1.0}}
    # p=0.6 at price 0.5 -> b=1, edge = (0.6*1 - 0.4)/1 = 0.2 -> 20% of bankroll
    assert interpret.size(spec, {"model_prob": 0.6, "price": 0.5}, 1000.0) \
        == pytest.approx(200.0)


def test_kelly_zero_on_negative_edge():
    spec = {"sizing": {"method": "kelly", "fraction": 1.0}}
    assert interpret.size(spec, {"model_prob": 0.4, "price": 0.5}, 1000.0) == 0.0


def test_size_never_exceeds_bankroll():
    spec = {"sizing": {"method": "fixed", "dollars": 1e9}}
    assert interpret.size(spec, {}, 100.0) == 100.0


# ── kelly delegates to the live sizer ─────────────────────────────────────────

def test_kelly_uses_the_live_sizer_not_a_local_copy(monkeypatch):
    """The backtest must size through wc/lib/kelly.py. If this test can be made
    to pass with the library stubbed out, a second implementation has crept
    back in."""
    from wc.lib import kelly as live_kelly
    calls = []

    def spy(p, price, balance, config, tau_days=0):
        calls.append((p, price, balance, config, tau_days))
        return 42.0

    monkeypatch.setattr(live_kelly, "size_tvm", spy)
    spec = {"sizing": {"method": "kelly", "fraction": 0.25}}
    stake = interpret.size(spec, {"model_prob": 0.6, "price": 0.5}, 1000.0)

    assert stake == 42.0
    assert len(calls) == 1
    assert calls[0][0] == 0.6 and calls[0][1] == 0.5


def test_kelly_matches_the_live_sizer_exactly():
    from wc.lib import kelly as live_kelly
    spec = {"sizing": {"method": "kelly", "fraction": 0.25,
                       "max_bet_dollars": 500.0}}
    ctx = {"model_prob": 0.62, "price": 0.48, "days_to_resolution": 14.0}
    expected = live_kelly.size_tvm(
        0.62, 0.48, 1000.0,
        {"kelly_fraction": 0.25, "max_bet_dollars": 500.0,
         "min_edge": 0.0, "min_bet_dollars": 0.0, "tvm_rate": 0.0},
        tau_days=14.0)
    assert interpret.size(spec, ctx, 1000.0) == pytest.approx(expected)


def test_min_bet_floor_defaults_off_so_stakes_are_never_rounded_up():
    """The live path floors tiny bets at $1. In a backtest that would invent
    exposure the strategy never asked for, so the floor defaults to 0."""
    spec = {"sizing": {"method": "kelly", "fraction": 0.001}}
    stake = interpret.size(spec, {"model_prob": 0.51, "price": 0.50}, 100.0)
    assert 0 < stake < 1.0


def test_min_edge_defaults_off_so_entry_conditions_are_the_only_gate():
    """A hidden sizing threshold would veto entries the spec said to take."""
    spec = {"sizing": {"method": "kelly", "fraction": 1.0}}
    # edge of 0.01 is below wc/lib/kelly.py's live min_edge default of 0.03
    assert interpret.size(spec, {"model_prob": 0.51, "price": 0.50}, 1000.0) > 0


def test_tvm_discount_is_opt_in_and_shrinks_long_dated_stakes():
    ctx = {"model_prob": 0.6, "price": 0.5, "days_to_resolution": 90.0}
    plain = interpret.size({"sizing": {"method": "kelly", "fraction": 1.0}},
                           ctx, 1000.0)
    discounted = interpret.size(
        {"sizing": {"method": "kelly", "fraction": 1.0, "tvm_rate": 0.08}},
        ctx, 1000.0)
    assert plain == pytest.approx(200.0)          # unchanged default
    assert discounted < plain
