"""v2 Phase 0 dataset: point-in-time features, targets, benchmarks, joins."""
import pytest

from wc.research import dataset as ds

H = 3600
D = 86400


def _m(mid, home, away, day, hg, ag, league="L", shots=(None, None)):
    ko = 1_000_000 + day * D
    return {"id": mid, "league": league, "home_key": home, "away_key": away,
            "kickoff": ko, "known_at": ko + 2 * H, "hg": hg, "ag": ag,
            "shots_h": shots[0], "shots_a": shots[1]}


def _q(qid, home, away, cutoff, league="L"):
    return {"qid": qid, "home_key": home, "away_key": away, "league": league, "cutoff": cutoff}


def test_outcome_labels():
    assert ds.outcome(2, 1) == "home"
    assert ds.outcome(1, 1) == "draw"
    assert ds.outcome(0, 3) == "away"


def test_devig_removes_the_margin_proportionally():
    ph, pd, pa = ds.devig(2.0, 4.0, 4.0)          # 0.5 + 0.25 + 0.25 = 1.0 already
    assert (ph, pd, pa) == pytest.approx((0.5, 0.25, 0.25))
    ph, pd, pa = ds.devig(1.8, 3.6, 3.6)          # overround 1.111
    assert ph + pd + pa == pytest.approx(1.0)
    assert ph == pytest.approx(0.5)
    assert ds.devig(None, 3.0, 3.0) is None
    assert ds.devig(1.0, 3.0, 3.0) is None        # odds of 1.0 are not a price


def test_leg_brier():
    assert ds.leg_brier([(1.0, 1), (0.0, 0)]) == 0
    assert ds.leg_brier([(0.5, 1), (0.5, 0)]) == pytest.approx(0.25)


def test_first_match_of_a_team_has_no_history():
    f = ds.build_features([], [_q("q", "a", "b", 1_000_000)])["q"]
    assert f["seen_h"] == 0 and f["seen_a"] == 0
    assert f["elo_h"] == ds.ELO_BASE and f["form_h"] is None


def test_elo_moves_after_a_known_result():
    ms = [_m("m1", "a", "b", 0, 2, 0)]
    f = ds.build_features(ms, [_q("q", "a", "b", ms[0]["known_at"] + 1)])["q"]
    assert f["elo_h"] > ds.ELO_BASE > f["elo_a"]
    assert f["elo_h"] - ds.ELO_BASE == pytest.approx(ds.ELO_BASE - f["elo_a"])
    assert f["seen_h"] == 1


def test_no_peek_a_result_known_at_or_after_the_cutoff_is_excluded():
    ms = [_m("m1", "a", "b", 0, 2, 0)]
    at = ds.build_features(ms, [_q("q", "a", "b", ms[0]["known_at"])])["q"]
    before = ds.build_features(ms, [_q("q", "a", "b", ms[0]["kickoff"])])["q"]
    assert at["seen_h"] == 0 and before["seen_h"] == 0


def test_order_of_input_does_not_matter():
    ms = [_m("m1", "a", "b", 0, 2, 0), _m("m2", "b", "a", 7, 1, 1)]
    q = [_q("q", "a", "b", ms[1]["known_at"] + 1)]
    assert ds.build_features(ms, q) == ds.build_features(list(reversed(ms)), q)


def test_form_goals_and_shots_use_the_last_five():
    ms = [_m(f"m{i}", "a", f"x{i}", i, 1, 0, shots=(10, 2)) for i in range(7)]
    f = ds.build_features(ms, [_q("q", "a", "b", ms[-1]["known_at"] + 1)])["q"]
    assert f["form_h"] == 3.0                      # won all of the last five
    assert f["gf_h"] == 1.0 and f["ga_h"] == 0.0
    assert f["sh_h"] == 10.0 and f["seen_h"] == 7


def test_missing_shots_stay_missing():
    ms = [_m("m1", "a", "b", 0, 1, 0)]
    f = ds.build_features(ms, [_q("q", "a", "b", ms[0]["known_at"] + 1)])["q"]
    assert f["sh_h"] is None


def test_rest_days_count_from_the_last_kickoff():
    ms = [_m("m1", "a", "b", 0, 1, 0)]
    f = ds.build_features(ms, [_q("q", "a", "c", ms[0]["kickoff"] + 5 * D)])["q"]
    assert f["rest_h"] == pytest.approx(5.0)
    assert f["rest_a"] is None


def test_home_and_away_records_are_venue_specific():
    ms = [_m("m1", "a", "b", 0, 1, 0), _m("m2", "c", "a", 3, 1, 0)]
    f = ds.build_features(ms, [_q("q", "a", "c", ms[1]["known_at"] + 1)])["q"]
    assert f["home_wr_h"] == 1.0 and f["home_n_h"] == 1     # a won its one home game
    assert f["away_wr_a"] is None                            # c has never played away


def test_head_to_head_is_oriented_to_the_current_home_team():
    ms = [_m("m1", "a", "b", 0, 2, 0), _m("m2", "b", "a", 7, 0, 1)]
    f = ds.build_features(ms, [_q("q", "b", "a", ms[1]["known_at"] + 1)])["q"]
    assert f["h2h_n"] == 2 and f["h2h_home_wr"] == 0.0      # b lost both


def test_league_rates_use_the_previous_365_days_only():
    old = _m("m0", "a", "b", 0, 1, 1)                        # a draw, long ago
    recent = _m("m1", "c", "d", 400, 1, 0)
    f = ds.build_features([old, recent], [_q("q", "a", "b", recent["known_at"] + 1)])["q"]
    assert f["lg_n"] == 1 and f["lg_draw"] == 0.0 and f["lg_home"] == 1.0


def test_join_kalshi_matches_names_and_kickoff_window():
    cands = [{"id": "x", "home": "IFK Göteborg", "away": "Halmstads BK",
              "kickoff": 1_000_000},
             {"id": "y", "home": "AIK", "away": "Västerås SK", "kickoff": 1_000_000}]
    hit = ds.join_kalshi("Goteborg", "Halmstad", 1_000_000 + 3600, cands)
    assert hit == ("x", False)
    assert ds.join_kalshi("Halmstad", "Goteborg", 1_000_000, cands) == ("x", True)
    assert ds.join_kalshi("Goteborg", "Halmstad", 1_000_000 + 40 * H, cands) is None


def test_join_kalshi_refuses_ambiguous():
    cands = [{"id": "x", "home": "IFK Göteborg", "away": "Halmstads BK", "kickoff": 1_000_000},
             {"id": "z", "home": "IFK Göteborg", "away": "Halmstads BK",
              "kickoff": 1_000_000 + 20 * H}]
    assert ds.join_kalshi("Goteborg", "Halmstad", 1_000_000 + 10 * H, cands) is None
