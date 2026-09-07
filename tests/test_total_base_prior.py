"""The totals base-rate prior is per-league config, not a hardcoded WC constant.

Regression cover for 2026-09-07: `WC_OVER_BASE` held 2026 World Cup group-stage
rates and `WC_BASE_WEIGHT = 0.25` was applied to every league's totals, even
though v4 has no World Cup league at all.
"""
import math

import pytest

import wc.arena as arena
import wc.brain_v4 as b4


FULL_TOTAL = {"type": "total", "period": "full", "threshold": 2.5}


def test_no_adjustment_when_league_has_no_configured_rates():
    """Default for every shipped league: the prior is a no-op."""
    assert arena._total_base_pf(FULL_TOTAL, "Over 2.5", 0.50, league="EPL") == 0.50


def test_no_adjustment_without_a_league():
    assert arena._total_base_pf(FULL_TOTAL, "Over 2.5", 0.50) == 0.50


def test_no_shipped_league_configures_totals_rates():
    """Guards against someone reintroducing WC rates as a global default."""
    for name, cfg in b4.LEAGUES.items():
        if name.startswith("_"):
            continue
        assert not cfg.get("total_over_base"), \
            f"{name} ships totals base rates; they must be measured, not assumed"


def test_configured_league_blends_in_logit_space(monkeypatch):
    cfg = dict(b4.LEAGUES["EPL"])
    cfg["total_over_base"] = {"2.5": 0.58}
    cfg["total_base_weight"] = 0.25
    monkeypatch.setitem(b4.LEAGUES, "EPL", cfg)

    got = arena._total_base_pf(FULL_TOTAL, "Over 2.5", 0.50, league="EPL")

    w, p, base = 0.25, 0.50, 0.58
    lg = (1 - w) * math.log(p / (1 - p)) + w * math.log(base / (1 - base))
    assert got == pytest.approx(1 / (1 + math.exp(-lg)))


def test_under_uses_complement(monkeypatch):
    cfg = dict(b4.LEAGUES["EPL"])
    cfg["total_over_base"] = {"2.5": 0.58}
    monkeypatch.setitem(b4.LEAGUES, "EPL", cfg)

    over = arena._total_base_pf(FULL_TOTAL, "Over 2.5", 0.50, league="EPL")
    under = arena._total_base_pf(FULL_TOTAL, "Under 2.5", 0.50, league="EPL")
    assert over > 0.50 > under


def test_non_total_legs_untouched(monkeypatch):
    cfg = dict(b4.LEAGUES["EPL"])
    cfg["total_over_base"] = {"2.5": 0.58}
    monkeypatch.setitem(b4.LEAGUES, "EPL", cfg)

    winner = {"type": "winner", "period": "full", "threshold": None}
    assert arena._total_base_pf(winner, "Home", 0.50, league="EPL") == 0.50


def test_half_totals_untouched(monkeypatch):
    """1H/2H totals must not get a full-match prior — that caused the -$25 bleed."""
    cfg = dict(b4.LEAGUES["EPL"])
    cfg["total_over_base"] = {"2.5": 0.58}
    monkeypatch.setitem(b4.LEAGUES, "EPL", cfg)

    half = {"type": "total", "period": "1H", "threshold": 2.5}
    assert arena._total_base_pf(half, "Over 2.5", 0.50, league="EPL") == 0.50
