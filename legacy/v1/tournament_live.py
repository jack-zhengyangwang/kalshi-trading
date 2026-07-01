#!/usr/bin/env python3
"""
tournament_live.py — ONE continuous paper tournament, started 24h before the
first World Cup game and run forward forever.

Each invocation: play every RESOLVED-but-not-yet-played game in chronological
order — replayed from ~24h pre-kickoff through resolution so the agents react to
the unfolding game (pre-game bets, goals, exits), settle $1/$0 — then run the
FULL Agent 3 per team and print a running PnL tally. First run = catch-up from
the Mexico game to now; later runs play games as they finish (the "live" part).

Per team, Agent 3 fixes whichever agent erred (not just kelly):
  • p_fair off → reweight the Brain's sources AND fix the culprit
      (LLM → prompt addendum; model → calibration shift)
  • sizing off → kelly / min_edge
Each team carries its OWN Brain (weights, LLM prompt note, model calibration),
Claude-seeded at init, so the teams diverge as they learn.

PAPER ONLY — never places real orders.   Run:  python3 tournament_live.py --once
"""
import argparse
import copy
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from group.run import _load_env_from_zshenv          # noqa: E402
from kalshi_client import KalshiClient               # noqa: E402
from group.brain import Brain                         # noqa: E402
from group.paper import PaperAccount                  # noqa: E402
from group.strategy import build_strategy             # noqa: E402
import backtest as bt                                 # noqa: E402
from agents import tournament_trainer as A3           # noqa: E402
from group import live_feed                            # noqa: E402
from group.scanner import _VS_RE, _hours_until         # noqa: E402

PREGAME_EVERY = 1800     # re-evaluate a pre-game market at most this often (low freq)
PREGAME_WINDOW_H = 30    # only pre-game games kicking off within this many hours

STATE_DIR = os.path.join(BASE_DIR, "tournament_v2")
PLAYED = os.path.join(STATE_DIR, "played.json")
TALLY = os.path.join(BASE_DIR, "logs", "tournament_v2.jsonl")
TCONFIG = os.path.join(BASE_DIR, "tournament_config.json")
GCONFIG = os.path.join(BASE_DIR, "group", "config.json")
STEP = 5            # in-play poll granularity (minutes)
START_CASH = 200.0


def _team_path(name):
    return os.path.join(STATE_DIR, f"{name}.json")


def _save_json(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=2)
    os.replace(tmp, path)


def _build_brain(base_cfg, brain_state):
    cfg = copy.deepcopy(base_cfg)
    cfg["brain_weights"] = brain_state.get("brain_weights", cfg.get("brain_weights"))
    cfg["llm_addendum"] = brain_state.get("llm_addendum", "")
    cfg["model_calibration"] = brain_state.get("model_calibration")
    # Dominance ON: live games get real shots/possession stats; resolved-but-past
    # games simply aren't on ESPN's live board so it falls back to (1.0, 1.0).
    cfg["use_dominance"] = True
    b = Brain(cfg)
    b._live_llm_estimate = lambda m, gs: (None, "")   # in-play uses Poisson+market (feasible)
    return b


def reset_and_seed(base_cfg, tcfg):
    """Wipe state and Claude-seed each team's rough untrained starting params."""
    os.makedirs(STATE_DIR, exist_ok=True)
    for f in os.listdir(STATE_DIR):
        os.remove(os.path.join(STATE_DIR, f))
    open(TALLY, "w").close()
    seed_brain = Brain(copy.deepcopy(base_cfg))   # plain Brain just for the LLM call
    print("=== RESET — Claude seeding rough starting params per team ===", flush=True)
    for t in tcfg["teams"]:
        seed = A3.seed_params_via_llm(seed_brain, t["name"], t["kind"])
        params = {**t["params"]}
        if "kelly_fraction" in seed:
            params["kelly_fraction"] = seed["kelly_fraction"]
        if "min_edge" in seed:
            params["min_edge"] = seed["min_edge"]
        brain_state = {"brain_weights": seed.get("brain_weights",
                       {"w_llm": 0.34, "w_model": 0.33, "w_data": 0.33}),
                       "llm_addendum": "", "model_calibration": None}
        state = {"kind": t["kind"], "params": params, "brain": brain_state,
                 "account": PaperAccount(t["name"], START_CASH).to_dict(),
                 "ledger": {"bets": [], "skipped": []}}
        _save_json(_team_path(t["name"]), state)
        print(f"  {t['name']}: kelly={params.get('kelly_fraction')} "
              f"min_edge={params.get('min_edge')} weights={brain_state['brain_weights']}", flush=True)
    _save_json(PLAYED, [])


