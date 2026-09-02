"""
test_arena_v2.py — offline deterministic test of the v2 arena loop's settle +
evolve paths (the parts that can't be exercised live without waiting for real
games to resolve). Uses a fake client and synthetic teams.

    python3 test_arena_v2.py
"""
import random

from group.paper import PaperAccount
from brain_v2 import BrainV2
import arena_v2 as a
import strategy_v2 as sv


class FakeClient:
    def __init__(self, results):
        self._results = results      # ticker -> "yes"/"no"

    def list_markets_by_tickers(self, tickers):
        return [{"ticker": tk, "status": "settled", "result": self._results[tk]}
                for tk in tickers if tk in self._results]


def _team_with_bet(cat, tid, ticker, entry, contracts, sources):
    t = a._new_team(cat, sv.seed_params()["aggressive_hold"], "test", 0, tid)
    acc = PaperAccount.from_dict(t["account"])
    acc.buy(ticker, contracts, entry, {"sub": "x"})
    cost = contracts * entry
    t["account"] = acc.to_dict()
    t["staked"] = cost
    t["bets"][ticker] = {"sources": sources, "p_fair": sources["data"], "ask": int(entry * 100),
                         "edge": 0.1, "contracts": contracts, "cost": cost}
    return t


def test_settle():
    # team A bet a winner @0.40, team B bet a loser @0.60
    tA = _team_with_bet("game_lines", 1, "TK-WIN", 0.40, 10, {"data": 0.6, "market": 0.45})
    tB = _team_with_bet("game_lines", 2, "TK-LOSE", 0.60, 10, {"data": 0.4, "market": 0.55})
    teams_by_cat = {"game_lines": [tA, tB]}
    brains = {"game_lines": BrainV2()}
    client = FakeClient({"TK-WIN": "yes", "TK-LOSE": "no"})
    res = a._settle(client, teams_by_cat, brains, cycle=1)
    assert res["settled"] == 2, res
    assert abs(tA["account"]["realized_pnl"] - 6.0) < 1e-6, tA["account"]  # 10*(1-.4)
    assert abs(tB["account"]["realized_pnl"] + 6.0) < 1e-6, tB["account"]  # 10*(0-.6)
    assert tA["n_closed"] == 1 and tB["n_closed"] == 1
    assert tA["fitness"] > 0 > tB["fitness"]
    assert not tA["bets"] and not tB["bets"]   # positions cleared
    print(f"  settle OK: A pnl={tA['account']['realized_pnl']:+.1f} fit={tA['fitness']:+.3f} | "
          f"B pnl={tB['account']['realized_pnl']:+.1f} fit={tB['fitness']:+.3f}")


def test_brain_assistant():
    """Stacker should shift weight toward the lower-Brier source after a round."""
    brain = BrainV2()
    w0 = dict(brain.weights)
    rng = random.Random(0)
    resolved = []
    for _ in range(40):
        y = rng.random() < 0.5
        # 'data' is well-calibrated; 'market' is noise around 0.5
        resolved.append({"sources": {"data": 0.85 if y else 0.15,
                                     "market": 0.5}, "outcome": 1 if y else 0})
    brain.update_stacker(resolved)
    print(f"  brain assistant: data {w0['data']:.2f}->{brain.weights['data']:.2f}, "
          f"market {w0['market']:.2f}->{brain.weights['market']:.2f}")
    assert brain.weights["data"] > w0["data"], brain.weights


def test_evolve():
    # 8 teams, all with enough closed bets, distinct fitness
    teams = []
    for i in range(8):
        t = a._new_team("game_lines", sv.seed_params()["aggressive_hold"], "random", 0, i)
        t["n_closed"] = 10
        t["fitness"] = float(i)          # team 0 worst, team 7 best
        teams.append(t)
    teams_by_cat = {"game_lines": teams}
    next_id = [100]
    summary = a._evolve(teams_by_cat, cycle=2, next_id=next_id)
    new = teams_by_cat["game_lines"]
    ids = {t["id"] for t in new}
    assert len(new) == 8, len(new)            # population size preserved
    assert 0 not in ids, "worst team should be culled"
    assert any(t["lineage"].startswith("child") for t in new), "a child should be bred"
    print(f"  evolve OK: {summary['game_lines']}; survivors={sorted(ids)}")


def test_live_pricing():
    """A favorite leading 1-0 at 80' should have a very high live win prob, and a
    much higher one than pre-game."""
    brain = BrainV2()
    pre = brain.game_prior("Argentina", "Algeria", market_total=2.6)
    pwin_pre = brain.data_pfair({"type": "winner", "period": "full", "team": "Argentina"},
                                pre, True)
    lp = brain.live_prior("Argentina", "Algeria", minute=80, home_score=1,
                          away_score=0, market_total=2.6)
    pwin_live = brain.live_pfair({"type": "winner", "period": "full", "team": "Argentina"},
                                 lp, True)
    print(f"  Argentina win: pregame {pwin_pre:.3f} -> live(1-0, 80') {pwin_live:.3f}")
    assert pwin_live > 0.9 and pwin_live > pwin_pre, (pwin_pre, pwin_live)


def test_exits():
    """A held position that becomes overpriced live should be sold by an active-exit
    team and recorded as a closed (exited) bet."""
    t = _team_with_bet("game_lines", 1, "TK-X", 0.40, 10, {"data": 0.5, "market": 0.42})
    t["params"] = sv.clip(sv.seed_params()["conservative_active"])
    game = {"event": "E", "legs": [{"ticker": "TK-X", "in_play": True,
                                    "p_fair": 0.50, "bid": 92, "ask": 93}]}
    teams_by_cat = {"game_lines": [t]}
    games_by_cat = {"game_lines": [game]}
    res = a._exits(teams_by_cat, games_by_cat)
    assert res["exited"] >= 1, res
    assert "TK-X" not in t["bets"], "fully-sold position should be cleared"
    assert t["account"]["realized_pnl"] > 0, t["account"]  # sold @0.92 vs entry 0.40
    print(f"  exits OK: realized={t['account']['realized_pnl']:+.2f} "
          f"closed={t['n_closed']}")


if __name__ == "__main__":
    print("settle:"); test_settle()
    print("brain assistant:"); test_brain_assistant()
    print("evolve:"); test_evolve()
    print("live pricing:"); test_live_pricing()
    print("exits:"); test_exits()
    print("\nALL PASS")
