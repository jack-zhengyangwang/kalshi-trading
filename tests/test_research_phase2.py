"""v2 Phase 2: Gate 2 by league tier and entry time."""
import numpy as np

from wc.research import phase2 as P2


def test_tiers():
    for lg in ("EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "UCL"):
        assert P2.tier(lg) == "T1"
    for lg in ("Championship", "Bundesliga2", "BrasileiroB", "BrasileiroC", "ArgNacionalB",
               "SerieB", "SerieC", "LaLiga2", "Ligue2"):
        assert P2.tier(lg) == "T3"
    for lg in ("ArgPrimera", "BrasileiroA", "MLS", "Allsvenskan", "ChinaSL", "Other", None):
        assert P2.tier(lg) == "T2"


def _legs(n, informative, seed):
    rng = np.random.default_rng(seed)
    truth = rng.uniform(0.1, 0.9, n)
    y = (rng.uniform(size=n) < truth).astype(float)
    market = np.full(n, 0.5) if informative else np.clip(truth + rng.normal(0, .05, n), .02, .98)
    model = truth if informative else rng.uniform(0.1, 0.9, n)
    return model, market, y


def test_group_gates_judge_only_big_enough_groups():
    m1, k1, y1 = _legs(2000, True, 0)
    m2, k2, y2 = _legs(100, True, 1)
    groups = ["T1"] * 2000 + ["T3"] * 100
    games = [f"g{i}" for i in range(2100)]
    r = P2.group_gates(np.r_[m1, m2], np.r_[k1, k2], np.r_[y1, y2], games, groups,
                       min_legs=150, draws=200)
    assert r["T1"]["judged"] and r["T1"]["gate2"]["passes"]
    assert not r["T3"]["judged"] and r["T3"]["gate2"] is None
    assert r["T3"]["legs"] == 100


def test_signal_needs_two_entry_times():
    res = {"B|T1|24": {"judged": True, "gate2": {"passes": True}},
           "B|T1|6": {"judged": True, "gate2": {"passes": True}},
           "B|T1|1": {"judged": True, "gate2": {"passes": False}},
           "B|T2|24": {"judged": True, "gate2": {"passes": True}},
           "B|T2|6": {"judged": True, "gate2": {"passes": False}},
           "B|T3|24": {"judged": False, "gate2": None}}
    assert P2.signals(res) == ["T1"]