def load_teams(base_cfg, tcfg):
    teams = []
    for t in tcfg["teams"]:
        st = json.load(open(_team_path(t["name"])))
        teams.append({
            "name": t["name"], "kind": st["kind"], "state": st,
            "strategy": build_strategy(st["kind"], t["name"], st["params"]),
            "brain": _build_brain(base_cfg, st["brain"]),
            "account": PaperAccount.from_dict(st["account"]),
            "ledger": st["ledger"],
        })
    return teams


def _mkt(leg, game, bid, ask):
    return {"ticker": leg["ticker"], "title": f"{game['home']} vs {game['away']} Winner?",
            "yes_sub_title": leg["sub"],
            "yes_bid_cents": round(bid * 100) if bid else None,
            "yes_ask_cents": round(ask * 100) if ask else None,
            "volume_yes": 0, "volume_no": 0, "category": "sports"}


def play_game(client, game, league, teams):
    """Replay one resolved game from T-24h through resolution for all teams (each
    with its own Brain), settle, and append to each team's cumulative ledger."""
    legs = game["legs"]
    espn_id, ko_iso = bt.find_espn(league, game["yyyymmdd"], game["home"], game["away"])
    goals = bt.get_goals(league, espn_id) if espn_id else []
    ko = bt._epoch(ko_iso) if ko_iso else bt._epoch(
        f"{game['yyyymmdd'][:4]}-{game['yyyymmdd'][4:6]}-{game['yyyymmdd'][6:]}T19:00:00Z")
    paths, pregame_q = {}, {}
    for l in legs:
        paths[l["ticker"]] = bt.get_price_path(client, game["series"], l["ticker"], ko)
        pregame_q[l["ticker"]] = _pregame_quote(client, game["series"], l["ticker"], ko)
    res = {l["ticker"]: l["result"] for l in legs}
    deltas = {t["name"]: t["account"].realized_pnl for t in teams}
    gbets = {t["name"]: [] for t in teams}     # this game's bets, per team
    gskip = {t["name"]: [] for t in teams}     # this game's skipped +EV legs

    # pre-game entries at ~T-24h (per-team Brain)
    gs0 = {"status": "pre", "minute": 0, "home_score": 0, "away_score": 0,
           "home_team": game["home"], "away_team": game["away"]}
    for leg in legs:
        bid, ask = pregame_q[leg["ticker"]]
        if not ask:
            continue
        mkt = _mkt(leg, game, bid, ask)
        for t in teams:
            pg = t["brain"].evaluate(mkt)
            comp = {"w_llm": pg.get("p_fair_llm"), "w_model": pg.get("p_fair_model"),
                    "w_data": pg.get("p_fair_data")}
            ctx = {"market": mkt, "pregame": pg, "live": None, "ask": ask, "bid": bid,
                   "balance": t["account"].cash, "tau_days": 1.0, "game_state": gs0}
            tk = leg["ticker"]
            d = t["strategy"].entry_dollars(ctx)
            if d > 0 and tk not in t["account"].positions:
                n = max(1, int(d / ask))
                if t["account"].buy(tk, n, ask, {"sub": leg["sub"], "game": game["prefix"]}):
                    gbets[t["name"]].append({"tk": tk, "p_fair": pg["p_fair"], "comp": comp,
                                             "entry": ask, "contracts": n, "dollars": n * ask,
                                             "outcome": res.get(tk)})
            elif pg["p_fair"] > ask:
                gskip[t["name"]].append({"tk": tk, "p_fair": pg["p_fair"], "ask": ask,
                                         "outcome": res.get(tk)})

    # in-play: entries (C/D) + exits, per-team
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
            tk = leg["ticker"]
            for t in teams:
                live = t["brain"].evaluate_live(mkt, gs)
                ctx = {"market": mkt, "pregame": None, "live": live, "ask": ask, "bid": bid,
                       "balance": t["account"].cash, "tau_days": 0, "game_state": gs}
                if tk not in t["account"].positions:
                    d = t["strategy"].entry_dollars(ctx)
                    if d > 0 and ask:
                        n = max(1, int(d / ask))
                        if t["account"].buy(tk, n, ask, {"sub": leg["sub"], "game": game["prefix"]}):
                            gbets[t["name"]].append({"tk": tk, "p_fair": live["live_p_fair"],
                                                     "comp": {}, "entry": ask, "contracts": n,
                                                     "dollars": n * ask, "outcome": res.get(tk)})
                    continue
                action, frac = t["strategy"].exit_decision(t["account"].positions[tk], ctx)
                if action != "HOLD" and frac > 0:
                    pos = t["account"].positions[tk]
                    want = max(1, min(int(round(pos["contracts"] * frac)), pos["contracts"]))
                    t["account"].sell(tk, want, bid)
                    if action == "OVERPRICED":
                        pos["_op_armed"] = False
                    elif action == "TAKE_PROFIT":
                        pos["_tp_done"] = True

    # settle + finalize this game's learning signal (hold-to-resolution baseline),
    # then fold this game's bets/skipped into each team's cumulative ledger.
    out = {}
    for t in teams:
        for tk in list(t["account"].positions):
            if res.get(tk) is not None:
                t["account"].settle(tk, res[tk])
        for b in gbets[t["name"]]:
            o = b["outcome"]
            b["pnl"] = b["contracts"] * ((o if o is not None else b["entry"]) - b["entry"])
        t["ledger"]["bets"].extend(gbets[t["name"]])
        t["ledger"]["skipped"].extend(gskip[t["name"]])
        out[t["name"]] = {"delta": t["account"].realized_pnl - deltas[t["name"]],
                          "bets": gbets[t["name"]], "skipped": gskip[t["name"]]}
    return out


