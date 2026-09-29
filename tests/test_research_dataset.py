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


# ── 0b: filling Jun–Sep results from ESPN and Kalshi ─────────────────────────

def _espn(eid, date, home, away, hs, as_, status="STATUS_FULL_TIME"):
    return {"id": eid, "date": date, "status": {"type": {"name": status}},
            "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"displayName": home}, "score": hs},
                {"homeAway": "away", "team": {"displayName": away}, "score": as_}]}]}


def test_espn_results_use_the_90_minute_outcome():
    games = ds.espn_results([
        _espn("1", "2026-09-12T15:30Z", "IFK Göteborg", "Halmstads BK", "2", "1"),
        _espn("2", "2026-09-12T15:30Z", "A", "B", "3", "2", "STATUS_FINAL_AET"),
        _espn("3", "2026-09-12T15:30Z", "C", "D", "1", "1", "STATUS_FINAL_PEN"),
        _espn("4", "2026-09-12T15:30Z", "E", "F", "0", "0", "STATUS_SCHEDULED"),
    ])
    by = {g["espn_id"]: g for g in games}
    assert set(by) == {"1", "2", "3"}
    assert by["1"]["res"] == "home" and by["1"]["hg"] == 2
    assert by["2"]["res"] == "draw" and by["2"]["hg"] is None     # level after 90'
    assert by["3"]["res"] == "draw"


REG = [("IFK Göteborg", "goteborg", "Allsvenskan", 100),
       ("Halmstads BK", "halmstad", "Allsvenskan", 100),
       ("Nacional", "nacional_uru", "Uruguay", 100),
       ("Club Nacional", "nacional_par", "Paraguay", 100),
       ("Peñarol", "penarol", "Uruguay", 100)]


def test_resolve_pair_finds_keys_and_their_shared_league():
    idx = ds.TeamIndex(REG)
    assert idx.resolve_pair("Goteborg", "Halmstad") == ("goteborg", "halmstad", "Allsvenskan")


def test_resolve_pair_uses_the_opponent_to_pick_the_right_namesake():
    idx = ds.TeamIndex(REG)
    assert idx.resolve_pair("Nacional", "Penarol") == ("nacional_uru", "penarol", "Uruguay")


def test_resolve_pair_refuses_unknown_teams():
    idx = ds.TeamIndex(REG)
    assert idx.resolve_pair("Goteborg", "Nowhere United") is None


def test_state_accepts_a_result_without_goals():
    m = _m("m1", "a", "b", 0, None, None)
    m["res"] = "draw"
    f = ds.build_features([m], [_q("q", "a", "b", m["known_at"] + 1)])["q"]
    assert f["form_h"] == 1.0 and f["gf_h"] is None and f["seen_h"] == 1


def test_kalshi_outcome_from_settled_legs():
    legs = [("T1", "Goteborg", "no"), ("T2", "Tie", "yes"), ("T3", "Halmstad", "no")]
    assert ds.kalshi_outcome(legs, "Goteborg", "Halmstad") == "draw"
    legs = [("T1", "Goteborg", "yes"), ("T2", "Tie", "no"), ("T3", "Halmstad", "no")]
    assert ds.kalshi_outcome(legs, "Goteborg", "Halmstad") == "home"
    assert ds.kalshi_outcome(legs[:2], "Goteborg", "Halmstad") is None   # incomplete


def test_extra_matches_skip_games_already_in_soccer_db():
    idx = ds.TeamIndex(REG)
    existing = [{"id": "x", "home_key": "goteborg", "away_key": "halmstad",
                 "kickoff": 1_789_227_000}]
    espn = ds.espn_results([_espn("1", "2026-09-12T15:30Z", "IFK Göteborg", "Halmstads BK",
                                  "2", "1")])
    assert ds.extra_matches(existing, espn, [], idx) == []
    new = ds.extra_matches([], espn, [], idx)
    assert len(new) == 1 and new[0]["league"] == "Allsvenskan" and new[0]["source"] == "espn"
