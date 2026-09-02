"""
tests/test_brains.py — the v4 per-league brain-set lifecycle: construction,
persistence, migration off the v3 'all' state, and league-attributed training.

These cover the seam that wires BrainV4 into arena/cycle/promote: brains are
keyed by LEAGUE, and a settled bet must train the brain that priced it.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import wc.brains as bl


class TestLeagueKeys(unittest.TestCase):
    def test_doc_key_is_not_a_league(self):
        # leagues.json carries a "_doc" note; a brain built for it would blow up
        # on calibration["base_goals_per_side"] because its value is a string.
        self.assertNotIn("_doc", bl.LEAGUE_KEYS)
        self.assertEqual(set(bl.LEAGUE_KEYS),
                         {"EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "Other"})


class TestLeagueForTicker(unittest.TestCase):
    def test_maps_series_prefix(self):
        self.assertEqual(bl.league_for_ticker("KXEPLGAME-25AUG16ARSMCI-ARS"), "EPL")
        self.assertEqual(bl.league_for_ticker("KXLALIGAGAME-25AUG16RMABAR-RMA"), "LaLiga")
        self.assertEqual(bl.league_for_ticker("KXBUNDESLIGAGAME-25AUG16BAYDOR-BAY"), "Bundesliga")

    def test_unknown_and_empty_fall_to_other(self):
        # 'Other' is also where the scanner routed them, so a bet can never
        # train a brain that did not price it.
        self.assertEqual(bl.league_for_ticker("KXMLSGAME-25AUG16LAFCLAG-LAF"), "Other")
        self.assertEqual(bl.league_for_ticker(""), "Other")
        self.assertEqual(bl.league_for_ticker(None), "Other")


class TestNew(unittest.TestCase):
    def test_builds_one_brain_per_league_at_defaults(self):
        brains = bl.new()
        self.assertEqual(set(brains), set(bl.LEAGUE_KEYS))
        for lg, br in brains.items():
            self.assertEqual(br.league, lg)
            self.assertFalse(br.use_llm)
            self.assertEqual(br.weights_version, 0)

    def test_llm_flag_propagates(self):
        for br in bl.new(use_llm=True).values():
            self.assertTrue(br.use_llm)
            self.assertGreater(br.weights["llm"], 0)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_then_load_round_trips_weights(self):
        brains = bl.new()
        brains["EPL"].weights["data"] = 0.77
        bl.save(brains, state_dir=self.dir)

        payload = json.load(open(bl.weights_path("EPL", self.dir)))
        self.assertIn("weights", payload)          # versioned form, not a bare dict
        self.assertIn("version", payload)
        self.assertAlmostEqual(payload["weights"]["data"], 0.77)

        again = bl.load(use_llm=False, state_dir=self.dir)
        self.assertAlmostEqual(again["EPL"].weights["data"], 0.77)

    def test_load_on_empty_dir_gives_defaults(self):
        brains = bl.load(use_llm=False, state_dir=self.dir)
        self.assertEqual(set(brains), set(bl.LEAGUE_KEYS))
        self.assertEqual(brains["EPL"].weights_version, 0)

    def test_migrates_v3_all_weights_to_every_league(self):
        # v3 wrote one bare dict for the single 'all' category. First v4 load
        # must start warm from it rather than throwing the season away.
        legacy = {"data": 0.5, "llm": 0.3, "market": 0.2}
        json.dump(legacy, open(bl.weights_path("all", self.dir), "w"))
        brains = bl.load(use_llm=False, state_dir=self.dir)
        for lg in bl.LEAGUE_KEYS:
            self.assertAlmostEqual(brains[lg].weights["data"], 0.5, msg=lg)
            self.assertAlmostEqual(brains[lg].weights["llm"], 0.3, msg=lg)

    def test_per_league_file_wins_over_legacy(self):
        json.dump({"data": 0.5, "llm": 0.3, "market": 0.2},
                  open(bl.weights_path("all", self.dir), "w"))
        json.dump({"weights": {"data": 0.9, "llm": 0.0, "market": 0.1}, "version": 4},
                  open(bl.weights_path("EPL", self.dir), "w"))
        brains = bl.load(use_llm=False, state_dir=self.dir)
        self.assertAlmostEqual(brains["EPL"].weights["data"], 0.9)
        self.assertEqual(brains["EPL"].weights_version, 4)
        self.assertAlmostEqual(brains["LaLiga"].weights["data"], 0.5)   # still migrated

    def test_llm_cache_round_trips(self):
        brains = bl.new()
        brains["SerieA"].llm_cache["JUV|MIL"] = {"p": 0.61}
        bl.save(brains, state_dir=self.dir)
        again = bl.load(use_llm=False, state_dir=self.dir)
        self.assertEqual(again["SerieA"].llm_cache["JUV|MIL"], {"p": 0.61})


class TestSetLlm(unittest.TestCase):
    def test_toggles_every_brain_and_opens_llm_weight(self):
        brains = bl.new(use_llm=False)
        bl.set_llm(brains, True)
        for br in brains.values():
            self.assertTrue(br.use_llm)
            self.assertGreater(br.weights["llm"], 0)
        bl.set_llm(brains, False)
        self.assertFalse(any(br.use_llm for br in brains.values()))


class TestTrain(unittest.TestCase):
    @staticmethod
    def _rows(ticker, n, data_p, outcome):
        return [{"ticker": ticker,
                 "sources": {"data": data_p, "market": 0.5},
                 "outcome": outcome} for _ in range(n)]

    def test_trains_only_the_league_that_priced_the_bet(self):
        brains = bl.new()
        before = {lg: dict(br.weights) for lg, br in brains.items()}
        # 20 well-called EPL outcomes: above min_sample, so EPL moves.
        changed = bl.train(brains, self._rows("KXEPLGAME-25AUG16ARSMCI-ARS", 20, 0.95, 1))
        self.assertEqual(list(changed), ["EPL"])
        self.assertNotEqual(brains["EPL"].weights, before["EPL"])
        for lg in bl.LEAGUE_KEYS:
            if lg != "EPL":
                self.assertEqual(brains[lg].weights, before[lg], msg=lg)

    def test_thin_sample_is_a_no_op(self):
        brains = bl.new()
        before = dict(brains["EPL"].weights)
        changed = bl.train(brains, self._rows("KXEPLGAME-25AUG16ARSMCI-ARS", 3, 0.95, 1))
        self.assertEqual(changed, {})
        self.assertEqual(brains["EPL"].weights, before)

    def test_rows_split_across_leagues(self):
        brains = bl.new()
        rows = (self._rows("KXEPLGAME-25AUG16ARSMCI-ARS", 20, 0.95, 1)
                + self._rows("KXSERIEAGAME-25AUG16JUVMIL-JUV", 20, 0.95, 1))
        changed = bl.train(brains, rows)
        self.assertEqual(set(changed), {"EPL", "SerieA"})

    def test_ungraded_and_sourceless_rows_are_dropped(self):
        brains = bl.new()
        rows = [{"ticker": "KXEPLGAME-X-Y", "sources": {"data": 0.9}, "outcome": None},
                {"ticker": "KXEPLGAME-X-Y", "sources": {}, "outcome": 1}]
        self.assertEqual(bl.train(brains, rows), {})

    def test_weights_version_bumps_only_on_a_real_write(self):
        brains = bl.new()
        bl.train(brains, self._rows("KXEPLGAME-25AUG16ARSMCI-ARS", 3, 0.95, 1))
        self.assertEqual(brains["EPL"].weights_version, 0)
        bl.train(brains, self._rows("KXEPLGAME-25AUG16ARSMCI-ARS", 20, 0.95, 1))
        self.assertEqual(brains["EPL"].weights_version, 1)


if __name__ == "__main__":
    unittest.main()