def _pregame_quote(client, series, ticker, ko):
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


def persist(teams):
    for t in teams:
        t["state"]["params"] = t["strategy"].p
        t["state"]["brain"] = {"brain_weights": t["brain"].config.get("brain_weights"),
                               "llm_addendum": t["brain"].config.get("llm_addendum", ""),
                               "model_calibration": t["brain"].config.get("model_calibration")}
        t["state"]["account"] = t["account"].to_dict()
        t["state"]["ledger"] = t["ledger"]
        _save_json(_team_path(t["name"]), t["state"])


# ── live (real-time) helpers ─────────────────────────────────────────────────

def _live_book(client, ticker):
    """(bid, ask) in dollars from the live Kalshi book, or (None, None)."""
    try:
        b = client.get_best_bid_cents(ticker)
        a = client.get_best_ask_cents(ticker)
    except Exception:
        return None, None
    return (b / 100.0 if b else None, a / 100.0 if a else None)


def _open_events(client, series):
    """{event_ticker: {home, away, legs:[{ticker, sub}]}} for tradeable (open) games."""
    out = {}
    for st in ("open", "active", "unopened"):
        try:
            d = client._get("/markets", params={"series_ticker": series, "status": st,
                                                "limit": 400}, auth=False)
        except Exception:
            continue
        for m in d.get("markets", []):
            et = m.get("event_ticker", "")
            if not et:
                continue
            e = out.setdefault(et, {"title": m.get("title", ""), "legs": [], "kickoff": ""})
            if not e["kickoff"]:
                e["kickoff"] = (m.get("occurrence_datetime") or m.get("close_time", "") or "")
            if not any(l["ticker"] == m["ticker"] for l in e["legs"]):
                e["legs"].append({"ticker": m["ticker"], "sub": m.get("yes_sub_title", "")})
    for e in out.values():
        mm = _VS_RE.search(e["title"])
        e["home"], e["away"] = (mm.group(1).strip(), mm.group(2).strip()) if mm else (None, None)
    return out


def pregame_step(client, teams, prefix, ev):
    """Low-freq pre-game: A/B (and any team whose pre-game logic fires) place bets
    on an upcoming game at the live Kalshi quote."""
    gs0 = {"status": "pre", "minute": 0, "home_score": 0, "away_score": 0,
           "home_team": ev["home"], "away_team": ev["away"]}
    acted = []
    for leg in ev["legs"]:
        bid, ask = _live_book(client, leg["ticker"])
        if not ask:
            continue
        mkt = _mkt(leg, ev, bid, ask)
        for t in teams:
            tk = leg["ticker"]
            if tk in t["account"].positions:
                continue
            pg = t["brain"].evaluate(mkt)
            comp = {"w_llm": pg.get("p_fair_llm"), "w_model": pg.get("p_fair_model"),
                    "w_data": pg.get("p_fair_data")}
            ctx = {"market": mkt, "pregame": pg, "live": None, "ask": ask, "bid": bid,
                   "balance": t["account"].cash, "tau_days": 1.0, "game_state": gs0}
            d = t["strategy"].entry_dollars(ctx)
            if d > 0:
                n = max(1, int(d / ask))
                if t["account"].buy(tk, n, ask, {"sub": leg["sub"], "game": prefix}):
                    t["ledger"]["bets"].append({"tk": tk, "p_fair": pg["p_fair"], "comp": comp,
                                                "entry": ask, "contracts": n, "dollars": n * ask,
                                                "outcome": None})
                    acted.append(f"{t['name']} pre BUY {leg['sub']} {n}@{ask:.2f}")
    return acted


