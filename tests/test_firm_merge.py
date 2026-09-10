"""Team identity and the merge.

The failure mode both guard against: a failed join does not LOSE a fixture, it
DUPLICATES one. Two rows for the same match means derived.py walks both — Elo
updates twice for one result, form counts it twice, head-to-head double-counts.
Nothing errors, every number stays plausible, and the knowledge base is quietly
wrong. So these tests care more about false merges and false splits than about
throughput.
"""
import pytest

from wc.firm import matches as M
from wc.firm.sources import openfootball as OF
from wc.firm.sources import teams as T


# ── normalisation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("Manchester United FC", "Man United"),
    ("FC Bayern München", "Bayern Munich"),
    ("Paris Saint-Germain", "PSG"),
    ("Atlético Madrid", "Atletico Madrid"),
    ("Tottenham Hotspur", "Spurs"),
    ("New York Red Bulls", "NYRB"),
])
def test_the_same_club_resolves_to_one_identity(a, b):
    assert T.normalise(a) == T.normalise(b)


@pytest.mark.parametrize("a,b", [
    ("Atlético Madrid", "Real Madrid"),
    ("Atlético Mineiro", "Cruzeiro"),
    ("Club América", "Club Tijuana"),
    ("Arsenal", "Arsenal de Sarandí"),
    ("Manchester United", "Manchester City"),
    ("Inter Milan", "AC Milan"),
])
def test_different_clubs_never_collapse(a, b):
    """'atletico' looks like furniture and is not: stripping it collapsed
    Atletico Madrid into 'madrid', one identity for two clubs."""
    assert T.normalise(a) != T.normalise(b)


def test_resolution_never_crosses_leagues():
    """Without league scoping, the two Arsenals become one club — and so do
    the several Serie As."""
    r = T.TeamRegistry()
    r.add("EPL", "Arsenal")
    r.add("ArgPrimera", "Arsenal de Sarandí")
    assert r.resolve("EPL", "Arsenal") != r.resolve("ArgPrimera", "Arsenal de Sarandí")


def test_an_unknown_name_is_reported_not_guessed():
    r = T.TeamRegistry()
    r.add("EPL", "Arsenal")
    assert r.resolve("EPL", "Ulaanbaatar City") is None
    assert r.report()["unresolved_count"] == 1


def test_every_mapped_league_has_a_country():
    """The HF catalogue is matched on name AND country. 'Serie A' alone hits
    Italy, Brazil and Ecuador — three competitions merged into one history."""
    for name, cfg in T.load_leagues().items():
        if cfg.get("af_name"):
            assert cfg.get("country"), f"{name} has af_name but no country"


# ── the merge ─────────────────────────────────────────────────────────────────

def row(source, league="EPL", home="Arsenal FC", away="Chelsea FC",
        ts=1_700_000_000, hg=2, ag=1, **extra):
    r = {"league": league, "kickoff_ts": ts, "home_raw": home, "away_raw": away,
         "home_goals": hg, "away_goals": ag, "source": source}
    r.update(extra)
    return r


def merge(*rows):
    reg = M.build_registry(list(rows))
    m = M.Merger(reg)
    for r in rows:
        m.add(r)
    return m


def test_the_same_fixture_from_two_feeds_becomes_one_row():
    m = merge(row("openfootball", home="Arsenal FC", away="Chelsea FC"),
              row("hf_datalake", home="Arsenal", away="Chelsea"))
    out = m.finish()
    assert len(out) == 1
    assert out[0]["sources"] == "hf_datalake,openfootball"


def test_a_kickoff_either_side_of_midnight_still_joins():
    """A 20:00 UTC kickoff is the next calendar day in some feeds. Strict date
    equality would fail to join a meaningful slice of fixtures."""
    m = merge(row("openfootball", ts=1_700_000_000),
              row("hf_datalake", ts=1_700_000_000 + 20 * 3600))
    assert len(m.finish()) == 1


def test_a_genuinely_different_date_does_not_join():
    """Two legs of a tie a week apart are two matches, not one."""
    m = merge(row("openfootball", ts=1_700_000_000),
              row("hf_datalake", ts=1_700_000_000 + 7 * 86400))
    assert len(m.finish()) == 2


def test_columns_stay_empty_where_a_source_lacks_them():
    m = merge(row("openfootball", ht_home_goals=1, ht_away_goals=0),
              row("hf_datalake", shots_home=14.0, odds_home=1.8))
    out = m.finish()[0]
    assert out["ht_home_goals"] == 1          # openfootball only
    assert out["shots_home"] == 14.0          # HF only
    assert out["corners_home"] is None        # neither carried it


