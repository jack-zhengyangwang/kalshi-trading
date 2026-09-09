"""The shared fact store.

One property matters more than everything else here: a fact is known AS OF a
moment, and the store must never answer with something learned later. "Arsenal's
form" is not a fact about Arsenal, it is a fact about Arsenal on a date, and a
store that forgets that hands a backtest results from matches not yet played —
a number that looks entirely plausible.
"""
import pytest

from wc.firm import facts
from wc.firm.sources import espn


@pytest.fixture
def con(tmp_path):
    c = facts.connect(str(tmp_path / "f.db"))
    yield c
    c.close()


# ── as-of, the whole point ────────────────────────────────────────────────────

def test_a_later_fact_is_invisible_to_an_earlier_query(con):
    facts.put(con, "Arsenal", "form", 2.0, as_of=100, source="t")
    facts.put(con, "Arsenal", "form", 0.4, as_of=500, source="t")
    con.commit()
    assert facts.get(con, "Arsenal", "form", 300) == 2.0, "read the future"
    assert facts.get(con, "Arsenal", "form", 600) == 0.4


def test_nothing_known_yet_is_none_not_zero(con):
    """A view must treat this as 'I cannot price it'. A fabricated zero is a
    belief the data did not support."""
    facts.put(con, "Arsenal", "form", 2.0, as_of=100, source="t")
    con.commit()
    assert facts.get(con, "Arsenal", "form", 50) is None
    assert facts.get(con, "Chelsea", "form", 999) is None


def test_revisions_do_not_destroy_the_earlier_record(con):
    """Append-only. Overwriting in place would erase what was knowable before
    the revision, which is exactly what a backtest needs."""
    facts.put(con, "A", "x", 1.0, as_of=100, source="t")
    facts.put(con, "A", "x", 2.0, as_of=200, source="t")
    con.commit()
    assert facts.get(con, "A", "x", 150) == 1.0
    assert facts.fact_count(con) == 2


def test_the_same_observation_replaces_itself(con):
    facts.put(con, "A", "x", 1.0, as_of=100, source="t")
    facts.put(con, "A", "x", 1.5, as_of=100, source="t")
    con.commit()
    assert facts.fact_count(con) == 1
    assert facts.get(con, "A", "x", 100) == 1.5


def test_snapshot_is_also_as_of(con):
    facts.put_many(con, [
        {"entity": "A", "feature": "f1", "value": 1.0, "as_of": 100},
        {"entity": "A", "feature": "f2", "value": 2.0, "as_of": 100},
        {"entity": "A", "feature": "f1", "value": 9.0, "as_of": 900},
    ], source="t")
    assert facts.snapshot(con, "A", 500) == {"f1": 1.0, "f2": 2.0}


# ── the index has no way to read the present ──────────────────────────────────

def test_index_agrees_with_the_store(con):
    facts.put_many(con, [
        {"entity": "A", "feature": "f", "value": 1.0, "as_of": 100},
        {"entity": "A", "feature": "f", "value": 2.0, "as_of": 200},
        {"entity": "B", "feature": "f", "value": 5.0, "as_of": 150},
    ], source="t")
    ix = facts.index(con, "f")
    assert ix.at("A", 150) == 1.0
    assert ix.at("A", 250) == 2.0
    assert ix.at("B", 100) is None
    assert ix.at("missing", 999) is None


def test_index_exposes_no_current_value():
    """The API must not permit reading 'the latest' — that is the mistake this
    class exists to make impossible."""
    ix = facts.AsOfIndex([("A", 100, 1.0)])
    for attr in ("latest", "current", "now", "value"):
        assert not hasattr(ix, attr), f"AsOfIndex.{attr} invites lookahead"


def test_index_is_unaffected_by_insertion_order(con):
    facts.put_many(con, [
        {"entity": "A", "feature": "f", "value": 3.0, "as_of": 300},
        {"entity": "A", "feature": "f", "value": 1.0, "as_of": 100},
    ], source="t")
    assert facts.index(con, "f").at("A", 200) == 1.0


# ── moneyline conversion ──────────────────────────────────────────────────────