def inplay_step(client, teams, prefix, ev, gs):
    """HIGH-freq in-play: stream the live Brain signal (live score + DOMINANCE +
    live book) to C/D for entries, and run every team's exits — on the real book."""
    try:
        from group import dominance
        stats = live_feed.get_game_stats(ev["home"], ev["away"])
        gs["_dom"] = (dominance.dominance_multipliers(stats["home"], stats["away"])
                      if stats else (1.0, 1.0))
    except Exception:
        gs["_dom"] = (1.0, 1.0)
    acted = []
    for leg in ev["legs"]:
        bid, ask = _live_book(client, leg["ticker"])
        if bid is None and ask is None:
            continue
        mkt = _mkt(leg, ev, bid, ask)
        for t in teams:
            tk = leg["ticker"]
            live = t["brain"].evaluate_live(mkt, gs)
            ctx = {"market": mkt, "pregame": None, "live": live, "ask": ask, "bid": bid,
                   "balance": t["account"].cash, "tau_days": 0, "game_state": gs}
            if tk not in t["account"].positions:
                d = t["strategy"].entry_dollars(ctx)
                if d > 0 and ask:
                    n = max(1, int(d / ask))
                    if t["account"].buy(tk, n, ask, {"sub": leg["sub"], "game": prefix}):
                        t["ledger"]["bets"].append({"tk": tk, "p_fair": live["live_p_fair"],
                                                    "comp": {}, "entry": ask, "contracts": n,
                                                    "dollars": n * ask, "outcome": None})
                        acted.append(f"{t['name']} BUY {leg['sub']} {n}@{ask:.2f} "
                                     f"(fair {live['live_p_fair']:.2f}, dom {gs['_dom'][0]:.2f}/{gs['_dom'][1]:.2f})")
            else:
                action, frac = t["strategy"].exit_decision(t["account"].positions[tk], ctx)
                if action != "HOLD" and frac > 0 and bid:
                    pos = t["account"].positions[tk]
                    want = max(1, min(int(round(pos["contracts"] * frac)), pos["contracts"]))
                    t["account"].sell(tk, want, bid)
                    if action == "OVERPRICED":
                        pos["_op_armed"] = False
                    elif action == "TAKE_PROFIT":
                        pos["_tp_done"] = True
                    acted.append(f"{t['name']} {action} {leg['sub']} {want}@{bid:.2f}")
    return acted


def settle_and_learn(client, teams, game):
    """A live-managed game has resolved — settle positions, finalize the learning
    signal on its (live-placed) ledger bets, and return per-team game results for
    Agent 3. No candlestick replay (it was already traded live)."""
    res = {}
    for l in game["legs"]:
        try:
            m = client.get_market(l["ticker"])
            r = (m.get("result") or "").lower()
        except Exception:
            r = ""
        res[l["ticker"]] = 1 if r == "yes" else (0 if r == "no" else None)
    legtks = set(res)
    deltas = {t["name"]: t["account"].realized_pnl for t in teams}
    out = {}
    for t in teams:
        gb = []
        for b in t["ledger"]["bets"]:
            if b["tk"] in legtks and b.get("outcome") is None:
                o = res[b["tk"]]
                b["outcome"] = o
                b["pnl"] = (b["contracts"] * ((o if o is not None else b["entry"]) - b["entry"])
                            if o is not None else 0.0)
                gb.append(b)
        for tk in list(t["account"].positions):
            if res.get(tk) is not None:
                t["account"].settle(tk, res[tk])
        out[t["name"]] = {"delta": t["account"].realized_pnl - deltas[t["name"]],
                          "bets": gb, "skipped": []}
    return out


