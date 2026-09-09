"""A PM's journal, and the point-in-time soccer knowledge it complements.

Two different kinds of knowing:
  • derived.py  — what is true of the WORLD, rebuilt by walking matches forward
  • Journal     — what is true of THIS MANAGER, written only when a bet settles

Both must be incapable of consulting the future. The first because it emits
before it updates; the second because nothing is written until a result exists.
"""
import pytest

from wc.backtest import data as market_data
from wc.firm import facts as F
from wc.firm.journal import Journal
from wc.firm.sources import derived


# ── the journal ───────────────────────────────────────────────────────────────

def test_an_untested_league_is_unproven_not_bad():
    """A desk entering a league it has never traded should size normally and
    find out, not be penalised for inexperience."""
    assert Journal("pm").size_multiplier("KXLIGAMX") == 1.0


def test_a_small_sample_earns_no_opinion():
    j = Journal("pm")
    for i in range(4):
        j.record(i, "KXLIGAMX", 0.9, 0, -5.0)
    assert j.reliability("KXLIGAMX") is None, "4 bets is noise, not a record"
    assert j.size_multiplier("KXLIGAMX") == 1.0


def test_being_confidently_wrong_scales_the_desk_down():
    j = Journal("pm")
    for i in range(12):
        j.record(i, "KXLIGAMX", 0.9, 0, -5.0)
    assert j.reliability("KXLIGAMX") > 0.25
    assert j.size_multiplier("KXLIGAMX") == pytest.approx(0.25)


def test_a_good_record_never_scales_the_desk_up():
    """Letting a hot streak raise the stake would make this a martingale."""
    j = Journal("pm")
    for i in range(20):
        j.record(i, "KXEPL", 0.9, 1, 5.0)
    assert j.reliability("KXEPL") < 0.25
    assert j.size_multiplier("KXEPL") == 1.0


def test_learning_is_per_league():
    j = Journal("pm")
    for i in range(12):
        j.record(i, "KXLIGAMX", 0.9, 0, -5.0)
    assert j.size_multiplier("KXLIGAMX") < 1.0
    assert j.size_multiplier("KXEPL") == 1.0, "one bad league is not all leagues"


def test_a_void_teaches_nothing():
    """A cancelled match makes a forecast unresolved, not wrong."""
    j = Journal("pm")
    for i in range(20):
        j.record(i, "KXEPL", 0.9, None, 0.0)
    assert len(j) == 0
    assert j.size_multiplier("KXEPL") == 1.0


def test_summary_breaks_out_what_was_learned():
    j = Journal("pm")
    for i in range(10):
        j.record(i, "KXEPL", 0.7, 1, 2.0)
    s = j.summary()
    assert s["all"]["n"] == 10
    assert s["series:KXEPL"]["hit_rate"] == 1.0
    assert "band:0.6" in s


# ── derived knowledge, point-in-time ──────────────────────────────────────────

def seed_match(con, event, home, away, winner_sub, close_ts):
    """Three legs of one settled fixture, as the store holds them."""
    for i, sub in enumerate([home, "Tie", away]):
        market_data.upsert_market(con, {
            "ticker": f"{event}-{i}", "series": "KXTEST", "title": sub,
            "sub_title": sub, "event_ticker": event, "home": home, "away": away,
            "close_time": close_ts, "status": "finalized",
            "result": "yes" if sub == winner_sub else "no"})
        market_data.set_result(con, f"{event}-{i}", "finalized",
                               "yes" if sub == winner_sub else "no")


@pytest.fixture
def dbs(tmp_path):
    m = market_data.connect(str(tmp_path / "m.db"))
    f = F.connect(str(tmp_path / "s.db"))
    return m, f


def test_settled_matches_are_read_in_order(dbs):
    m, _ = dbs
    seed_match(m, "E2", "Arsenal", "Chelsea", "Arsenal", 2000)
    seed_match(m, "E1", "Arsenal", "Spurs", "Spurs", 1000)
    got = derived.settled_matches(m)
    assert [x[0] for x in got] == [1000, 2000]
    assert got[0][4] == "away", "Spurs were away and won"
    assert got[1][4] == "home"


def test_a_fact_reflects_only_matches_already_finished(dbs):
    """The loop emits before it updates. Reversing those two lines would leak
    every result into its own prediction."""
    m, f = dbs
    seed_match(m, "E1", "Arsenal", "Spurs", "Arsenal", 1000)
    seed_match(m, "E2", "Arsenal", "Chelsea", "Arsenal", 2000)
    seed_match(m, "E3", "Arsenal", "Everton", "Arsenal", 3000)
    derived.build(m, f)

    at_first = F.get(f, "Arsenal", "elo", 1000)
    at_third = F.get(f, "Arsenal", "elo", 3000)
    assert at_first is None or at_first == pytest.approx(derived.ELO_BASE), \
        "before any result, Arsenal must sit at the base rating"
    assert at_third > derived.ELO_BASE, "two wins should have raised it"


def test_a_team_never_seen_has_no_facts(dbs):
    """A desk entering an unfamiliar league should know that it knows nothing."""
    m, f = dbs
    seed_match(m, "E1", "Arsenal", "Spurs", "Arsenal", 1000)
    derived.build(m, f)
    assert F.get(f, "Guadalajara", "elo", 9999) is None


def test_matches_seen_grows_as_the_league_is_observed(dbs):
    m, f = dbs
    for i in range(4):
        seed_match(m, f"E{i}", "Arsenal", "Spurs", "Arsenal", 1000 + i * 100)
    derived.build(m, f)
    early = F.get(f, "Arsenal", "matches_seen", 1150)
    late = F.get(f, "Arsenal", "matches_seen", 1350)
    assert late > early


def test_home_and_away_records_are_kept_apart(dbs):
    m, f = dbs
    for i in range(4):
        seed_match(m, f"H{i}", "Arsenal", "Spurs", "Arsenal", 1000 + i * 100)
    for i in range(4):
        seed_match(m, f"A{i}", "Chelsea", "Arsenal", "Chelsea", 2000 + i * 100)
    derived.build(m, f)
    assert F.get(f, "Arsenal", "home_win_rate", 9999) == pytest.approx(1.0)
    assert F.get(f, "Arsenal", "away_win_rate", 9999) == pytest.approx(0.0)


def test_head_to_head_accumulates(dbs):
    m, f = dbs
    for i in range(3):
        seed_match(m, f"E{i}", "Arsenal", "Spurs", "Arsenal", 1000 + i * 100)
    derived.build(m, f)
    pair = "|".join(sorted(["Arsenal", "Spurs"]))
    assert F.get(f, pair, "h2h_games", 9999) >= 2


def test_an_unresolved_fixture_contributes_nothing(dbs):
    """Every leg losing is not a resolved game."""
    m, f = dbs
    for i, sub in enumerate(["Arsenal", "Tie", "Spurs"]):
        market_data.upsert_market(m, {
            "ticker": f"X-{i}", "series": "KXTEST", "title": sub,
            "sub_title": sub, "event_ticker": "X", "home": "Arsenal",
            "away": "Spurs", "close_time": 1000, "status": "finalized",
            "result": "no"})
        market_data.set_result(m, f"X-{i}", "finalized", "no")
    assert derived.settled_matches(m) == []
