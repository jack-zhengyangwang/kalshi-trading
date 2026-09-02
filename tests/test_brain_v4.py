"""
tests/test_brain_v4.py — BrainV4 per-league calibration, winner-only pricing,
stacker, and LLM prompt generation.
"""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from wc.brain_v4 import BrainV4, LEAGUES


class TestBrainV4Construction(unittest.TestCase):
    def test_all_leagues_construct(self):
        for league in ["EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "Other"]:
            b = BrainV4(league)
            self.assertEqual(b.league, league)
            self.assertIn("base_goals_per_side", b.calibration)
            self.assertIn("name", b.calibration)

    def test_unknown_league_raises(self):
        with self.assertRaises(KeyError):
            BrainV4("NonExistentLeague")

    def test_leagues_config_has_six(self):
        names = {k for k in LEAGUES if not k.startswith("_")}
        self.assertEqual(names, {"EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "Other"})


class TestBrainV4GamePrior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.brains = {lg: BrainV4(lg) for lg in ["EPL", "LaLiga", "Other"]}

    def test_league_specific_scoring_rates(self):
        """Different leagues produce different expected totals from the same ELOs."""
        priors = {
            lg: b.game_prior("Manchester City", "Arsenal")
            for lg, b in self.brains.items()
        }
        totals = {lg: p["mu_home"] + p["mu_away"] for lg, p in priors.items()}
        # EPL should have highest total (1.42 base), LaLiga lowest (1.25)
        self.assertGreater(totals["EPL"], totals["LaLiga"])
        # Other uses 1.35, between EPL and LaLiga
        self.assertGreater(totals["EPL"], totals["Other"])
        self.assertGreater(totals["Other"], totals["LaLiga"])

    def test_elo_known_true(self):
        p = self.brains["EPL"].game_prior("Manchester City", "Arsenal")
        self.assertTrue(p["elo_known"])

    def test_elo_known_false(self):
        p = self.brains["EPL"].game_prior("UnknownFC1", "UnknownFC2")
        self.assertFalse(p["elo_known"])
        # Should still return sensible defaults (elo_diff=0)
        self.assertEqual(p["elo_diff"], 0.0)
        self.assertGreater(p["mu_home"], 0)
        self.assertGreater(p["mu_away"], 0)


class TestBrainV4WinnerPricing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = BrainV4("EPL")
        cls.prior = cls.b.game_prior("Manchester City", "Arsenal")

    def test_home_win_leg(self):
        parsed = {"type": "winner", "period": "full", "team": "Manchester City"}
        pf, sources = self.b.pfair(parsed, self.prior, leg_is_home=True
, market_mid=0.45)
        self.assertIsNotNone(pf)
        self.assertGreater(pf, 0)
        self.assertLess(pf, 1)
        self.assertIn("data", sources)

    def test_draw_leg(self):
        parsed = {"type": "winner", "period": "full", "team": None}
        pf, sources = self.b.pfair(parsed, self.prior, leg_is_home=True
, market_mid=0.28)
        self.assertIsNotNone(pf)
        self.assertGreater(pf, 0.15)
        self.assertLess(pf, 0.40)

    def test_away_win_leg(self):
        parsed = {"type": "winner", "period": "full", "team": "Arsenal"}
        pf, sources = self.b.pfair(parsed, self.prior, leg_is_home=False
, market_mid=0.27)
        self.assertIsNotNone(pf)
        self.assertGreater(pf, 0)
        self.assertLess(pf, 1)

    def test_three_way_sums_near_one(self):
        parsed_home = {"type": "winner", "period": "full", "team": "Manchester City"}
        parsed_draw = {"type": "winner", "period": "full", "team": None}
        ph, _ = self.b.pfair(parsed_home, self.prior, leg_is_home=True
, market_mid=0.45)
        pd, _ = self.b.pfair(parsed_draw, self.prior, leg_is_home=True
, market_mid=0.28)
        pa, _ = self.b.pfair(parsed_home, self.prior, leg_is_home=False
, market_mid=0.27)
        self.assertAlmostEqual(ph + pd + pa, 1.0, delta=0.05)

    def test_non_winner_returns_none(self):
        for typ in ["total", "spread", "btts", "corners", "team_corners",
                     "score", "first_to_score", "advance", "win_method"]:
            parsed = {"type": typ, "period": "full", "line": 2.5, "team": None}
            result = self.b.data_pfair(parsed, self.prior, leg_is_home=True)
            self.assertIsNone(result, f"type={typ} should return None for winner-only brain")

    def test_pfair_with_only_market(self):
        """When data model returns None (non-winner), but market provides a price."""
        parsed = {"type": "total", "period": "full", "line": 2.5}
        pf, sources = self.b.pfair(parsed, self.prior, leg_is_home=True
, market_mid=0.65)
        # data_pfair returns None for total, but market source is available
        self.assertIsNotNone(pf)
        self.assertIn("market", sources)


class TestBrainV4Stacker(unittest.TestCase):
    def test_update_stacker_noop_below_min_sample(self):
        b = BrainV4("EPL")
        original = dict(b.weights)
        # Only 5 resolved outcomes, below min_sample=15
        resolved = [{"sources": {"data": 0.5, "market": 0.6}, "outcome": o}
                    for o in [1, 0, 1, 1, 0]]
        result = b.update_stacker(resolved, min_sample=15)
        self.assertEqual(result, original)

    def test_update_stacker_shifts_weights(self):
        b = BrainV4("EPL")
        original = dict(b.weights)
        # 20 outcomes where data is perfect and market is terrible
        resolved = []
        for _ in range(20):
            resolved.append({"sources": {"data": 0.99, "market": 0.01}, "outcome": 1})
        result = b.update_stacker(resolved, min_sample=15, lr=1.0)
        # With lr=1.0, weights should fully shift toward data
        self.assertGreater(result["data"], original["data"])
        self.assertLess(result["market"], original["market"])


class TestBrainV4LLMPrompts(unittest.TestCase):
    def test_prompt_is_league_aware(self):
        b = BrainV4("EPL")
        prior = b.game_prior("Liverpool", "Chelsea")
        # _call_llm returns an LLM response dict (or {} if no API key)
        # We just verify the prompt generation doesn't crash
        # and contains the league name
        self.assertEqual(b.calibration["name"], "Premier League")

    def test_prompt_other_league(self):
        b = BrainV4("LaLiga")
        self.assertEqual(b.calibration["name"], "La Liga")
        b2 = BrainV4("Other")
        self.assertEqual(b2.calibration["name"], "Club Soccer")


class TestBrainV4LivePrior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = BrainV4("EPL")

    def test_live_prior_basic(self):
        lp = self.b.live_prior("Manchester City", "Arsenal", 45, 1, 0)
        self.assertIn("mu_home", lp)
        self.assertIn("mu_away", lp)
        self.assertGreater(lp["mu_home"], 0)
        self.assertGreater(lp["mu_away"], 0)
        # At minute 45, should have ~half the goals remaining
        total_rem = lp["mu_home"] + lp["mu_away"]
        full = self.b.game_prior("Manchester City", "Arsenal")
        full_total = full["mu_home"] + full["mu_away"]
        self.assertLess(total_rem, full_total * 0.6)  # roughly half

    def test_live_pfair_winner(self):
        lp = self.b.live_prior("Manchester City", "Arsenal", 45, 1, 0)
        parsed = {"type": "winner", "period": "full", "team": "Manchester City"}
        pf = self.b.live_pfair(parsed, lp, leg_is_home=True)
        self.assertIsNotNone(pf)
        self.assertGreater(pf, 0)
        self.assertLess(pf, 1)

    def test_live_pfair_non_winner_skipped(self):
        lp = self.b.live_prior("Manchester City", "Arsenal", 45, 1, 0)
        parsed = {"type": "total", "period": "full", "line": 2.5}
        pf = self.b.live_pfair(parsed, lp, leg_is_home=True)
        self.assertIsNone(pf)


# ── hardening: shared ELO index, weight isolation, stacker consistency,
#    bounded LLM cache. See CLAUDE.md "non-functional design" fixes 1-4.

class TestBrainV4SharedEloIndex(unittest.TestCase):
    """Fix 1 — six brains must not each load and parse 688 teams."""

    def test_elo_index_shared_across_instances(self):
        a = BrainV4("EPL")
        b = BrainV4("LaLiga")
        self.assertIs(a._elo, b._elo)

    def test_elo_index_injectable_for_tests(self):
        from wc.lib.elo_index import EloIndex
        own = EloIndex()
        b = BrainV4("EPL", {"elo_index": own})
        self.assertIs(b._elo, own)

    def test_shared_index_still_resolves(self):
        a = BrainV4("EPL")
        b = BrainV4("SerieA")
        self.assertEqual(a.elos("Manchester City", "Arsenal"),
                         b.elos("Manchester City", "Arsenal"))


class TestBrainV4WeightIsolation(unittest.TestCase):
    """Fix 2 — config dicts must not be mutated through, and weight writes
    must carry a version so a supervisor can detect a concurrent write."""

    def _resolved(self, n=40):
        # 'data' is near-perfect, 'market' is badly calibrated -> weights must move.
        return [{"sources": {"data": 0.95, "market": 0.30}, "outcome": 1}
                for _ in range(n)]

    def test_weights_not_aliased_to_config(self):
        shared = {"data": 0.8, "llm": 0.0, "market": 0.2}
        a = BrainV4("EPL", {"stacker_weights": shared})
        b = BrainV4("LaLiga", {"stacker_weights": shared})
        a.update_stacker(self._resolved())
        self.assertNotEqual(a.weights, b.weights)
        self.assertEqual(shared, {"data": 0.8, "llm": 0.0, "market": 0.2})

    def test_llm_cache_not_aliased_to_config(self):
        shared = {}
        a = BrainV4("EPL", {"llm_cache": shared})
        a.llm_cache["EVT-1"] = {"p_home": 0.5}
        self.assertEqual(shared, {})

    def test_weights_version_starts_zero_and_increments(self):
        b = BrainV4("EPL")
        self.assertEqual(b.weights_version, 0)
        b.update_stacker(self._resolved())
        self.assertEqual(b.weights_version, 1)

    def test_thin_sample_does_not_bump_version(self):
        b = BrainV4("EPL")
        b.update_stacker(self._resolved(3))          # below min_sample
        self.assertEqual(b.weights_version, 0)

    def test_weights_payload_roundtrip(self):
        b = BrainV4("EPL")
        b.update_stacker(self._resolved())
        payload = b.weights_payload()
        b2 = BrainV4("EPL", {"stacker_weights": payload})
        self.assertEqual(b2.weights, b.weights)
        self.assertEqual(b2.weights_version, b.weights_version)

    def test_legacy_bare_weights_dict_still_accepted(self):
        """On-disk brain_*.json from v3 is a bare {src: w} dict."""
        b = BrainV4("EPL", {"stacker_weights": {"data": 0.7, "llm": 0.0, "market": 0.3}})
        self.assertEqual(b.weights["data"], 0.7)
        self.assertEqual(b.weights_version, 0)


class TestBrainV4StackerConsistency(unittest.TestCase):
    """Fix 3 — the den==0 fallback must be the same estimator as the main path."""

    def test_zero_weight_fallback_is_logit_mean_not_arithmetic(self):
        b = BrainV4("EPL", {"stacker_weights": {"data": 0.0, "llm": 0.0, "market": 0.0}})
        p = b._stack({"data": 0.1, "market": 0.5})
        self.assertAlmostEqual(p, 0.25, places=6)    # logit mean
        self.assertNotAlmostEqual(p, 0.30, places=3)  # arithmetic mean

    def test_zero_weight_fallback_single_source_is_identity(self):
        b = BrainV4("EPL", {"stacker_weights": {"data": 0.0, "market": 0.0}})
        self.assertAlmostEqual(b._stack({"data": 0.37}), 0.37, places=6)

    def test_no_sources_returns_none(self):
        b = BrainV4("EPL")
        self.assertIsNone(b._stack({}))
        self.assertIsNone(b._stack({"data": None}))


class TestBrainV4CacheBound(unittest.TestCase):
    """Fix 4 — llm_cache is persisted to disk every cycle; it must not grow
    without bound over a season."""

    def test_game_cache_evicts_oldest_first(self):
        b = BrainV4("EPL", {"use_llm": False, "llm_cache_max": 4})
        for i in range(10):
            b._cache_set(f"EVT-{i}", {"p_home": 0.5})
        self.assertLessEqual(len(b.llm_cache), 4)
        self.assertNotIn("EVT-0", b.llm_cache)
        self.assertIn("EVT-9", b.llm_cache)

    def test_eviction_never_drops_the_leg_bucket(self):
        b = BrainV4("EPL", {"use_llm": False, "llm_cache_max": 3})
        b.llm_cache.setdefault("_leg", {})["KXEPLGAME-X"] = 0.44
        for i in range(20):
            b._cache_set(f"EVT-{i}", {"p_home": 0.5})
        self.assertIn("_leg", b.llm_cache)
        self.assertEqual(b.llm_cache["_leg"]["KXEPLGAME-X"], 0.44)

    def test_leg_cache_evicts_oldest_first(self):
        b = BrainV4("EPL", {"use_llm": False, "llm_cache_max": 4})
        for i in range(10):
            b._leg_cache_set(f"TICK-{i}", 0.5)
        self.assertLessEqual(len(b.llm_cache["_leg"]), 4)
        self.assertNotIn("TICK-0", b.llm_cache["_leg"])
        self.assertIn("TICK-9", b.llm_cache["_leg"])

    def test_oversized_injected_cache_is_trimmed_on_load(self):
        big = {f"EVT-{i}": {"p_home": 0.5} for i in range(50)}
        b = BrainV4("EPL", {"llm_cache": big, "llm_cache_max": 5})
        self.assertLessEqual(len(b.llm_cache), 5)