def _learn_and_tally(teams, g, gres, played, kind):
    line = [f"── {g.get('home','?')} vs {g.get('away','?')} ({g['prefix']}) {kind}"]
    for t in teams:
        r = A3.full_update(t["strategy"], t["brain"], t["ledger"],
                           gres[t["name"]]["bets"], gres[t["name"]]["skipped"])
        tag = " | ".join(r["diag"]) if r["diag"] else "no change"
        line.append(f"   {t['name']:<22} game Δ${gres[t['name']]['delta']:+.2f}  "
                    f"cum ${t['account'].realized_pnl:+.2f}  [A3: {tag}]")
    print("\n".join(line), flush=True)
    played.add(g["prefix"])
    persist(teams)
    _save_json(PLAYED, sorted(played))
    with open(TALLY, "a") as f:
        f.write(json.dumps({"game": g["prefix"],
                            "standings": {t["name"]: round(t["account"].realized_pnl, 2)
                                          for t in teams}}) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="(ignored — one cycle per run)")
    ap.add_argument("--series", default="KXWCGAME")
    ap.add_argument("--league", default="fifa.world")
    ap.add_argument("--reset", action="store_true", help="force a fresh reset + reseed")
    args = ap.parse_args()

    _load_env_from_zshenv()
    base_cfg = json.load(open(GCONFIG))
    tcfg = json.load(open(TCONFIG))
    client = KalshiClient()

    if args.reset or not os.path.exists(PLAYED):
        reset_and_seed(base_cfg, tcfg)

    teams = load_teams(base_cfg, tcfg)
    played = set(json.load(open(PLAYED)))
    managed_path = os.path.join(STATE_DIR, "live_managed.json")
    pregame_path = os.path.join(STATE_DIR, "pregame_ts.json")
    live_managed = set(json.load(open(managed_path))) if os.path.exists(managed_path) else set()
    pregame_ts = json.load(open(pregame_path)) if os.path.exists(pregame_path) else {}

    # 1. Resolved-but-unplayed games: live-managed → settle+learn; else → replay.
    games = bt.discover_games(client, args.series)
    by_prefix = {}
    for g in games:
        g["series"] = args.series
        by_prefix[g["prefix"]] = g
        if g["prefix"] in played:
            continue
        if g["prefix"] in live_managed:
            gres = settle_and_learn(client, teams, g)
            _learn_and_tally(teams, g, gres, played, "RESOLVED (live-traded)")
        else:
            gres = play_game(client, g, args.league, teams)
            _learn_and_tally(teams, g, gres, played, "RESOLVED (replay)")

    # 2. Open events: live → high-freq in-play (C/D); upcoming → throttled pre-game.
    events = _open_events(client, args.series)
    for prefix, ev in events.items():
        if prefix in played or not ev["home"]:
            continue
        gs = live_feed.get_game_state(ev["home"], ev["away"])
        if gs and gs["status"] == "in":
            acted = inplay_step(client, teams, prefix, ev, gs)
            live_managed.add(prefix)
            print(f"  [IN-PLAY] {ev['home']} {gs['home_score']}-{gs['away_score']} "
                  f"{ev['away']} {gs['minute']}': "
                  + ("; ".join(acted) if acted else "no action"), flush=True)
        elif (not gs or gs["status"] == "pre"):
            hrs = _hours_until(ev.get("kickoff", ""))
            if (hrs is not None and 0 < hrs <= PREGAME_WINDOW_H
                    and time.time() - pregame_ts.get(prefix, 0) > PREGAME_EVERY):
                acted = pregame_step(client, teams, prefix, ev)
                pregame_ts[prefix] = time.time()
                if acted:
                    print(f"  [PRE-GAME] {prefix}: " + "; ".join(acted), flush=True)

    persist(teams)
    _save_json(PLAYED, sorted(played))
    _save_json(managed_path, sorted(live_managed))
    _save_json(pregame_path, pregame_ts)

    print("\n=== RUNNING PnL TALLY ===", flush=True)
    for t in sorted(teams, key=lambda x: x["account"].realized_pnl, reverse=True):
        p, bw = t["strategy"].p, t["brain"].config.get("brain_weights")
        print(f"  {t['name']:<22} ${t['account'].realized_pnl:+.2f}  "
              f"open={len(t['account'].positions)} kelly={p['kelly_fraction']} "
              f"edge={p['min_edge']} weights={bw} "
              f"addendum={'Y' if t['brain'].config.get('llm_addendum') else '-'} "
              f"calib={t['brain'].config.get('model_calibration') or '-'}", flush=True)


if __name__ == "__main__":
    main()
