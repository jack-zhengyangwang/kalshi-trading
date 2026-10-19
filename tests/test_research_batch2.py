"""Batch 2 pre-registered tests (docs/findings/11_PREREG_BATCH2.md)."""
from wc.research import batch2 as b2

H = 3600
DAY = 24 * H
KO = 100 * DAY


def _game(quotes, roles, results, subs=None, vol=None, espn_id=None, swapped=False):
    """quotes: {ticker: (bid, ask)} quoted at KO - 24h and KO - 6h."""
    legs = {t: {KO - 24 * H: q, KO - 6 * H: q} for t, q in quotes.items()}
    return {"ev": "EV", "kickoff": KO, "legs": legs, "roles": roles, "results": results,
            "subs": subs or {}, "vol": vol or {}, "espn_id": espn_id, "swapped": swapped}


BOOK = {"H": (54, 56), "A": (24, 26), "T": (19, 21)}
ROLES = {"H": "home", "A": "away", "T": "tie"}


def test_bet_yes_costs_the_ask_and_records_bias():
    g = _game(BOOK, ROLES, {"H": "yes", "A": "no", "T": "no"})
    tr = b2.bet(g, "H", "yes", 24)
    assert tr["price"] == 56 and tr["fee"] == 2
    assert tr["pnl"] == 100 - 56 - 2
    assert tr["bias"] == 100 - 55          # won - mid


def test_bet_no_costs_100_minus_bid():
    g = _game(BOOK, ROLES, {"H": "yes", "A": "no", "T": "no"})
    tr = b2.bet(g, "A", "no", 24)
    assert tr["price"] == 76               # 100 - 24
    assert tr["pnl"] == 100 - 76 - b2.b1.taker_fee(76)
    assert tr["bias"] == 100 - 75          # NO mid = 100 - 25


def test_bet_without_a_quote_is_none():
    g = _game(BOOK, ROLES, {"H": "yes", "A": "no", "T": "no"})
    g["legs"]["H"] = {}
    assert b2.bet(g, "H", "yes", 24) is None


def test_h1_fades_only_exact_big_club_names():
    subs = {"H": "Real Madrid", "A": "Inter Toronto", "T": "Tie"}
    g = _game(BOOK, ROLES, {"H": "no", "A": "yes", "T": "no"}, subs=subs)
    trades = b2.h1_trades(g, 24)
    assert [(t["ticker"], t["side"]) for t in trades] == [("H", "no")]


def test_h3_buys_the_tie_with_optional_mid_filter():
    g = _game(BOOK, ROLES, {"H": "no", "A": "no", "T": "yes"})
    assert [t["ticker"] for t in b2.h3_trades(g, 24, 0)] == ["T"]
    assert b2.h3_trades(g, 24, 25) == []   # tie mid 20 < 25


def test_h4_fades_favourite_only_in_busy_games():
    vol = {"H": {KO - 30 * H: 400, KO - 2 * H: 9999}, "A": {KO - 30 * H: 100}, "T": {}}
    g = _game(BOOK, ROLES, {"H": "no", "A": "yes", "T": "no"}, vol=vol)
    assert b2.game_volume(g, 24) == 500    # the bar after entry doesn't count
    assert [(t["ticker"], t["side"]) for t in b2.h4_trades(g, 500, 24)] == [("H", "no")]
    assert b2.h4_trades(g, 501, 24) == []


def _espn_event(eid, ts, home, away, hs, as_, status="STATUS_FULL_TIME"):
    return {"id": eid, "date": ts, "competitions": [{"status": {"type": {
        "name": status, "completed": True}}, "competitors": [
        {"homeAway": "home", "team": {"id": home}, "score": str(hs)},
        {"homeAway": "away", "team": {"id": away}, "score": str(as_)}]}]}


def _iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def test_history_margins_and_extra_time_counts_as_zero():
    hist = b2.team_history([
        _espn_event("1", _iso(KO - 5 * DAY), "10", "20", 4, 0),
        _espn_event("2", _iso(KO - 4 * DAY), "30", "40", 3, 0, "STATUS_FINAL_PEN"),
    ])
    assert hist["10"] == [(KO - 5 * DAY, 4)]
    assert hist["20"] == [(KO - 5 * DAY, -4)]
    assert hist["30"] == [(KO - 4 * DAY, 0)]


