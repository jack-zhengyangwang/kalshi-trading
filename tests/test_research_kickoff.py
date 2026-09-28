"""Kickoff times: ESPN is the source of truth, Kalshi is the cross-check."""
from wc.research import kickoff as ko


def _espn_event(date, home, away, eid="1"):
    return {"id": eid, "date": date, "competitions": [{"competitors": [
        {"homeAway": "home", "team": {"displayName": home, "shortDisplayName": home}},
        {"homeAway": "away", "team": {"displayName": away, "shortDisplayName": away}},
    ]}]}


def test_names_match_ignores_accents_and_club_suffixes():
    assert ko.names_match("Goteborg", "IFK Göteborg")
    assert ko.names_match("Halmstad", "Halmstads BK")
    assert ko.names_match("Bournemouth", "AFC Bournemouth")
    assert not ko.names_match("Halmstad", "IFK Göteborg")


def test_names_match_needs_more_than_a_generic_word():
    # "FC" / "United" alone must not glue two different clubs together
    assert not ko.names_match("FC Dallas", "FC Porto")
    assert not ko.names_match("Leeds United", "Newcastle United")


def test_ticker_date():
    assert ko.ticker_date("KXALLSVENSKANGAME-26SEP12IFKHAL") == "2026-09-12"
    assert ko.ticker_date("garbage") is None


def test_find_kickoff_matches_both_teams():
    events = [_espn_event("2026-09-12T13:00Z", "AIK", "Västerås SK", "a"),
              _espn_event("2026-09-12T15:30Z", "IFK Göteborg", "Halmstads BK", "b")]
    hit = ko.find_kickoff("Goteborg", "Halmstad", events)
    assert hit["espn_id"] == "b"
    assert hit["kickoff_ts"] == 1789227000          # 2026-09-12T15:30Z
    assert hit["swapped"] is False


def test_find_kickoff_accepts_swapped_home_away_and_says_so():
    events = [_espn_event("2026-09-12T15:30Z", "Halmstads BK", "IFK Göteborg")]
    hit = ko.find_kickoff("Goteborg", "Halmstad", events)
    assert hit is not None and hit["swapped"] is True


def test_find_kickoff_refuses_ambiguous_matches():
    events = [_espn_event("2026-09-12T15:30Z", "IFK Göteborg", "Halmstads BK", "a"),
              _espn_event("2026-09-13T15:30Z", "IFK Göteborg", "Halmstads BK", "b")]
    assert ko.find_kickoff("Goteborg", "Halmstad", events) is None


def test_find_kickoff_one_team_is_not_enough():
    events = [_espn_event("2026-09-12T15:30Z", "IFK Göteborg", "AIK")]
    assert ko.find_kickoff("Goteborg", "Halmstad", events) is None


def test_reconcile_statuses():
    espn = 1_000_000
    assert ko.reconcile(espn, espn + 3 * 3600, offset=3 * 3600)["status"] == "agree"
    assert ko.reconcile(espn, espn + 9 * 3600, offset=3 * 3600)["status"] == "disagree"
    assert ko.reconcile(espn, None, offset=3 * 3600)["status"] == "espn_only"
    assert ko.reconcile(None, espn, offset=3 * 3600)["status"] == "kalshi_only"
    assert ko.reconcile(None, None, offset=3 * 3600)["status"] == "missing"


def test_reconcile_keeps_espn_as_truth_when_they_disagree():
    r = ko.reconcile(1_000_000, 1_000_000 + 9 * 3600, offset=3 * 3600)
    assert r["kickoff_ts"] == 1_000_000
    assert r["kalshi_minus_espn_s"] == 9 * 3600


def test_reconcile_falls_back_to_kalshi_minus_offset():
    r = ko.reconcile(None, 1_000_000, offset=3 * 3600)
    assert r["kickoff_ts"] == 1_000_000 - 3 * 3600
