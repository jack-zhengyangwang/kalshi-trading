#!/usr/bin/env python3
"""
train_replay.py — chronological PAPER replay-trainer over RESOLVED games.

Replays each finished game from ~24h before kickoff (when the Kalshi market is
fully live) through resolution, with the 4 tournament teams trading on paper
(real candlestick prices, settle $1/$0). After EACH game resolves, the ENHANCED
Agent 3 (agents/tournament_trainer) runs so params evolve game-by-game:
  • over-conservative / idle  → lower min_edge, raise kelly (regret of missed $)
  • sizing error (good p_fair, poor ROI) → fix the Trader's kelly
  • pricing error (high Brier) → fix the Brain: reweight sources (+ reprompt flag)

No real orders. Does NOT touch the live paper logs (in-memory ledgers only), so
the running live tournament is unaffected.

    python3 train_replay.py --series KXWCGAME --league fifa.world
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from group.run import _load_env_from_zshenv          # noqa: E402
from kalshi_client import KalshiClient               # noqa: E402
from group.brain import Brain                         # noqa: E402
from group.paper import PaperAccount                  # noqa: E402
from group.strategy import build_strategy             # noqa: E402
import backtest as bt                                 # noqa: E402  (reuse helpers)
from agents.tournament_trainer import tune_from_ledger, reweight_brain  # noqa: E402

STEP = 5   # in-play poll granularity (minutes) — enough for exit decisions


def _pregame_quote(client, series, ticker, ko):
    """(bid, ask) ~24h before kickoff — the earliest live two-sided quote."""
    path = f"/series/{series}/markets/{ticker}/candlesticks"
    try:
        r = client.session.get(client.base_url + path,
                               headers=client._auth_headers("GET", path),
                               params={"start_ts": ko - 86400, "end_ts": ko - 1800,
                                       "period_interval": 60}, timeout=20)
        cs = r.json().get("candlesticks", []) if r.ok else []
    except Exception:
        cs = []
    for c in cs:
        bid = (c.get("yes_bid") or {}).get("close_dollars")
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        if bid not in (None, "") and ask not in (None, ""):
            return float(bid), float(ask)
    return None, None


def _mkt(leg, game, bid, ask):
    return {"ticker": leg["ticker"], "title": f"{game['home']} vs {game['away']} Winner?",
            "yes_sub_title": leg["sub"],
            "yes_bid_cents": round(bid * 100) if bid else None,
            "yes_ask_cents": round(ask * 100) if ask else None,
            "volume_yes": 0, "volume_no": 0, "category": "sports"}


def replay_and_record(client, brain, series, league, game, teams):
    """Replay one game; return {name: {bets, skipped}} ledger for Agent 3."""
    legs = game["legs"]
    espn_id, ko_iso = bt.find_espn(league, game["yyyymmdd"], game["home"], game["away"])
    goals = bt.get_goals(league, espn_id) if espn_id else []
    ko = bt._epoch(ko_iso) if ko_iso else bt._epoch(
        f"{game['yyyymmdd'][:4]}-{game['yyyymmdd'][4:6]}-{game['yyyymmdd'][6:]}T19:00:00Z")
    bt._LLM_CACHE.clear()
    paths = {l["ticker"]: bt.get_price_path(client, series, l["ticker"], ko) for l in legs}
    pregame_q = {l["ticker"]: _pregame_quote(client, series, l["ticker"], ko) for l in legs}
    res = {l["ticker"]: l["result"] for l in legs}
    fh, fa = bt.score_at(goals, 130)
    print(f"\n── {game['home']} {fh}-{fa} {game['away']}  ({game['yyyymmdd']})")
    ledger = {t["name"]: {"bets": [], "skipped": []} for t in teams}

    # ── pregame entries at ~T-24h ──────────────────────────────────────────
    gs0 = {"status": "pre", "minute": 0, "home_score": 0, "away_score": 0,
           "home_team": game["home"], "away_team": game["away"]}
    for leg in legs:
        bid, ask = pregame_q[leg["ticker"]]
        if not ask:
            continue
        mkt = _mkt(leg, game, bid, ask)
        pg = brain.evaluate(mkt)
        comp = {"w_llm": pg.get("p_fair_llm"), "w_model": pg.get("p_fair_model"),
                "w_data": pg.get("p_fair_data")}
        for team in teams:
            acct, strat, tk = team["account"], team["strategy"], leg["ticker"]
            ctx = {"market": mkt, "pregame": pg, "live": None, "ask": ask, "bid": bid,
                   "balance": acct.cash, "tau_days": 1.0, "game_state": gs0}
            dollars = strat.entry_dollars(ctx)
            if dollars > 0 and tk not in acct.positions:
                n = max(1, int(dollars / ask))
                if acct.buy(tk, n, ask, {"sub": leg["sub"], "game": game["prefix"]}):
                    ledger[team["name"]]["bets"].append(
                        {"tk": tk, "p_fair": pg["p_fair"], "comp": comp, "entry": ask,
                         "contracts": n, "dollars": n * ask, "outcome": res.get(tk)})
            elif pg["p_fair"] > ask:    # Brain liked it, team passed → regret candidate
                ledger[team["name"]]["skipped"].append(
                    {"tk": tk, "p_fair": pg["p_fair"], "ask": ask, "outcome": res.get(tk)})

    # ── in-play exits ──────────────────────────────────────────────────────
    last = 95 if espn_id else 0
    for minute in range(1, last + 1, STEP):
        h, a = bt.score_at(goals, minute)
        gs = {"status": "in", "minute": minute, "home_score": h, "away_score": a,
              "home_team": game["home"], "away_team": game["away"]}
        for leg in legs:
            bid, ask = bt._price_at(paths[leg["ticker"]], minute)
            if bid is None:
                continue
            mkt = _mkt(leg, game, bid, ask)
            prev = bt._LLM_CACHE.get(leg["ticker"], (None, None))
            live = brain.evaluate_live(mkt, gs, prev_score=prev[0], prev_llm=prev[1])
            bt._LLM_CACHE[leg["ticker"]] = (live["score_key"], live["llm_for_cache"])
            for team in teams:
                acct, strat, tk = team["account"], team["strategy"], leg["ticker"]
                ctx = {"market": mkt, "pregame": None, "live": live, "ask": ask,
                       "bid": bid, "balance": acct.cash, "tau_days": 0, "game_state": gs}
                # IN-PLAY ENTRY — C (late-scalp) and D (momentum) can only enter
                # here. Without this they never get an opportunity to trade.
                if tk not in acct.positions:
                    dollars = strat.entry_dollars(ctx)
                    if dollars > 0 and ask:
                        n = max(1, int(dollars / ask))
                        if acct.buy(tk, n, ask, {"sub": leg["sub"], "game": game["prefix"]}):
                            ledger[team["name"]]["bets"].append(
                                {"tk": tk, "p_fair": live["live_p_fair"], "comp": {},
                                 "entry": ask, "contracts": n, "dollars": n * ask,
                                 "outcome": res.get(tk)})
                    continue
                action, frac = strat.exit_decision(acct.positions[tk], ctx)
                if action != "HOLD" and frac > 0:
                    pos = acct.positions[tk]
                    want = max(1, min(int(round(pos["contracts"] * frac)), pos["contracts"]))
                    acct.sell(tk, want, bid)
                    if action == "OVERPRICED":
                        pos["_op_armed"] = False
                    elif action == "TAKE_PROFIT":
                        pos["_tp_done"] = True

    # ── settle + finalize the learning signal (hold-to-resolution baseline) ──
    for team in teams:
        for tk in list(team["account"].positions):
            if res.get(tk) is not None:
                team["account"].settle(tk, res[tk])
    for led in ledger.values():
        for b in led["bets"]:
            o = b["outcome"]
            b["pnl"] = b["contracts"] * ((o if o is not None else b["entry"]) - b["entry"])
    return ledger


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXWCGAME")
    ap.add_argument("--league", default="fifa.world")
    args = ap.parse_args()

    _load_env_from_zshenv()
    config = json.load(open(os.path.join(BASE_DIR, "group", "config.json")))
    tcfg = json.load(open(os.path.join(BASE_DIR, "tournament_config.json")))
    config["exclude_event_suffixes"] = []   # historical replay — bet every finished game
    config["use_dominance"] = False         # past games have no live ESPN stats
    client = KalshiClient()
    brain = Brain(config)

    teams = [{"name": t["name"],
              "strategy": build_strategy(t["kind"], t["name"], dict(t["params"])),
              "account": PaperAccount(t["name"], tcfg.get("starting_cash", 200.0))}
             for t in tcfg["teams"]]

    games = bt.discover_games(client, args.series)   # chronological
    print(f"=== replay-trainer: {len(games)} resolved {args.series} games, "
          f"4 teams on paper, Agent 3 after each ===")
    pooled = []
    for g in games:
        ledger = replay_and_record(client, brain, args.series, args.league, g, teams)
        for t in teams:
            pooled += ledger[t["name"]]["bets"]
        note = reweight_brain(pooled, config)         # global pricing fix
        if note:
            print(f"   [BRAIN] {note}")
        for t in teams:
            r = tune_from_ledger(t["strategy"], ledger[t["name"]])
            brier = f"{r['brier']:.2f}" if r["brier"] is not None else "—"
            tag = " | ".join(r["diag"]) if r["diag"] else "no change"
            print(f"   [A3 {t['name']:<22}] n={r['n']} roi={r['roi']:+.0%} brier={brier} "
                  f"miss=${r['missed']:.1f} → kelly={r['kelly']} edge={r['min_edge']} | {tag}")

    print("\n=== FINAL (after replay-training) ===")
    for t in sorted(teams, key=lambda x: x["account"].realized_pnl, reverse=True):
        a, p = t["account"], t["strategy"].p
        print(f"  {a.name:<24} P&L {a.realized_pnl:+.2f}  trades={a.trades} "
              f"settled={a.settled} | kelly={p['kelly_fraction']} min_edge={p['min_edge']}")

    # Persist the per-team learned params as a warm-start for the LIVE paper
    # tournament (it loads tournament/<name>_tuned.json). Paper-only — does NOT
    # touch the live wallet's group/config.json or trainer_state.json.
    state_dir = os.path.join(BASE_DIR, "tournament")
    os.makedirs(state_dir, exist_ok=True)
    for t in teams:
        p = t["strategy"].p
        path = os.path.join(state_dir, f"{t['name']}_tuned.json")
        tmp = path + ".tmp"
        json.dump({"kelly_fraction": p["kelly_fraction"], "min_edge": p["min_edge"]},
                  open(tmp, "w"), indent=2)
        os.replace(tmp, path)
    print("  → persisted per-team params to tournament/*_tuned.json (paper warm-start)")
    print(f"\n  Brain pricing RECOMMENDATION (NOT auto-applied to the live wallet): "
          f"brain_weights → {config.get('brain_weights')}")


if __name__ == "__main__":
    main()
