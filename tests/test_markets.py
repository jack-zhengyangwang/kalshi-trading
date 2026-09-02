"""
tests/test_markets.py — Parser coverage + fair_yes_v2 smoke tests.
"""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import wc.markets as mv


class TestParseMarket(unittest.TestCase):
    def test_winner_home(self):
        r = mv.parse_market_v2("KXWCGAME-26JUN11KORCZE-CZE", "Czechia")
        self.assertEqual(r["type"], "winner")
        self.assertEqual(r["period"], "full")
        self.assertEqual(r["team"], "Czechia")

    def test_winner_tie(self):
        r = mv.parse_market_v2("KXWCGAME-26JUN11KORCZE-TIE", "Tie")
        self.assertEqual(r["type"], "winner")
        self.assertIsNone(r["team"])

    def test_spread(self):
        r = mv.parse_market_v2("KXWCSPREAD-26JUN11KORCZE-KOR2", "Korea Republic wins by more than 1.5")
        self.assertEqual(r["type"], "spread")
        self.assertEqual(r["line"], 1.5)

    def test_total(self):
        r = mv.parse_market_v2("KXWCTOTAL-26JUN11KORCZE-O25", "Over 2.5 goals")
        self.assertEqual(r["type"], "total")
        self.assertEqual(r["period"], "full")
        self.assertEqual(r["line"], 2.5)

    def test_total_1h(self):
        r = mv.parse_market_v2("KXWC1HTOTAL-26JUN11KORCZE-O15", "Over 1.5 goals")
        self.assertEqual(r["type"], "total")
        self.assertEqual(r["period"], "1H")

    def test_btts(self):
        r = mv.parse_market_v2("KXWCBTTS-26JUN11KORCZE-BTTS", "Both Teams To Score")
        self.assertEqual(r["type"], "btts")

    def test_team_total(self):
        r = mv.parse_market_v2("KXWCTEAMTOTAL-26JUN11KORCZE-CZE2", "Czechia over 1.5 goals")
        self.assertEqual(r["type"], "team_total")
        self.assertEqual(r["line"], 1.5)

    def test_corners(self):
        r = mv.parse_market_v2("KXWCCORNERS-26JUN11KORCZE-9", "9+ corners")
        self.assertEqual(r["type"], "corners")
        self.assertEqual(r["threshold"], 9)

    def test_team_corners(self):
        r = mv.parse_market_v2("KXWCTCORNERS-26JUN11KORCZE-CZE5", "Czechia: 5+")
        self.assertEqual(r["type"], "team_corners")
        self.assertEqual(r["threshold"], 5)
        self.assertEqual(r["team"], "Czechia")

    def test_score(self):
        r = mv.parse_market_v2("KXWCSCORE-26JUN11KORCZE-2-1", "Czechia wins 2-1")
        self.assertEqual(r["type"], "score")
        self.assertIsNotNone(r["score"])
        self.assertEqual(r["score"]["a"], 2)
        self.assertEqual(r["score"]["b"], 1)

    def test_advance(self):
        r = mv.parse_market_v2("KXWCADVANCE-26JUN11KORCZE-CZE", "Czechia advances")
        self.assertEqual(r["type"], "advance")
        self.assertEqual(r["team"], "Czechia")

    def test_win_method(self):
        r = mv.parse_market_v2("KXWCMOV-26JUN11KORCZE-CZE-REG", "Czechia to win in Regulation Time")
        self.assertEqual(r["type"], "win_method")
        self.assertEqual(r["phase"], "reg")

    def test_first_goalscorer(self):
        r = mv.parse_market_v2("KXWCFIRSTGOAL-26JUN11KORCZE-SON", "Son Heung-min")
        self.assertEqual(r["type"], "first_goalscorer")

    def test_unknown_series(self):
        r = mv.parse_market_v2("KXWCUNKNOWN-26JUN11KORCZE-X", "something")
        self.assertEqual(r["type"], "unknown")


class TestFairYes(unittest.TestCase):
    def test_total_over_likely(self):
        p = mv.fair_yes_v2({"type": "total", "period": "full", "line": 0.5},
                           mu_home=1.3, mu_away=1.3)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.85)

    def test_total_over_unlikely(self):
        p = mv.fair_yes_v2({"type": "total", "period": "full", "line": 5.5},
                           mu_home=1.0, mu_away=1.0)
        self.assertIsNotNone(p)
        self.assertLess(p, 0.10)

    def test_btts_moderate(self):
        p = mv.fair_yes_v2({"type": "btts", "period": "full", "line": None,
                            "threshold": None, "team": None, "score": None, "phase": None},
                           mu_home=1.3, mu_away=1.3)
        self.assertIsNotNone(p)
        self.assertTrue(0.4 < p < 0.7)

    def test_winner_1h_tie(self):
        p = mv.fair_yes_v2({"type": "winner", "period": "1H", "team": None,
                            "line": None, "threshold": None, "score": None, "phase": None},
                           mu_home=0.6, mu_away=0.6)
        self.assertIsNotNone(p)
        self.assertTrue(0.3 < p < 0.7)

    def test_spread_home_favorite(self):
        p = mv.fair_yes_v2({"type": "spread", "period": "full", "line": 0.5},
                           mu_home=1.8, mu_away=0.6, leg_is_home=True)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.5)

    def test_corners_atleast(self):
        p = mv.fair_yes_v2({"type": "corners", "period": "full", "threshold": 9,
                            "line": None, "team": None, "score": None, "phase": None},
                           mu_home=1.0, mu_away=1.0)
        self.assertIsNotNone(p)
        self.assertTrue(0.3 < p < 0.8)

    def test_advance_favorite(self):
        p = mv.fair_yes_v2({"type": "advance", "period": "full",
                            "line": None, "threshold": None, "team": None, "score": None, "phase": None},
                           mu_home=2.0, mu_away=0.5, leg_is_home=True)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.65)
