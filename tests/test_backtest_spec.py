"""The DSL validator: malformed specs must be rejected at LOAD time.

The whole safety argument for the DSL is that a bad spec fails validation
rather than executing something surprising. These tests are that argument.
"""
import json

import pytest

from wc.backtest.spec import SpecError, load, validate


def base():
    return {
        "name": "t", "side": "yes",
        "entry": {"all": [{"signal": "price", "op": "lt", "value": 0.10}]},
        "sizing": {"method": "fixed", "dollars": 5.0},
        "exit": {"any": [{"signal": "days_to_resolution", "op": "lt", "value": 1}]},
        "caps": {"daily_spend_dollars": 50.0, "per_market_dollars": 5.0,
                 "total_exposure_dollars": 200.0},
    }


def test_baseline_spec_isbase():
    assert validate(base())["name"] == "t"


@pytest.mark.parametrize("field", ["name", "side", "entry", "sizing", "exit", "caps"])
def test_missing_required_field_rejected(field):
    s = base(); del s[field]
    with pytest.raises(SpecError, match=field):
        validate(s)


def test_unknown_signal_rejected():
    s = base()
    s["entry"] = {"all": [{"signal": "gamma_exposure", "op": "gt", "value": 1}]}
    with pytest.raises(SpecError, match="unknown signal"):
        validate(s)


def test_unknown_operator_rejected():
    s = base()
    s["entry"] = {"all": [{"signal": "price", "op": "approximately", "value": 0.1}]}
    with pytest.raises(SpecError, match="unknown operator"):
        validate(s)


def test_bad_side_rejected():
    s = base(); s["side"] = "maybe"
    with pytest.raises(SpecError, match="side"):
        validate(s)


def test_empty_entry_rejected():
    """An empty entry would trade everything."""
    s = base(); s["entry"] = {}
    with pytest.raises(SpecError, match="entry"):
        validate(s)


@pytest.mark.parametrize("cap", ["daily_spend_dollars", "per_market_dollars",
                                 "total_exposure_dollars"])
def test_caps_are_mandatory(cap):
    s = base(); del s["caps"][cap]
    with pytest.raises(SpecError, match=cap):
        validate(s)


@pytest.mark.parametrize("bad", [0, 0.0, -1, "50", None, True])
def test_non_positive_cap_rejected(bad):
    """Consistent with wc/lib/caps.py: a cap that is zero or missing must
    block, never silently disable the check."""
    s = base(); s["caps"]["daily_spend_dollars"] = bad
    with pytest.raises(SpecError, match="daily_spend_dollars"):
        validate(s)


@pytest.mark.parametrize("frac", [0, -0.1, 1.5, "0.25"])
def test_kelly_fraction_range_enforced(frac):
    s = base(); s["sizing"] = {"method": "kelly", "fraction": frac}
    with pytest.raises(SpecError, match="fraction"):
        validate(s)


def test_position_signals_rejected_in_entry():
    """unrealized_pnl_pct is meaningless without an open position."""
    s = base()
    s["entry"] = {"all": [{"signal": "unrealized_pnl_pct", "op": "lt", "value": -0.5}]}
    with pytest.raises(SpecError, match="exit-only"):
        validate(s)


def test_position_signals_allowed_in_exit():
    s = base()
    s["exit"] = {"any": [{"signal": "unrealized_pnl_pct", "op": "lt", "value": -0.5}]}
    assert validate(s)


def test_signal_out_of_range_rejected():
    s = base()
    s["entry"] = {"all": [{"signal": "price", "op": "gt", "value": 2.0}]}
    with pytest.raises(SpecError, match="maximum"):
        validate(s)


def test_between_requires_ordered_pair():
    s = base()
    s["entry"] = {"all": [{"signal": "price", "op": "between", "value": [0.9, 0.1]}]}
    with pytest.raises(SpecError, match="low must be"):
        validate(s)


def test_no_escape_hatch_in_vocabulary():
    """The DSL must never gain a field that executes a string. If this fails,
    every safety argument in docs/backtester/03_STRATEGY_DSL.md collapses."""
    from wc.backtest import spec as m
    forbidden = {"eval", "exec", "python", "code", "lambda", "callback", "custom"}
    for name in list(m.SIGNALS) + list(m.POSITION_SIGNALS) + list(m.OPS):
        assert not any(f in name.lower() for f in forbidden), name


def test_shipped_strategies_all_validate():
    """Every committed strategy must load. Guards against a bad commit."""
    import glob
    from wc import paths
    import os
    files = glob.glob(os.path.join(paths.ROOT, "strategies", "*.json"))
    assert files, "no strategies committed"
    for f in files:
        assert load(f)["name"]


def test_invalid_json_names_the_file(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json")
    with pytest.raises(SpecError, match="invalid JSON"):
        load(str(p))


def test_unknown_sizing_key_rejected():
    """A typo'd knob must fail loudly, not silently do nothing."""
    s = base()
    s["sizing"] = {"method": "kelly", "fraction": 0.25, "tvm_rte": 0.08}
    with pytest.raises(SpecError, match="unknown sizing key"):
        validate(s)


def test_kelly_passthrough_keys_accepted():
    s = base()
    s["sizing"] = {"method": "kelly", "fraction": 0.25, "min_edge": 0.03,
                   "min_bet_dollars": 1.0, "tvm_rate": 0.08}
    assert validate(s) is s


def test_universe_leg_is_known_and_closed():
    ok = dict(base(), universe={"series": ["*"], "leg": "draw"})
    validate(ok)
    for bad in ("tie", "Draw", 3):
        with pytest.raises(SpecError):
            validate(dict(base(), universe={"leg": bad}))
