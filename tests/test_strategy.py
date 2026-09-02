"""
tests/test_strategy.py — Strategy v3 sizing edge cases.
"""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import wc.strategy as s3


class TestEntrySize(unittest.TestCase):
    def test_price_band_rejects_longshot(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.02, "kelly_fraction": 0.25,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.15, "ask": 10, "in_play": False}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_price_band_rejects_near_lock(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.02, "kelly_fraction": 0.25,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.95, "ask": 95, "in_play": False}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_inplay_rejected_when_inplay_flag_off(self):
        s = s3.Strategist({"pregame": 1, "inplay": 0, "price_floor": 0.12,
                           "price_ceiling": 0.92, "min_edge": 0.02,
                           "kelly_fraction": 0.25, "unit_cap_frac": 0.05,
                           "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.60, "ask": 50, "in_play": True}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_pregame_rejected_when_pregame_flag_off(self):
        s = s3.Strategist({"pregame": 0, "inplay": 1, "price_floor": 0.12,
                           "price_ceiling": 0.92, "min_edge": 0.02,
                           "kelly_fraction": 0.25, "unit_cap_frac": 0.05,
                           "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.60, "ask": 50, "in_play": False}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_edge_below_min_edge_rejected(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.05, "kelly_fraction": 0.25,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.53, "ask": 50, "in_play": False}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_market_focus_filters(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.02, "kelly_fraction": 0.25,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0},
                          market_focus=["total"])
        bet = s.entry_size({"p_fair": 0.60, "ask": 50, "in_play": False,
                            "type": "winner"}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_market_focus_allows_match(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.02, "kelly_fraction": 0.25,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0},
                          market_focus=["total"])
        bet = s.entry_size({"p_fair": 0.70, "ask": 60, "in_play": False,
                            "type": "total"}, balance=100)
        self.assertGreater(bet, 0.0)

    def test_unit_cap_binds(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.02, "kelly_fraction": 0.99,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.80, "ask": 50, "in_play": False}, balance=100)
        self.assertLessEqual(bet, 5.01)

    def test_zero_balance(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.02, "kelly_fraction": 0.25,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.70, "ask": 50, "in_play": False}, balance=0)
        self.assertEqual(bet, 0.0)

    def test_scalp_gating_early(self):
        s = s3.Strategist({"pregame": 0, "inplay": 1, "scalp": 1,
                           "scalp_min_minute": 70, "scalp_min_prob": 0.85,
                           "price_floor": 0.12, "price_ceiling": 0.95,
                           "min_edge": 0.01, "kelly_fraction": 0.2,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.90, "ask": 85, "in_play": True,
                            "minute": 50}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_scalp_gating_low_prob(self):
        s = s3.Strategist({"pregame": 0, "inplay": 1, "scalp": 1,
                           "scalp_min_minute": 70, "scalp_min_prob": 0.85,
                           "price_floor": 0.12, "price_ceiling": 0.95,
                           "min_edge": 0.01, "kelly_fraction": 0.2,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.70, "ask": 65, "in_play": True,
                            "minute": 75}, balance=100)
        self.assertEqual(bet, 0.0)

    def test_scalp_enters(self):
        s = s3.Strategist({"pregame": 0, "inplay": 1, "scalp": 1,
                           "scalp_min_minute": 70, "scalp_min_prob": 0.85,
                           "price_floor": 0.12, "price_ceiling": 0.95,
                           "min_edge": 0.01, "kelly_fraction": 0.2,
                           "unit_cap_frac": 0.05, "alloc_tilt": 1.0})
        bet = s.entry_size({"p_fair": 0.90, "ask": 85, "in_play": True,
                            "minute": 75}, balance=100)
        self.assertGreater(bet, 0.0)

    def test_tau_discount(self):
        s = s3.Strategist({"pregame": 1, "price_floor": 0.12, "price_ceiling": 0.92,
                           "min_edge": 0.02, "kelly_fraction": 0.25,
                           "unit_cap_frac": 0.10, "alloc_tilt": 1.0,
                           "tvm_rate": 0.5})
        bet_soon = s.entry_size({"p_fair": 0.70, "ask": 50, "in_play": False},
                                balance=100, tau_days=1)
        bet_far = s.entry_size({"p_fair": 0.70, "ask": 50, "in_play": False},
                               balance=100, tau_days=30)
        self.assertLess(bet_far, bet_soon)
