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


# ── #2 vs #7 longshots ────────────────────────────────────────────────────────

def _lgame(quotes, results, roles, ev="EV"):
    """quotes: {ticker: (bid, ask)} at kickoff-24h."""
    return {"ev": ev, "kickoff": KO,
            "legs": {t: {KO - 24 * H: q} for t, q in quotes.items()},
            "results": results, "roles": roles}


def test_entry_quote_takes_last_valid_bar_before_the_cutoff():
    bars = {KO - 30 * H: (10, 12), KO - 25 * H: (20, 22), KO - 23 * H: (40, 42)}
    assert b1.entry_quote(bars, KO, 24) == (20, 22)


def test_entry_quote_refuses_stale_or_invalid():
    assert b1.entry_quote({KO - 31 * H: (20, 22)}, KO, 24) is None     # >6h stale
    assert b1.entry_quote({KO - 24 * H: (0, 3)}, KO, 24) is None       # no bid


def test_longshot_no_buys_no_at_100_minus_bid_and_pays_the_fee():
    g = _lgame({"A": (4, 6), "B": (60, 62), "T": (30, 32)},
               {"A": "no", "B": "yes", "T": "no"}, {"A": "away", "B": "home", "T": "tie"})
    [t] = b1.longshot_no_trades(g, threshold=10, entry_h=24)
    assert t["ticker"] == "A" and t["price"] == 96          # NO ask = 100 - YES bid 4
    assert t["fee"] == b1.taker_fee(96)
    assert t["pnl"] == 100 - 96 - b1.taker_fee(96)


def test_longshot_no_losing_trade():
    g = _lgame({"A": (4, 6)}, {"A": "yes"}, {"A": "away"})
    [t] = b1.longshot_no_trades(g, threshold=10, entry_h=24)
    assert t["pnl"] == -96 - b1.taker_fee(96)


def test_longshot_no_threshold_is_on_the_mid():
    g = _lgame({"A": (10, 12)}, {"A": "no"}, {"A": "away"})         # mid 11
    assert b1.longshot_no_trades(g, threshold=10, entry_h=24) == []
    assert len(b1.longshot_no_trades(g, threshold=15, entry_h=24)) == 1


def test_longshot_yes_uses_angelini_thresholds_and_skips_the_draw():
    g = _lgame({"H": (20, 22), "A": (12, 14), "T": (10, 12)},
               {"H": "yes", "A": "no", "T": "no"}, {"H": "home", "A": "away", "T": "tie"})
    trades = {t["ticker"]: t for t in b1.longshot_yes_trades(g)}
    assert set(trades) == {"H", "A"}                        # home mid 21 <= 24, away 13 <= 14
    assert trades["H"]["pnl"] == 100 - 22 - b1.taker_fee(22)
    assert trades["A"]["pnl"] == -14 - b1.taker_fee(14)


def test_longshot_yes_away_threshold_is_stricter():
    g = _lgame({"A": (19, 21)}, {"A": "yes"}, {"A": "away"})         # mid 20 > 14
    assert b1.longshot_yes_trades(g) == []


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    trades = [{"ev": f"E{i}", "pnl": p} for i, p in enumerate([5, -3, 8, 1, -2, 4, 0, 6])]
    a, b = b1.bootstrap(trades), b1.bootstrap(trades)
    assert a == b
    assert a["ci99"][0] <= a["ci95"][0] <= a["mean"] <= a["ci95"][1] <= a["ci99"][1]


def test_bootstrap_resamples_whole_games():
    # two trades in one game move together: the interval must be wider than if independent
    same = [{"ev": "E0", "pnl": 10}, {"ev": "E0", "pnl": 10}] + \
           [{"ev": f"E{i}", "pnl": -1} for i in range(1, 9)]
    r = b1.bootstrap(same)
    assert r["n_trades"] == 10 and r["n_games"] == 9


def test_load_frozen_refuses_a_missing_file(tmp_path):
    import pytest
    with pytest.raises(b1.NotFrozen):
        b1.load_frozen(str(tmp_path / "nope.json"), "longshot_no")


# ── #1 maker ──────────────────────────────────────────────────────────────────

def _mgame(quote, lows, result="yes", fee_type="quadratic", entry_ts=KO - 24 * H):
    return {"ev": "EV", "kickoff": KO, "legs": {"A": {entry_ts: quote}},
            "lows": {"A": lows}, "results": {"A": result}, "roles": {"A": "home"},
            "fee_types": {"A": fee_type}}


def test_maker_fee_depends_on_the_series():
    assert b1.maker_fee(50, "quadratic") == 0
    assert b1.maker_fee(50, "quadratic_with_maker_fees") == 1     # 0.4375c -> 1c
    assert b1.maker_fee(10, "quadratic_with_maker_fees") == 1
    assert b1.maker_fee(50, None) == 1                            # unknown -> charge it


def test_maker_order_rests_at_the_bid_and_never_crosses():
    g = _mgame((60, 61), {})
    [o] = b1.maker_orders(g, min_mid=50, offset=1)
    assert o["price"] == 60                                       # bid+1 would equal the ask


def test_maker_fills_only_on_a_later_trade_strictly_below_our_price():
    at = _mgame((60, 62), {KO - 10 * H: 60})
    below = _mgame((60, 62), {KO - 10 * H: 59})
    before = _mgame((60, 62), {KO - 30 * H: 50})
    assert b1.maker_orders(at, 50, 0)[0]["filled"] is False
    assert b1.maker_orders(below, 50, 0)[0]["filled"] is True
    assert b1.maker_orders(before, 50, 0)[0]["filled"] is False


def test_maker_pnl_counts_only_filled_orders_with_the_maker_fee():
    win = b1.maker_orders(_mgame((60, 62), {KO - 10 * H: 59}, "yes",
                                 "quadratic_with_maker_fees"), 50, 0)[0]
    assert win["pnl"] == 100 - 60 - b1.maker_fee(60, "quadratic_with_maker_fees")
    lose = b1.maker_orders(_mgame((60, 62), {KO - 10 * H: 59}, "no"), 50, 0)[0]
    assert lose["pnl"] == -60
    unfilled = b1.maker_orders(_mgame((60, 62), {}), 50, 0)[0]
    assert unfilled["pnl"] is None


def test_maker_skips_legs_below_the_minimum_mid():
    assert b1.maker_orders(_mgame((40, 42), {KO - 10 * H: 30}), 50, 0) == []