def test_a_real_value_is_never_overwritten_by_a_null():
    m = merge(row("openfootball", ht_home_goals=2),
              row("hf_datalake", ht_home_goals=None))
    assert m.finish()[0]["ht_home_goals"] == 2


def test_a_score_disagreement_is_logged_not_averaged():
    """A disagreement is a data-quality signal about the feeds, not noise."""
    m = merge(row("openfootball", hg=2, ag=1), row("hf_datalake", hg=1, ag=1))
    out = m.finish()[0]
    assert m.stats["conflicts"] == 1
    assert out["conflict"] and "openfootball" in out["conflict"]
    assert (out["home_goals"], out["away_goals"]) == (2, 1), "openfootball wins"


def test_a_missing_score_is_filled_by_whoever_has_one():
    m = merge(row("kalshi", hg=None, ag=None, kalshi_event="KXEPLGAME-X"),
              row("openfootball", hg=3, ag=0))
    out = m.finish()[0]
    assert (out["home_goals"], out["away_goals"]) == (3, 0)
    assert out["kalshi_event"] == "KXEPLGAME-X"


def test_an_unresolvable_row_is_counted_not_merged_blindly():
    reg = M.build_registry([row("openfootball")])
    m = M.Merger(reg)
    m.add(row("hf_datalake", home="Totally Unknown United",
              away="Also Unknown City"))
    assert m.stats["unresolved"] >= 1


def test_a_team_playing_itself_is_refused():
    reg = M.build_registry([row("openfootball")])
    m = M.Merger(reg)
    m.add(row("openfootball", home="Arsenal FC", away="Arsenal"))
    assert m.finish() == []


def test_known_at_trails_kickoff():
    """A result is not knowable at kick-off."""
    out = merge(row("openfootball", ts=1_700_000_000)).finish()[0]
    assert out["known_at"] > out["kickoff_ts"]


# ── the guards ────────────────────────────────────────────────────────────────

def test_duplicate_detection_catches_a_double_counted_fixture(tmp_path):
    con = M.connect(str(tmp_path / "s.db"))
    base = {"league": "EPL", "home": "Arsenal", "away": "Chelsea",
            "home_key": "arsenal", "away_key": "chelsea",
            "home_goals": 2, "away_goals": 1, "known_at": 1_700_007_200,
            "sources": "openfootball"}
    M.write(con, [dict(base, match_id="a", kickoff_ts=1_700_000_000),
                  dict(base, match_id="b", kickoff_ts=1_700_010_000)])
    assert len(M.find_duplicates(con)) == 1
    rep = M.report(con)
    assert any("counted TWICE" in b for b in rep["blocking"])


def test_a_clean_table_has_no_blocking_issues(tmp_path):
    con = M.connect(str(tmp_path / "s.db"))
    M.write(con, merge(row("openfootball"), row("hf_datalake")).finish())
    assert M.report(con)["blocking"] == []


def test_report_shows_which_columns_a_source_filled(tmp_path):
    con = M.connect(str(tmp_path / "s.db"))
    M.write(con, merge(row("openfootball", ht_home_goals=1),
                       row("hf_datalake", shots_home=9.0)).finish())
    cov = M.report(con)["column_coverage"]
    assert cov["ht_home_goals"]["n"] == 1
    assert cov["shots_home"]["n"] == 1
    assert cov["kalshi_event"]["n"] == 0


# ── openfootball parsing ──────────────────────────────────────────────────────

def test_both_score_shapes_are_handled():
    """`score` is a dict in most files and a bare list in some. Assuming one
    shape crashes an entire season over a handful of rows."""
    assert OF._score({"ft": [3, 0], "ht": [1, 0]}) == ([3, 0], [1, 0])
    assert OF._score([2, 2]) == ([2, 2], [None, None])
    assert OF._score(None) == (None, None)
    assert OF._score({"ht": [1, 0]}) == (None, None)      # not played


def test_a_missing_time_still_yields_a_usable_kickoff():
    """Noon is a deliberate mid-day anchor so a +/-1 day join window lands on
    the right fixture whichever side of midnight the feed put it."""
    ts = OF._kickoff("2026-09-06", None)
    import datetime as dt
    assert dt.datetime.fromtimestamp(ts, dt.timezone.utc).hour == 12
