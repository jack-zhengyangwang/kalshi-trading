"""Portfolio managers: independent capital, independent track record.

A PM is what the firm promotes or fires, so these tests are about its
definition being unambiguous at load time and its book behaving like a desk —
independent traders, one risk limit.
"""
import json

# Every shipped desk starts here. Change it in every config/pms/*.json at once.
FIRM_STAKE = 500.0

import pytest

from wc.backtest import engine
from wc.firm import pm as pm_mod

SPEC_PATH = "strategies/agents/edge-taker.json"


def cfg(**over):
    c = {
        "name": "desk", "view": {"view": "market"}, "bankroll": 1000.0,
        "agents": [{"name": "a1", "spec": SPEC_PATH}],
    }
    c.update(over)
    return c


def write(tmp_path, c):
    p = tmp_path / f"{c.get('name', 'pm')}.json"
    p.write_text(json.dumps(c))
    return str(p)


# ── validation: unambiguous at load, never at trade time ──────────────────────

@pytest.mark.parametrize("field", ["name", "view", "agents", "bankroll"])
def test_missing_required_field_rejected(field):
    c = cfg()
    del c[field]
    with pytest.raises(pm_mod.PMError, match=field):
        pm_mod.validate(c)


def test_a_pm_with_no_agents_cannot_trade():
    with pytest.raises(pm_mod.PMError, match="cannot trade"):
        pm_mod.validate(cfg(agents=[]))


@pytest.mark.parametrize("bad", [0, -100, "1000", True])
def test_bankroll_must_be_a_positive_number(bad):
    with pytest.raises(pm_mod.PMError, match="bankroll"):
        pm_mod.validate(cfg(bankroll=bad))


def test_duplicate_agent_names_rejected():
    """Per-agent P&L would be indistinguishable, which defeats the point of a
    desk breakdown."""
    with pytest.raises(pm_mod.PMError, match="duplicate agent name"):
        pm_mod.validate(cfg(agents=[{"name": "x", "spec": SPEC_PATH},
                                    {"name": "x", "spec": SPEC_PATH}]))


def test_weighted_allocation_requires_weights():
    with pytest.raises(pm_mod.PMError, match="positive 'weight'"):
        pm_mod.validate(cfg(allocation="weighted",
                            agents=[{"name": "a", "spec": SPEC_PATH}]))


def test_unknown_allocation_rejected():
    with pytest.raises(pm_mod.PMError, match="allocation"):
        pm_mod.validate(cfg(allocation="by_vibes"))


def test_unknown_view_is_caught_at_load(tmp_path):
    with pytest.raises(pm_mod.PMError, match="unknown view"):
        pm_mod.load(write(tmp_path, cfg(view={"view": "crystal_ball"})))


def test_a_broken_agent_spec_names_the_agent(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "b", "side": "yes"}))    # no caps
    with pytest.raises(pm_mod.PMError, match="broken"):
        pm_mod.load(write(tmp_path, cfg(
            agents=[{"name": "broken", "spec": str(bad)}])))


# ── capital ───────────────────────────────────────────────────────────────────

def test_equal_allocation_splits_evenly(tmp_path):
    m = pm_mod.load(write(tmp_path, cfg(agents=[
        {"name": "a", "spec": SPEC_PATH}, {"name": "b", "spec": SPEC_PATH},
        {"name": "c", "spec": SPEC_PATH}])))
    assert [round(a.bankroll) for a in m.agents] == [333, 333, 333]


def test_weighted_allocation_follows_the_weights(tmp_path):
    m = pm_mod.load(write(tmp_path, cfg(allocation="weighted", agents=[
        {"name": "a", "spec": SPEC_PATH, "weight": 3.0},
        {"name": "b", "spec": SPEC_PATH, "weight": 1.0}])))
    assert [round(a.bankroll) for a in m.agents] == [750, 250]


def test_allocation_never_exceeds_the_pm_bankroll(tmp_path):
    m = pm_mod.load(write(tmp_path, cfg(allocation="weighted", agents=[
        {"name": "a", "spec": SPEC_PATH, "weight": 99.0},
        {"name": "b", "spec": SPEC_PATH, "weight": 1.0}])))
    assert sum(a.bankroll for a in m.agents) == pytest.approx(m.bankroll)


# ── lookahead is surfaced, not left to be known ───────────────────────────────

def test_a_structural_pm_declares_its_lookahead(tmp_path):
    m = pm_mod.load(write(tmp_path, cfg(view={"view": "structural"})))
    assert m.carries_lookahead is True


def test_an_elo_pm_does_not(tmp_path):
    m = pm_mod.load(write(tmp_path, cfg(view={"view": "elo"})))
    assert m.carries_lookahead is False


# ── the shipped firm ──────────────────────────────────────────────────────────

def test_every_shipped_pm_loads():
    firm = pm_mod.load_all()
    assert firm, "config/pms/ is empty"
    for m in firm:
        assert m.agents and m.bankroll > 0


def test_the_firm_has_a_do_nothing_baseline():
    """Gate 2.5 needs something to beat. Without it, 'this PM made money' has
    no reference point."""
    assert any(m.view.name == "market" for m in pm_mod.load_all())


def test_the_firm_holds_more_than_one_view():
    """The whole point. PMs sharing one view are one opinion sized several
    ways, and ranking them is ranking risk-parameter luck."""
    assert len({m.view.name for m in pm_mod.load_all()}) >= 3


def test_every_shipped_pm_starts_with_the_same_bankroll():
    """The leaderboard is a race. A desk with more capital can post a larger
    dollar P&L without being better, so every shipped PM starts with the
    firm's standard stake — and a config that gives one desk more fails here
    rather than quietly winning."""
    rolls = {m.name: m.bankroll for m in pm_mod.load_all()}
    assert set(rolls.values()) == {FIRM_STAKE}, rolls
