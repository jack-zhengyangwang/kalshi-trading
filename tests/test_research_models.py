"""v2 Phase 1: baseline models and the two gates."""
import math

import numpy as np
import pytest

from wc.research import models as M


def test_scores():
    P = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    assert M.leg_brier3(P, ["home", "away"]) == 0
    P = np.full((2, 3), 1 / 3)
    assert M.logloss3(P, ["home", "draw"]) == pytest.approx(math.log(3))
    assert M.leg_brier3(P, ["home", "draw"]) == pytest.approx((4 / 9 + 1 / 9 + 1 / 9) / 3)


def test_base_rate_model_uses_league_rates_and_sums_to_one():
    X = {"lg_home": np.array([0.5, np.nan]), "lg_draw": np.array([0.2, np.nan])}
    P = M.BaseRate(fallback=(0.45, 0.27, 0.28)).predict(X)
    assert P[0] == pytest.approx([0.5, 0.2, 0.3])
    assert P[1] == pytest.approx([0.45, 0.27, 0.28])


def test_sk_model_columns_are_home_draw_away_whatever_sklearn_sorts():
    rng = np.random.default_rng(0)
    n = 600
    x = rng.normal(size=n)
    y = np.where(x > 0.5, "home", np.where(x < -0.5, "away", "draw"))
    m = M.SkModel("lr", ["x"], C=1.0).fit({"x": x}, y)
    P = m.predict({"x": np.array([3.0, -3.0])})
    assert P.shape == (2, 3) and P.sum(axis=1) == pytest.approx([1, 1])
    assert P[0].argmax() == 0          # big x -> home
    assert P[1].argmax() == 2          # small x -> away


def test_sk_model_handles_missing_values():
    x = np.array([1.0, np.nan, -1.0, 2.0, np.nan, -2.0] * 50)
    y = np.array(["home", "draw", "away", "home", "draw", "away"] * 50)
    for kind in ("lr", "hgb"):
        P = M.SkModel(kind, ["x"]).fit({"x": x}, y).predict({"x": np.array([np.nan])})
        assert np.isfinite(P).all()


def test_gate2_finds_information_only_where_it_exists():
    rng = np.random.default_rng(1)
    n = 3000
    truth = rng.uniform(0.1, 0.9, n)
    y = (rng.uniform(size=n) < truth).astype(float)
    groups = np.arange(n)
    market = np.clip(truth + rng.normal(0, 0.05, n), 0.02, 0.98)
    noise = rng.uniform(0.1, 0.9, n)
    r = M.gate2(market, noise, y, groups, draws=200)
    assert r["ci99_model"][0] < 0 < r["ci99_model"][1]     # noise adds nothing
    assert not r["passes"]
    # a model that knows something the market doesn't
    market_bad = np.full(n, 0.5)
    r = M.gate2(market_bad, truth, y, groups, draws=200)
    assert r["passes"] and r["ci99_model"][0] > 0


def test_gate1_brier_difference_is_negative_when_model_is_better():
    rng = np.random.default_rng(2)
    n = 2000
    truth = rng.uniform(0.1, 0.9, n)
    y = (rng.uniform(size=n) < truth).astype(float)
    r = M.gate1(truth, np.full(n, 0.5), y, np.arange(n), draws=200)
    assert r["diff"] < 0 and r["passes"]
    r = M.gate1(np.full(n, 0.5), truth, y, np.arange(n), draws=200)
    assert r["diff"] > 0 and not r["passes"]
