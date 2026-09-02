"""
tests/test_elo_index.py — EloIndex loading, resolution, and fuzzy matching.
"""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from wc.lib.elo_index import EloIndex


class TestEloIndexLoading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ei = EloIndex()

    def test_loads_teams(self):
        self.assertGreater(len(self.ei.team_elo), 100,
                           "Should have 100+ teams (national + club)")

    def test_national_teams(self):
        self.assertIn("Argentina", self.ei.team_elo)
        self.assertIn("Brazil", self.ei.team_elo)
        self.assertIn("England", self.ei.team_elo)

    def test_club_teams(self):
        self.assertIn("Arsenal", self.ei.team_elo)
        self.assertIn("Barcelona", self.ei.team_elo)


class TestEloResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ei = EloIndex()

    def test_exact_match(self):
        self.assertEqual(self.ei.elo("Arsenal"), 2063.8)
        self.assertEqual(self.ei.elo("Barcelona"), 1951.6)

    def test_alias_match(self):
        # Kalshi national-team aliases
        self.assertEqual(self.ei.elo("Czechia"),
                         self.ei.team_elo.get("Czech Republic"))
        self.assertEqual(self.ei.elo("USA"),
                         self.ei.team_elo.get("USA"))

    def test_club_alias_match(self):
        # Club alias table
        self.assertEqual(self.ei.elo("Man City"),
                         self.ei.team_elo.get("Man City"))
        self.assertEqual(self.ei.elo("PSG"),
                         self.ei.team_elo.get("Paris SG"))

    def test_fuzzy_match(self):
        # difflib close-match fallback
        man_utd = self.ei.elo("Manchester United")
        self.assertIsNotNone(man_utd)
        # "Man Utd" should fuzzy-match to something close
        self.assertGreater(man_utd, 1500)

    def test_unknown_team(self):
        self.assertIsNone(self.ei.elo("NonexistentFC"))
        self.assertIsNone(self.ei.elo(""))

    def test_elo_diff(self):
        diff = self.ei.elo_diff("Manchester City", "Arsenal")
        self.assertLess(diff, 0)   # MCI < ARS in clubelo

    def test_elo_diff_unknown(self):
        self.assertEqual(self.ei.elo_diff("NonexistentFC", "Arsenal"), 0.0)
        self.assertEqual(self.ei.elo_diff("Arsenal", "NonexistentFC"), 0.0)