def test_positive_and_negative_moneylines_convert_correctly():
    """Getting the sign wrong inverts every favourite — the kind of error that
    looks like a strategy."""
    assert espn.implied_from_moneyline(100) == pytest.approx(0.5)
    assert espn.implied_from_moneyline(115) == pytest.approx(100 / 215)
    assert espn.implied_from_moneyline(-150) == pytest.approx(150 / 250)
    assert espn.implied_from_moneyline(None) is None
    assert espn.implied_from_moneyline("x") is None


def test_devig_normalises_to_one():
    """Raw implied probabilities sum above 1 — the overround is the bookmaker's
    margin. Comparing a vigged book against Kalshi would show a systematic edge
    in every market in the same direction, which is what a fake edge looks
    like."""
    raw = {"h": espn.implied_from_moneyline(115),
           "a": espn.implied_from_moneyline(185),
           "d": espn.implied_from_moneyline(280)}
    assert sum(raw.values()) > 1.0, "the book has a margin"
    fair = espn.devig(raw)
    assert sum(fair.values()) == pytest.approx(1.0)
    assert fair["h"] > fair["a"] > fair["d"]


def test_devig_survives_a_missing_leg():
    fair = espn.devig({"h": 0.5, "a": 0.6, "d": None})
    assert sum(fair.values()) == pytest.approx(1.0)
    assert "d" not in fair


# ── extraction ────────────────────────────────────────────────────────────────

def test_form_is_oriented_to_the_team_not_the_scoreline():
    """`score` is the MATCH score; `atVs` says which side the team was on.
    Reading it left-to-right inverts every away result."""
    payload = {"lastFiveGames": [{
        "team": {"displayName": "Arsenal"},
        "events": [
            # Arsenal were AWAY and won 0-3. Read left-to-right this looks like
            # a 3-goal defeat.
            {"gameResult": "W", "homeTeamScore": "0", "awayTeamScore": "3",
             "atVs": "at", "gameDate": "2026-09-01T00:00:00Z"},
        ]}]}
    rows = {r["feature"]: r["value"]
            for r in espn.team_facts(payload, 1_790_000_000)}
    assert rows["form_last5_gd"] == pytest.approx(3.0)
    assert rows["form_last5_gf"] == pytest.approx(3.0)


def test_form_is_dated_to_the_last_game_not_the_fetch():
    """A last-five record became true when the fifth game finished. Dating it
    today claims we knew it earlier than we did."""
    payload = {"lastFiveGames": [{
        "team": {"displayName": "Arsenal"},
        "events": [
            {"gameResult": "W", "homeTeamScore": "1", "awayTeamScore": "0",
             "atVs": "vs", "gameDate": "2026-09-01T00:00:00Z"},
            {"gameResult": "L", "homeTeamScore": "0", "awayTeamScore": "2",
             "atVs": "vs", "gameDate": "2026-09-05T00:00:00Z"},
        ]}]}
    rows = espn.team_facts(payload, 1_790_000_000)      # fetched later, in 2026
    import datetime as dt
    stamped = {r["as_of"] for r in rows if r["feature"] == "form_last5_ppg"}
    when = dt.datetime.fromtimestamp(stamped.pop(), dt.timezone.utc)
    assert when.strftime("%Y-%m-%d") == "2026-09-05"


def test_a_string_team_field_does_not_kill_the_fetch():
    """ESPN returns `team` as an object in most places and a bare string in
    some standings payloads. Crashing a whole league over a label field is a
    poor trade."""
    payload = {"standings": {"groups": [{"standings": {"entries": [
        {"team": "Arsenal", "stats": [{"name": "rank", "value": 3}]}]}}]}}
    rows = espn.team_facts(payload, 100)
    assert {"entity": "Arsenal", "feature": "table_rank",
            "value": 3, "as_of": 100} in rows


def test_coverage_reports_what_the_firm_knows(con):
    facts.put_many(con, [
        {"entity": "A", "feature": "form", "value": 1.0, "as_of": 100}],
        source="espn:eng.1")
    cov = facts.coverage(con)
    assert cov[0]["feature"] == "form" and cov[0]["source"] == "espn:eng.1"
