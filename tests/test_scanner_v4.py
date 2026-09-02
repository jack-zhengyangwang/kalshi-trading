"""
tests/test_scanner.py — v4 league routing and winner-only filtering.
"""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from wc.scanner import league_from_game_series


class TestLeagueRouting(unittest.TestCase):
    def test_known_leagues(self):
        cases = {
            "KXEPLGAME": "EPL",
            "KXLALIGAGAME": "LaLiga",
            "KXSERIEAGAME": "SerieA",
            "KXBUNDESLIGAGAME": "Bundesliga",
            "KXBLGAME": "Bundesliga",
            "KXLIGUE1GAME": "Ligue1",
        }
        for series, expected in cases.items():
            self.assertEqual(league_from_game_series(series), expected,
                             f"{series} -> {expected}")

    def test_unknown_falls_to_other(self):
        unknowns = [
            "KXMLSGAME",        # MLS
            "KXUCLGAME",        # Champions League
            "KXWCGAME",         # World Cup
            "KXEREDIVISIEGAME", # Eredivisie
            "KXCHAMPIONSHIPGAME",
            "KXUNKNOWNGAME",
        ]
        for series in unknowns:
            self.assertEqual(league_from_game_series(series), "Other",
                             f"{series} -> Other")

    def test_non_game_series(self):
        # Non-GAME series are not league-mapped (caller shouldn't pass them)
        self.assertEqual(league_from_game_series("KXEPLTOTAL"), "Other")
        self.assertEqual(league_from_game_series("KXEPLSPREAD"), "Other")