def test_last_margin_respects_entry_time_and_14_day_window():
    hist = {"10": [(KO - 20 * DAY, 5), (KO - 3 * DAY, 3), (KO - 23 * H, -4)]}
    # the game 23h before kickoff ended after the 24h entry: not known yet
    assert b2.last_margin(hist, "10", KO, 24) == 3
    assert b2.last_margin({"10": [(KO - 20 * DAY, 5)]}, "10", KO, 24) is None


def test_h2_direction_and_swapped_espn_sides():
    espn = {"E": {"home": "10", "away": "20"}}
    hist = {"10": [(KO - 3 * DAY, 3)], "20": [(KO - 3 * DAY, -2)]}
    g = _game(BOOK, ROLES, {"H": "no", "A": "yes", "T": "no"}, espn_id="E")
    got = {(t["ticker"], t["side"]) for t in b2.h2_trades(g, 2, hist, espn)}
    assert got == {("H", "no"), ("A", "yes")}
    assert {(t["ticker"]) for t in b2.h2_trades(g, 3, hist, espn)} == {"H"}
    g["swapped"] = True                    # Kalshi home is ESPN away
    got = {(t["ticker"], t["side"]) for t in b2.h2_trades(g, 2, hist, espn)}
    assert got == {("H", "yes"), ("A", "no")}


def test_select_needs_min_bets():
    grid = {"a": {"n_trades": 49, "mean": 9.0}, "b": {"n_trades": 60, "mean": 1.0},
            "c": {"n_trades": 80, "mean": 1.0}}
    assert b2.select(grid) == "c"
    assert b2.select({"a": {"n_trades": 10, "mean": 5.0}}) is None


def test_reg_time_prefix_is_stripped_and_roles_redone():
    g = _game(BOOK, {"H": None, "A": None, "T": None}, {"H": "no", "A": "yes", "T": "no"},
              subs={"H": "Reg Time: Arsenal", "A": "Reg Time: Fulham", "T": "Reg Time: Tie"})
    b2.clean_roles(g, {"H": ("Arsenal", "Fulham"), "A": ("Arsenal", "Fulham"),
                       "T": ("Arsenal", "Fulham")})
    assert g["roles"] == {"H": "home", "A": "away", "T": "tie"}
    assert [t["ticker"] for t in b2.h1_trades(g, 24)] == ["H"]


def test_h1_skips_womens_series():
    g = _game({"KXUCLWGAME-X-PSG": (54, 56), "KXUCLWGAME-X-ARS": (24, 26)},
              {"KXUCLWGAME-X-PSG": "home", "KXUCLWGAME-X-ARS": "away"},
              {"KXUCLWGAME-X-PSG": "yes", "KXUCLWGAME-X-ARS": "no"},
              subs={"KXUCLWGAME-X-PSG": "PSG", "KXUCLWGAME-X-ARS": "Arsenal"})
    assert b2.h1_trades(g, 24) == []


def test_hourly_from_snapshots_matches_backfill_convention():
    # snapshots at 02:00, 02:10, 03:00 (cumulative 66, 76, 76); 03:05 -> hour 04:00
    rows = [(2 * H, 44, 46, 66), (2 * H + 600, 44, 46, 76), (3 * H, 44, 45, 76),
            (3 * H + 300, 43, 45, 80)]
    got = b2.hourly_from_snapshots(rows)
    assert got[2 * H] == (44, 46, 66)       # first hour carries the running total
    assert got[3 * H] == (44, 45, 10)       # last snapshot in (02:00, 03:00]; 76 - 66
    assert got[4 * H] == (43, 45, 4)


def test_collector_bars_fill_gaps_but_never_overwrite_backfill():
    g = _game(BOOK, ROLES, {"H": "yes", "A": "no", "T": "no"})
    g["vol"] = {"H": {KO - 24 * H: 7}}
    rows = {"H": [(KO - 24 * H, 1, 2, 500), (KO - 3 * H, 50, 52, 600), (KO + H, 9, 9, 999)]}
    b2.add_collector_bars(g, rows)
    assert g["legs"]["H"][KO - 24 * H] == (54, 56)       # backfill kept
    assert g["vol"]["H"][KO - 24 * H] == 7
    assert g["legs"]["H"][KO - 3 * H] == (50, 52)        # gap filled
    assert g["vol"]["H"][KO - 3 * H] == 100
    assert KO + H not in g["legs"]["H"]                  # nothing at or after kickoff
