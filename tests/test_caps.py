"""Safety caps must fail CLOSED.

Regression cover for the two-month fail-open: `master.get(key, 0.0)` guarded by
`if cap and ...` meant a missing key gave 0.0, which is falsy, so the guard was
skipped and real orders ran with no cap at all.
"""
import pytest

from wc.lib.caps import read_cap, check_caps


@pytest.mark.parametrize("master, expected", [
    ({"daily_cap_dollars": 200.0}, (200.0, True)),
    ({"daily_cap_dollars": 1}, (1.0, True)),
    ({"daily_cap_dollars": "150"}, (150.0, True)),
])
def test_usable_caps_are_ok(master, expected):
    assert read_cap(master, "daily_cap_dollars") == expected


@pytest.mark.parametrize("master", [
    {},                                  # the actual bug: key absent entirely
    {"daily_cap_dollars": 0},            # zero is not "unlimited"
    {"daily_cap_dollars": 0.0},
    {"daily_cap_dollars": -5},           # negative is nonsense
    {"daily_cap_dollars": None},
    {"daily_cap_dollars": ""},
    {"daily_cap_dollars": "unlimited"},
])
def test_unusable_caps_fail_closed(master):
    val, ok = read_cap(master, "daily_cap_dollars")
    assert ok is False, "an unusable cap must not report ok"
    assert val == 0.0


def test_zero_is_not_falsy_disabled():
    """The old code read 0.0 and treated it as 'no cap configured, skip check'.
    A cap of zero must instead be rejected, never interpreted as unlimited."""
    _, ok = read_cap({"per_game_cap_dollars": 0.0}, "per_game_cap_dollars")
    assert ok is False


def test_check_caps_reports_every_missing_key():
    master = {"daily_cap_dollars": 200.0}
    values, missing = check_caps(master, ["daily_cap_dollars", "per_game_cap_dollars"])
    assert values["daily_cap_dollars"] == 200.0
    assert missing == ["per_game_cap_dollars"]


def test_check_caps_empty_missing_when_all_present():
    master = {"daily_cap_dollars": 200.0, "per_game_cap_dollars": 50.0}
    _, missing = check_caps(master, ["daily_cap_dollars", "per_game_cap_dollars"])
    assert missing == []


def test_shipped_switchboard_has_usable_caps():
    """The committed config must not reintroduce the missing-key bug."""
    import json
    from wc import paths
    master = json.load(open(paths.SWITCHBOARD_V3))["master"]
    _, missing = check_caps(master, ["daily_cap_dollars", "per_game_cap_dollars"])
    assert missing == [], f"switchboard is missing usable caps: {missing}"
