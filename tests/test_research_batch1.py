"""Batch 1 pre-registered tests (docs/findings/05_PREREG_BATCH1.md)."""
from wc.research import batch1 as b1

H = 3600
KO = 100 * H


def _game(legs):
    return {"ev": "EV", "kickoff": KO, "legs": legs}


def test_taker_fee_rounds_up_to_the_cent():
    assert b1.taker_fee(50) == 2          # 0.07*0.5*0.5 = 1.75c -> 2c
    assert b1.taker_fee(10) == 1          # 0.63c -> 1c
    assert b1.taker_fee(99) == 1
    assert b1.taker_fee(0) == 0


def test_no_gap_in_a_normal_book():
    g = _game({"A": {KO - 5 * H: (44, 46)}, "B": {KO - 5 * H: (29, 31)},
               "T": {KO - 5 * H: (24, 26)}})
    assert b1.three_way_gaps(g) == []


def test_buy_set_gap_after_fees():
    # asks 30+30+30 = 90c, fees 2+2+2 = 6c -> 96c, gap 4c
    g = _game({"A": {KO - 5 * H: (28, 30)}, "B": {KO - 5 * H: (28, 30)},
               "T": {KO - 5 * H: (28, 30)}})
    gaps = b1.three_way_gaps(g)
    assert len(gaps) == 1
    assert gaps[0]["side"] == "buy" and gaps[0]["gap_c"] == 4


def test_sell_set_gap_after_fees():
    # bids 40+35+35 = 110c, fees 2+2+2 = 6c -> 104c, gap 4c
    g = _game({"A": {KO - 5 * H: (40, 42)}, "B": {KO - 5 * H: (35, 37)},
               "T": {KO - 5 * H: (35, 37)}})
    gaps = b1.three_way_gaps(g)
    assert [x["side"] for x in gaps] == ["sell"] and gaps[0]["gap_c"] == 4


def test_gap_smaller_than_fees_is_not_a_gap():
    # asks 32+32+32 = 96c, fees 6c -> 102c
    g = _game({"A": {KO - 5 * H: (30, 32)}, "B": {KO - 5 * H: (30, 32)},
               "T": {KO - 5 * H: (30, 32)}})
    assert b1.three_way_gaps(g) == []


def test_bar_needs_all_three_legs_quoted():
    g = _game({"A": {KO - 5 * H: (28, 30)}, "B": {KO - 5 * H: (28, 30)},
               "T": {KO - 4 * H: (28, 30)}})
    assert b1.three_way_gaps(g) == []


def test_wide_or_empty_quotes_do_not_count():
    wide = _game({"A": {KO - 5 * H: (10, 30)}, "B": {KO - 5 * H: (28, 30)},
                  "T": {KO - 5 * H: (28, 30)}})
    empty = _game({"A": {KO - 5 * H: (0, 30)}, "B": {KO - 5 * H: (28, 30)},
                   "T": {KO - 5 * H: (28, 30)}})
    assert b1.three_way_gaps(wide) == [] and b1.three_way_gaps(empty) == []


def test_bars_at_or_after_kickoff_are_ignored():
    g = _game({"A": {KO: (28, 30)}, "B": {KO: (28, 30)}, "T": {KO: (28, 30)}})
    assert b1.three_way_gaps(g) == []


def test_games_without_exactly_three_legs_are_skipped():
    g = _game({"A": {KO - 5 * H: (28, 30)}, "B": {KO - 5 * H: (28, 30)}})
    assert b1.three_way_gaps(g) == []


def test_runs_counts_consecutive_hours():
    ts = [KO - 5 * H, KO - 4 * H, KO - 2 * H]
    assert b1.runs(ts) == [2, 1]
    assert b1.runs([]) == []
