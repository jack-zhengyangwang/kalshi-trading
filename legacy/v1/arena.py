#!/usr/bin/env python3
"""
arena.py — multi-category paper tournament (cloud-portable).

Four market buckets, each with the same four strategy archetypes (A/B/C/D) =
16 teams competing on paper:
    winner       (KXWCGAME)            — LLM + ELO model + market
    spread       (KXWCSPREAD)          — ELO/Poisson model vs market
    team_props   (KXWCTEAMTOTAL)       — ELO/Poisson model vs market
    game_events  (KXWCTOTAL, KXWCBTTS) — ELO/Poisson model vs market

Each category has ONE shared Brain (price each leg once; the 4 strategies differ
only in sizing/exits). Agent 3 runs after every resolved game: a single PRICING
pass per category tunes the shared Brain (reweight model↔market, calibrate), and
a per-team SIZING pass tunes kelly/min_edge. Best team per category is the
promotion candidate for the real wallet (still off).

One continuous tournament from 24h before the first game: it catches up the
already-resolved games (replay), then trades live going forward — pre-game on a
throttle, in-play every cycle. PAPER ONLY.

Cloud-portable: keys via group/env_portable (env → .env → ~/.zshenv); one cycle
per invocation (`python3 arena.py`) so it runs under cron or launchd.
"""
import argparse
import copy
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from group.env_portable import load_keys              # noqa: E402
from kalshi_client import KalshiClient                # noqa: E402
from group.brain import Brain                          # noqa: E402
from group.paper import PaperAccount                   # noqa: E402
from group.strategy import build_strategy              # noqa: E402
from group import live_feed                            # noqa: E402
from group.scanner import _VS_RE, _hours_until         # noqa: E402
from agents import tournament_trainer as A3            # noqa: E402
import backtest as bt                                  # noqa: E402

ARENA = os.path.join(BASE_DIR, "arena_state")
PLAYED = os.path.join(ARENA, "played.json")
MANAGED = os.path.join(ARENA, "live_managed.json")
PREGAME_TS = os.path.join(ARENA, "pregame_ts.json")
TALLY = os.path.join(BASE_DIR, "logs", "arena.jsonl")
GCONFIG = os.path.join(BASE_DIR, "group", "config.json")
TCONFIG = os.path.join(BASE_DIR, "tournament_config.json")
ACONFIG = os.path.join(BASE_DIR, "categories.json")
STEP = 5
PREGAME_EVERY = 600
PREGAME_WINDOW_H = 30


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=2)
    os.replace(tmp, path)


def _load(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def _team_path(name):
    return os.path.join(ARENA, f"team_{name}.json")


def _brain_path(cat):
    return os.path.join(ARENA, f"brain_{cat}.json")


def _suffix(event_ticker):
    p = (event_ticker or "").split("-", 1)
    return p[1] if len(p) == 2 else None


# ── config ───────────────────────────────────────────────────────────────────

def _kind_params(tcfg):
    out = {}
    for t in tcfg["teams"]:
        out[t["kind"]] = t["params"]
    return out


def _team_specs(acfg):
    """One spec per (category, strategy). A category may override the global
    strategy sweep with its own `strategies` list (e.g. the scoreline specialist
    runs ONE bespoke team instead of the A/B/C/D four)."""
    specs = []
    for cat, cat_cfg in acfg["categories"].items():
        for s in cat_cfg.get("strategies", acfg["strategies"]):
            specs.append({"cat": cat, "kind": s["kind"],
                          "name": f"{cat}_{s['suffix']}"})
    return specs


# ── brains (one per category, shared pricing) ────────────────────────────────

def _build_brain(base_cfg, cat, cat_cfg, brain_state):
    cfg = copy.deepcopy(base_cfg)
    cfg["brain_weights"] = brain_state.get("brain_weights")
    cfg["llm_addendum"] = brain_state.get("llm_addendum", "")
    cfg["model_calibration"] = brain_state.get("model_calibration")
    cfg["use_dominance"] = True
    b = Brain(cfg)
    if not cat_cfg.get("llm", True):
        b._llm_estimate = lambda m: (None, "")        # adjacent: no LLM in pricing
    b._live_llm_estimate = lambda m, gs: (None, "")    # in-play feasibility
    return b


def reset_and_seed(base_cfg, tcfg, acfg):
    os.makedirs(ARENA, exist_ok=True)
    os.makedirs(os.path.dirname(TALLY), exist_ok=True)   # logs/ may not exist on a fresh box
    for f in os.listdir(ARENA):
        os.remove(os.path.join(ARENA, f))
    open(TALLY, "w").close()
    kp = _kind_params(tcfg)
    seed_brain = Brain(copy.deepcopy(base_cfg))
    print("=== RESET — Claude-seeding rough starting params (16 teams) ===", flush=True)
    for cat, cat_cfg in acfg["categories"].items():
        default_w = ({"w_llm": 0.34, "w_model": 0.33, "w_data": 0.33} if cat_cfg.get("llm")
                     else {"w_llm": 0.0, "w_model": 0.5, "w_data": 0.5})
        _save(_brain_path(cat), {"brain_weights": default_w, "llm_addendum": "",
                                 "model_calibration": None})
        for s in acfg["strategies"]:
            name = f"{cat}_{s['suffix']}"
            seed = A3.seed_params_via_llm(seed_brain, name, s["kind"])
            params = {**kp[s["kind"]]}
            for k in ("kelly_fraction", "min_edge"):
                if k in seed:
                    params[k] = seed[k]
            _save(_team_path(name), {
                "cat": cat, "kind": s["kind"], "params": params,
                "account": PaperAccount(name, acfg.get("starting_cash", 200.0)).to_dict(),
                "ledger": {"bets": [], "skipped": []}})
        print(f"  {cat}: 4 teams seeded, weights {default_w}", flush=True)
    _save(PLAYED, [])


def load_arena(base_cfg, acfg):
    brains = {}
    for cat, cat_cfg in acfg["categories"].items():
        brains[cat] = _build_brain(base_cfg, cat, cat_cfg, _load(_brain_path(cat), {}))
    teams = []
    for spec in _team_specs(acfg):
        st = _load(_team_path(spec["name"]), None)
        if st is None:
            continue
        teams.append({"name": spec["name"], "cat": spec["cat"], "kind": spec["kind"],
                      "state": st, "brain": brains[spec["cat"]],
                      "strategy": build_strategy(spec["kind"], spec["name"], st["params"]),
                      "account": PaperAccount.from_dict(st["account"]),
                      "ledger": st["ledger"]})
    return brains, teams


def persist(brains, teams):
    for cat, b in brains.items():
        _save(_brain_path(cat), {"brain_weights": b.config.get("brain_weights"),
                                 "llm_addendum": b.config.get("llm_addendum", ""),
                                 "model_calibration": b.config.get("model_calibration")})
    for t in teams:
        t["state"]["params"] = t["strategy"].p
        t["state"]["account"] = t["account"].to_dict()
        t["state"]["ledger"] = t["ledger"]
        _save(_team_path(t["name"]), t["state"])


# ── pricing (shared per category) ────────────────────────────────────────────

def _mkt(leg, home, away, bid, ask):
    return {"ticker": leg["ticker"], "title": f"{home} vs {away} Winner?",
            "yes_sub_title": leg["sub"],
            "yes_bid_cents": round(bid * 100) if bid else None,
            "yes_ask_cents": round(ask * 100) if ask else None,
            "volume_yes": 0, "volume_no": 0, "category": "sports"}


def price(cat_cfg, brain, mkt, matchup, mtotal, gs):
    """Return (phase, result_dict, comp) priced ONCE per leg; phase ∈ pregame/live.
    result_dict is shaped for the strategies (p_fair/sigma/p_fair_lo or live_p_fair)."""
    live = bool(gs and gs.get("status") == "in")
    if cat_cfg["pricing"] == "winner":
        if live:
            r = brain.evaluate_live(mkt, gs)
            return "live", r, {}
        r = brain.evaluate(mkt)
        comp = {"w_llm": r.get("p_fair_llm"), "w_model": r.get("p_fair_model"),
                "w_data": r.get("p_fair_data")}
        return "pregame", r, comp
    if cat_cfg["pricing"] == "scoreline":
        if not matchup or not matchup[0]:
            return None, None, {}
        r = brain.evaluate_scoreline(mkt, matchup[0], matchup[1], game_state=gs,
                                     market_total=mtotal)
        if not r:
            return None, None, {}
        p, pm = r["p_fair"], (r.get("p_market") if r.get("p_market") is not None else r["p_fair"])
        sigma = min(0.20, max(0.03, abs(p - pm) * 1.2 + 0.03))
        comp = {"w_model": r.get("p_fair_model"), "w_data": r.get("p_fair_data")}
        if live:
            return "live", {"live_p_fair": p, "sigma": sigma, "p_inplay": p, "p_market": pm,
                            "p_llm": None, "score_key": (gs["home_score"], gs["away_score"]),
                            "llm_for_cache": None}, comp
        return "pregame", {"p_fair": p, "p_fair_lo": max(1e-4, p - sigma),
                           "p_fair_hi": min(0.99, p + sigma), "sigma": sigma,
                           "p_fair_llm": None, "p_fair_model": r.get("p_fair_model"),
                           "p_fair_data": pm, "reasoning": ""}, comp
    # adjacent
    if not matchup or not matchup[0]:
        return None, None, {}
    r = brain.evaluate_market(mkt, matchup[0], matchup[1], game_state=gs, market_total=mtotal)
    if not r:
        return None, None, {}
    p, pm = r["p_fair"], (r.get("p_market") if r.get("p_market") is not None else r["p_fair"])
    sigma = min(0.20, max(0.04, abs(p - pm) * 1.2 + 0.04))
    comp = {"w_model": r.get("p_fair_model"), "w_data": r.get("p_fair_data")}
    if live:
        return "live", {"live_p_fair": p, "sigma": sigma, "p_inplay": p, "p_market": pm,
                        "p_llm": None, "score_key": (gs["home_score"], gs["away_score"]),
                        "llm_for_cache": None}, comp
    return "pregame", {"p_fair": p, "p_fair_lo": max(0.01, p - sigma),
                       "p_fair_hi": min(0.99, p + sigma), "sigma": sigma,
                       "p_fair_llm": None, "p_fair_model": r.get("p_fair_model"),
                       "p_fair_data": pm, "reasoning": ""}, comp


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
        b = (c.get("yes_bid") or {}).get("close_dollars")
        a = (c.get("yes_ask") or {}).get("close_dollars")
        if b not in (None, "") and a not in (None, ""):
            return float(b), float(a)
    return None, None


def _pregame_quote_repr(client, series, ticker, ko):
    """Representative pre-game quote for THIN markets (e.g. KXWCSCORE). Same
    candlestick window as _pregame_quote, but skips empty-orderbook sentinels
    (≤1¢ bid / ≥99¢ ask) and returns the quote CLOSEST to kickoff — so a leg
    that was genuinely quoted is priced off its real settling book, not the
    1¢/99¢ placeholder. Returns (None, None) if the leg was never really quoted
    (correctly → no trade)."""
    path = f"/series/{series}/markets/{ticker}/candlesticks"
    try:
        r = client.session.get(client.base_url + path,
                               headers=client._auth_headers("GET", path),
                               params={"start_ts": ko - 86400, "end_ts": ko - 1800,
                                       "period_interval": 60}, timeout=20)
        cs = r.json().get("candlesticks", []) if r.ok else []
    except Exception:
        cs = []
    best = (None, None)
    for c in cs:                                   # ascending time → last good = closest to KO
        b = (c.get("yes_bid") or {}).get("close_dollars")
        a = (c.get("yes_ask") or {}).get("close_dollars")
        if b in (None, "") or a in (None, ""):
            continue
        b, a = float(b), float(a)
        if b <= 0.01 and a >= 0.99:                # empty-book placeholder
            continue
        best = (b, a)
    return best


def _live_book(client, ticker):
    try:
        b = client.get_best_bid_cents(ticker)
        a = client.get_best_ask_cents(ticker)
    except Exception:
        return None, None
    return (b / 100.0 if b else None, a / 100.0 if a else None)


# ── catch-up replay (resolved events) ────────────────────────────────────────

def _series_settled(client, series):
    """{suffix: [{ticker, sub, result}]} for one series' settled markets."""
    out = {}
    for st in ("settled", "finalized"):
        try:
            d = client._get("/markets", params={"series_ticker": series, "status": st,
                                                "limit": 1000}, auth=False)
        except Exception:
            continue
        for m in d.get("markets", []):
            suf = _suffix(m.get("event_ticker", ""))
            if not suf:
                continue
            r = (m.get("result") or "").lower()
            out.setdefault(suf, []).append({"ticker": m["ticker"], "sub": m.get("yes_sub_title", ""),
                                            "result": 1 if r == "yes" else (0 if r == "no" else None)})
    return out


def resolved_events(client, acfg):
    games = bt.discover_games(client, acfg["winner_series"])
    series_cache = {}
    for cat, cat_cfg in acfg["categories"].items():
        for s in cat_cfg["series"]:
            if s != acfg["winner_series"] and s not in series_cache:
                series_cache[s] = _series_settled(client, s)
    events = []
    for g in games:
        suf = _suffix(g["prefix"])
        cats = {}
        for cat, cat_cfg in acfg["categories"].items():
            legs = []
            for s in cat_cfg["series"]:
                if s == acfg["winner_series"]:
                    legs += [{"ticker": l["ticker"], "sub": l["sub"], "result": l["result"]}
                             for l in g["legs"]]
                else:
                    legs += series_cache.get(s, {}).get(suf, [])
            cats[cat] = {"series": cat_cfg["series"], "legs": legs}
        events.append({"prefix": g["prefix"], "suffix": suf, "home": g["home"],
                       "away": g["away"], "yyyymmdd": g["yyyymmdd"], "cats": cats})
    return events


def play_event(client, ev, acfg, brains, teams):
    """Replay one resolved event across all categories from T-24h → resolution."""
    espn_id, ko_iso = bt.find_espn("fifa.world", ev["yyyymmdd"], ev["home"], ev["away"])
    goals = bt.get_goals("fifa.world", espn_id) if espn_id else []
    ko = bt._epoch(ko_iso) if ko_iso else bt._epoch(
        f"{ev['yyyymmdd'][:4]}-{ev['yyyymmdd'][4:6]}-{ev['yyyymmdd'][6:]}T19:00:00Z")
    matchup = (ev["home"], ev["away"])
    deltas = {t["name"]: t["account"].realized_pnl for t in teams}
    gbets = {t["name"]: [] for t in teams}
    gskip = {t["name"]: [] for t in teams}

    # market-implied total for this event (from the totals series' T-24h quotes)
    mtotal = None
    tser = next((s for c in acfg["categories"].values() for s in c["series"]
                 if s.endswith("TOTAL") and not s.endswith("TEAMTOTAL")), None)
    if tser:
        ladder = []
        for cat in ev["cats"].values():
            if tser in cat["series"]:
                for leg in cat["legs"]:
                    b, a = _pregame_quote(client, tser, leg["ticker"], ko)
                    mid = (b + a) / 2 if (b is not None and a is not None) else (a or b)
                    if mid is not None:
                        ladder.append(mid)
        if len(ladder) >= 4:
            mtotal = sum(ladder)

    for cat, catinfo in ev["cats"].items():
        cat_cfg = acfg["categories"][cat]
        cat_teams = [t for t in teams if t["cat"] == cat]
        legs = catinfo["legs"]
        res = {l["ticker"]: l["result"] for l in legs}
        paths = {l["ticker"]: bt.get_price_path(client, catinfo["series"][0] if len(catinfo["series"]) == 1
                                                else _series_of(l["ticker"]), l["ticker"], ko) for l in legs}

        # pre-game (T-24h): price once per leg, all 4 strategies act
        for leg in legs:
            b, a = _pregame_quote(client, _series_of(leg["ticker"]), leg["ticker"], ko)
            if not a:
                continue
            mkt = _mkt(leg, ev["home"], ev["away"], b, a)
            phase, r, comp = price(cat_cfg, brains[cat], mkt, matchup, mtotal, None)
            if r is None:
                continue
            for t in cat_teams:
                tk = leg["ticker"]
                ctx = {"market": mkt, "pregame": r, "live": None, "ask": a, "bid": b,
                       "balance": t["account"].cash, "tau_days": 1.0,
                       "game_state": {"status": "pre", "minute": 0, "home_score": 0,
                                      "away_score": 0, "home_team": ev["home"], "away_team": ev["away"]}}
                d = t["strategy"].entry_dollars(ctx)
                if d > 0 and tk not in t["account"].positions:
                    n = max(1, int(d / a))
                    if t["account"].buy(tk, n, a, {"sub": leg["sub"]}):
                        gbets[t["name"]].append({"tk": tk, "p_fair": r["p_fair"], "comp": comp,
                                                 "entry": a, "contracts": n, "dollars": n * a,
                                                 "outcome": res.get(tk)})
                elif r.get("p_fair", 0) > a:
                    gskip[t["name"]].append({"tk": tk, "p_fair": r["p_fair"], "ask": a,
                                             "outcome": res.get(tk)})

        # in-play: C/D entries + all exits
        last = 95 if espn_id else 0
        for minute in range(1, last + 1, STEP):
            h, a_sc = bt.score_at(goals, minute)
            gs = {"status": "in", "minute": minute, "home_score": h, "away_score": a_sc,
                  "home_team": ev["home"], "away_team": ev["away"]}
            for leg in legs:
                bid, ask = bt._price_at(paths[leg["ticker"]], minute)
                if bid is None:
                    continue
                mkt = _mkt(leg, ev["home"], ev["away"], bid, ask)
                phase, r, comp = price(cat_cfg, brains[cat], mkt, matchup, mtotal, gs)
                if r is None:
                    continue
                for t in cat_teams:
                    tk = leg["ticker"]
                    ctx = {"market": mkt, "pregame": None, "live": r, "ask": ask, "bid": bid,
                           "balance": t["account"].cash, "tau_days": 0, "game_state": gs}
                    if tk not in t["account"].positions:
                        d = t["strategy"].entry_dollars(ctx)
                        if d > 0 and ask:
                            n = max(1, int(d / ask))
                            if t["account"].buy(tk, n, ask, {"sub": leg["sub"]}):
                                gbets[t["name"]].append({"tk": tk, "p_fair": r["live_p_fair"],
                                                         "comp": comp, "entry": ask, "contracts": n,
                                                         "dollars": n * ask, "outcome": res.get(tk)})
                    else:
                        action, frac = t["strategy"].exit_decision(t["account"].positions[tk], ctx)
                        if action != "HOLD" and frac > 0:
                            pos = t["account"].positions[tk]
                            want = max(1, min(int(round(pos["contracts"] * frac)), pos["contracts"]))
                            t["account"].sell(tk, want, bid)
                            if action == "OVERPRICED":
                                pos["_op_armed"] = False
                            elif action == "TAKE_PROFIT":
                                pos["_tp_done"] = True

        for t in cat_teams:
            for tk in list(t["account"].positions):
                if res.get(tk) is not None:
                    t["account"].settle(tk, res[tk])

    for t in teams:
        for bset in gbets[t["name"]]:
            o = bset["outcome"]
            bset["pnl"] = bset["contracts"] * ((o if o is not None else bset["entry"]) - bset["entry"])
        t["ledger"]["bets"].extend(gbets[t["name"]])
        t["ledger"]["skipped"].extend(gskip[t["name"]])
    return {t["name"]: {"delta": t["account"].realized_pnl - deltas[t["name"]],
                        "bets": gbets[t["name"]], "skipped": gskip[t["name"]]} for t in teams}


# series lookup by ticker prefix (e.g. KXWCSPREAD-... -> KXWCSPREAD)
def _series_of(ticker):
    return ticker.split("-", 1)[0]


def learn_and_tally(brains, teams, ev_prefix, gres, played, kind, acfg):
    line = [f"── {ev_prefix} {kind}"]
    # one PRICING pass per category from pooled cumulative bets
    for cat, brain in brains.items():
        pooled = [b for t in teams if t["cat"] == cat for b in t["ledger"]["bets"]]
        d = A3.price_update(brain, pooled)
        if d:
            line.append(f"   [{cat} BRAIN] " + " | ".join(d))
    # per-team SIZING
    for t in teams:
        r = A3.full_update(t["strategy"], t["brain"], t["ledger"],
                           gres[t["name"]]["bets"], gres[t["name"]]["skipped"], do_pricing=False)
        if gres[t["name"]]["delta"] or r["diag"]:
            tag = " | ".join(r["diag"]) if r["diag"] else ""
            line.append(f"   {t['name']:<18} Δ${gres[t['name']]['delta']:+.2f} "
                        f"cum ${t['account'].realized_pnl:+.2f} {tag}")
    print("\n".join(line), flush=True)
    played.add(ev_prefix)
    persist(brains, teams)
    _save(PLAYED, sorted(played))
    with open(TALLY, "a") as f:
        f.write(json.dumps({"game": ev_prefix, "standings": {
            t["name"]: round(t["account"].realized_pnl, 2) for t in teams}}) + "\n")


def print_tally(teams):
    print("\n=== ARENA TALLY (best per category → wallet candidate) ===", flush=True)
    cats = sorted(set(t["cat"] for t in teams))
    for cat in cats:
        ct = sorted([t for t in teams if t["cat"] == cat],
                    key=lambda x: x["account"].realized_pnl, reverse=True)
        best = ct[0]
        print(f"  [{cat}] leader {best['name'].split('_')[-1]} ${best['account'].realized_pnl:+.2f} | "
              + "  ".join(f"{t['name'].split('_')[-1]}:{t['account'].realized_pnl:+.1f}" for t in ct), flush=True)


# ── live (real-time) handling ────────────────────────────────────────────────

def _open_events(client, acfg):
    series_cat = {s: cat for cat, c in acfg["categories"].items() for s in c["series"]}
    ev = {}
    for s, cat in series_cat.items():
        for st in ("open", "active", "unopened"):
            try:
                d = client._get("/markets", params={"series_ticker": s, "status": st,
                                                    "limit": 400}, auth=False)
            except Exception:
                continue
            for m in d.get("markets", []):
                suf = _suffix(m.get("event_ticker", ""))
                if not suf:
                    continue
                e = ev.setdefault(suf, {"title": "", "kickoff": "", "cats": {}})
                if s == acfg["winner_series"] and not e["title"]:
                    e["title"] = m.get("title", "")
                if not e["kickoff"]:
                    e["kickoff"] = m.get("occurrence_datetime") or m.get("close_time", "") or ""
                cc = e["cats"].setdefault(cat, {"series": acfg["categories"][cat]["series"], "legs": []})
                if not any(l["ticker"] == m["ticker"] for l in cc["legs"]):
                    cc["legs"].append({"ticker": m["ticker"], "sub": m.get("yes_sub_title", "")})
    out = {}
    for suf, e in ev.items():
        mm = _VS_RE.search(e["title"])
        home, away = (mm.group(1).strip(), mm.group(2).strip()) if mm else (None, None)
        out[f"{acfg['winner_series']}-{suf}"] = {"home": home, "away": away,
                                                 "kickoff": e["kickoff"], "cats": e["cats"]}
    return out


def _live_mtotal(client, acfg, ev):
    tser = next((s for c in acfg["categories"].values() for s in c["series"]
                 if s.endswith("TOTAL") and not s.endswith("TEAMTOTAL")), None)
    if not tser:
        return None
    ladder = []
    for cat in ev["cats"].values():
        if tser in cat["series"]:
            for leg in cat["legs"]:
                b, a = _live_book(client, leg["ticker"])
                mid = (b + a) / 2 if (b is not None and a is not None) else (a or b)
                if mid is not None:
                    ladder.append(mid)
    return sum(ladder) if len(ladder) >= 4 else None


def live_step(client, acfg, brains, teams, prefix, ev, gs):
    try:
        from group import dominance
        stats = live_feed.get_game_stats(ev["home"], ev["away"])
        gs["_dom"] = (dominance.dominance_multipliers(stats["home"], stats["away"])
                      if stats else (1.0, 1.0))
    except Exception:
        gs["_dom"] = (1.0, 1.0)
    matchup = (ev["home"], ev["away"])
    mtotal = _live_mtotal(client, acfg, ev)
    acted = []
    for cat, catinfo in ev["cats"].items():
        cat_cfg = acfg["categories"][cat]
        cat_teams = [t for t in teams if t["cat"] == cat]
        for leg in catinfo["legs"]:
            bid, ask = _live_book(client, leg["ticker"])
            if bid is None and ask is None:
                continue
            mkt = _mkt(leg, ev["home"], ev["away"], bid, ask)
            _, r, comp = price(cat_cfg, brains[cat], mkt, matchup, mtotal, gs)
            if r is None:
                continue
            for t in cat_teams:
                tk = leg["ticker"]
                ctx = {"market": mkt, "pregame": None, "live": r, "ask": ask, "bid": bid,
                       "balance": t["account"].cash, "tau_days": 0, "game_state": gs}
                if tk not in t["account"].positions:
                    d = t["strategy"].entry_dollars(ctx)
                    if d > 0 and ask:
                        n = max(1, int(d / ask))
                        if t["account"].buy(tk, n, ask, {"sub": leg["sub"]}):
                            t["ledger"]["bets"].append({"tk": tk, "p_fair": r["live_p_fair"],
                                                        "comp": comp, "entry": ask, "contracts": n,
                                                        "dollars": n * ask, "outcome": None})
                            acted.append(f"{t['name']} BUY {leg['sub']} {n}@{ask:.2f}")
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


def pre_step(client, acfg, brains, teams, prefix, ev):
    matchup = (ev["home"], ev["away"])
    mtotal = _live_mtotal(client, acfg, ev)
    gs0 = {"status": "pre", "minute": 0, "home_score": 0, "away_score": 0,
           "home_team": ev["home"], "away_team": ev["away"]}
    acted = []
    for cat, catinfo in ev["cats"].items():
        cat_cfg = acfg["categories"][cat]
        cat_teams = [t for t in teams if t["cat"] == cat]
        for leg in catinfo["legs"]:
            b, a = _live_book(client, leg["ticker"])
            if not a:
                continue
            mkt = _mkt(leg, ev["home"], ev["away"], b, a)
            _, r, comp = price(cat_cfg, brains[cat], mkt, matchup, mtotal, None)
            if r is None:
                continue
            for t in cat_teams:
                tk = leg["ticker"]
                if tk in t["account"].positions:
                    continue
                ctx = {"market": mkt, "pregame": r, "live": None, "ask": a, "bid": b,
                       "balance": t["account"].cash, "tau_days": 1.0, "game_state": gs0}
                d = t["strategy"].entry_dollars(ctx)
                if d > 0:
                    n = max(1, int(d / a))
                    if t["account"].buy(tk, n, a, {"sub": leg["sub"]}):
                        t["ledger"]["bets"].append({"tk": tk, "p_fair": r["p_fair"], "comp": comp,
                                                    "entry": a, "contracts": n, "dollars": n * a,
                                                    "outcome": None})
                        acted.append(f"{t['name']} pre BUY {leg['sub']} {n}@{a:.2f}")
    return acted


def settle_and_learn(client, ev, teams):
    """A live-traded event resolved — finalize outcomes on its (live-placed) ledger
    bets + settle positions. No replay (already traded live)."""
    res = {}
    for catinfo in ev["cats"].values():
        for leg in catinfo["legs"]:
            res[leg["ticker"]] = leg.get("result")
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


# ── one-shot backfill of a NEW category (no global reset) ────────────────────

def _seed_category(base_cfg, tcfg, acfg, cat):
    """Create brain_<cat>.json + team file(s) for a freshly-added category WITHOUT
    touching any existing team/brain. Static seed params (no LLM) → deterministic
    and free. No-op for files that already exist."""
    os.makedirs(ARENA, exist_ok=True)
    cat_cfg = acfg["categories"][cat]
    bp = _brain_path(cat)
    if not os.path.exists(bp):
        default_w = ({"w_llm": 0.34, "w_model": 0.33, "w_data": 0.33} if cat_cfg.get("llm")
                     else {"w_llm": 0.0, "w_model": 0.5, "w_data": 0.5})
        _save(bp, {"brain_weights": default_w, "llm_addendum": "", "model_calibration": None})
    kp = _kind_params(tcfg)
    for spec in [s for s in _team_specs(acfg) if s["cat"] == cat]:
        tp = _team_path(spec["name"])
        if os.path.exists(tp):
            continue
        _save(tp, {"cat": cat, "kind": spec["kind"], "params": {**kp[spec["kind"]]},
                   "account": PaperAccount(spec["name"], acfg.get("starting_cash", 200.0)).to_dict(),
                   "ledger": {"bets": [], "skipped": []}})


def backfill_cat(base_cfg, tcfg, acfg, client, cat):
    """Replay every already-resolved game for ONE category's team(s) only, so a
    newly-added category gets the same from-the-start history as the rest of the
    board — without resetting or re-replaying any existing team. Per-game
    standings go to logs/arena_<cat>.jsonl (the shared arena.jsonl is untouched)."""
    if cat not in acfg["categories"]:
        print(f"[backfill] unknown category {cat!r}", flush=True)
        return
    _seed_category(base_cfg, tcfg, acfg, cat)
    cat_cfg = acfg["categories"][cat]
    brain = _build_brain(base_cfg, cat, cat_cfg, _load(_brain_path(cat), {}))
    teams = []
    for spec in [s for s in _team_specs(acfg) if s["cat"] == cat]:
        st = _load(_team_path(spec["name"]), None)
        teams.append({"name": spec["name"], "cat": cat, "kind": spec["kind"], "state": st,
                      "brain": brain, "strategy": build_strategy(spec["kind"], spec["name"], st["params"]),
                      "account": PaperAccount.from_dict(st["account"]), "ledger": st["ledger"]})

    log = os.path.join(BASE_DIR, "logs", f"arena_{cat}.jsonl")
    open(log, "w").close()
    events = resolved_events(client, acfg)
    print(f"=== BACKFILL [{cat}] — {len(teams)} team(s) over {len(events)} resolved games ===", flush=True)
    for ev in events:
        gres = _replay_cat_event(client, ev, acfg, cat, brain, teams)
        # learn: shared pricing pass from pooled cat bets + per-team sizing
        pooled = [b for t in teams for b in t["ledger"]["bets"]]
        A3.price_update(brain, pooled)
        for t in teams:
            A3.full_update(t["strategy"], t["brain"], t["ledger"],
                           gres[t["name"]]["bets"], gres[t["name"]]["skipped"], do_pricing=False)
            t["state"]["params"] = t["strategy"].p
        line = [f"── {ev['prefix']} (backfill)"]
        for t in teams:
            line.append(f"   {t['name']:<18} Δ${gres[t['name']]['delta']:+.2f} "
                        f"cum ${t['account'].realized_pnl:+.2f} "
                        f"(bets {len(gres[t['name']]['bets'])})")
        print("\n".join(line), flush=True)
        with open(log, "a") as f:
            f.write(json.dumps({"game": ev["prefix"], "standings": {
                t["name"]: round(t["account"].realized_pnl, 2) for t in teams}}) + "\n")
    # persist team + brain (brain shared-per-category file)
    _save(_brain_path(cat), {"brain_weights": brain.config.get("brain_weights"),
                             "llm_addendum": brain.config.get("llm_addendum", ""),
                             "model_calibration": brain.config.get("model_calibration")})
    for t in teams:
        t["state"]["account"] = t["account"].to_dict()
        t["state"]["ledger"] = t["ledger"]
        _save(_team_path(t["name"]), t["state"])
    print(f"=== BACKFILL [{cat}] done → {log} ===", flush=True)
    for t in teams:
        print(f"   {t['name']}: cum ${t['account'].realized_pnl:+.2f}", flush=True)


def _replay_cat_event(client, ev, acfg, cat, brain, teams):
    """Replay one resolved event for a SINGLE category's teams (T-24h → resolution).
    A scoped clone of play_event — mtotal still anchored from the event's totals
    legs so the Poisson grid is calibrated to this game's implied total."""
    espn_id, ko_iso = bt.find_espn("fifa.world", ev["yyyymmdd"], ev["home"], ev["away"])
    goals = bt.get_goals("fifa.world", espn_id) if espn_id else []
    ko = bt._epoch(ko_iso) if ko_iso else bt._epoch(
        f"{ev['yyyymmdd'][:4]}-{ev['yyyymmdd'][4:6]}-{ev['yyyymmdd'][6:]}T19:00:00Z")
    matchup = (ev["home"], ev["away"])
    cat_cfg = acfg["categories"][cat]
    deltas = {t["name"]: t["account"].realized_pnl for t in teams}
    gbets = {t["name"]: [] for t in teams}
    gskip = {t["name"]: [] for t in teams}

    # market-implied total (from the totals series' T-24h quotes across the event)
    mtotal = None
    tser = next((s for c in acfg["categories"].values() for s in c["series"]
                 if s.endswith("TOTAL") and not s.endswith("TEAMTOTAL")), None)
    if tser:
        ladder = []
        for catinfo in ev["cats"].values():
            if tser in catinfo["series"]:
                for leg in catinfo["legs"]:
                    b, a = _pregame_quote(client, tser, leg["ticker"], ko)
                    mid = (b + a) / 2 if (b is not None and a is not None) else (a or b)
                    if mid is not None:
                        ladder.append(mid)
        if len(ladder) >= 4:
            mtotal = sum(ladder)

    catinfo = ev["cats"][cat]
    legs = catinfo["legs"]
    res = {l["ticker"]: l["result"] for l in legs}
    paths = {l["ticker"]: bt.get_price_path(client, _series_of(l["ticker"]), l["ticker"], ko)
             for l in legs}

    # pre-game (T-24h) — thin scoreline legs use the representative quote
    # (closest-to-kickoff, skipping empty-book placeholders).
    quote = _pregame_quote_repr if cat_cfg.get("pricing") == "scoreline" else _pregame_quote
    for leg in legs:
        b, a = quote(client, _series_of(leg["ticker"]), leg["ticker"], ko)
        if not a:
            continue
        mkt = _mkt(leg, ev["home"], ev["away"], b, a)
        phase, r, comp = price(cat_cfg, brain, mkt, matchup, mtotal, None)
        if r is None:
            continue
        for t in teams:
            tk = leg["ticker"]
            ctx = {"market": mkt, "pregame": r, "live": None, "ask": a, "bid": b,
                   "balance": t["account"].cash, "tau_days": 1.0,
                   "game_state": {"status": "pre", "minute": 0, "home_score": 0,
                                  "away_score": 0, "home_team": ev["home"], "away_team": ev["away"]}}
            d = t["strategy"].entry_dollars(ctx)
            if d > 0 and tk not in t["account"].positions:
                n = max(1, int(d / a))
                if t["account"].buy(tk, n, a, {"sub": leg["sub"]}):
                    gbets[t["name"]].append({"tk": tk, "p_fair": r["p_fair"], "comp": comp,
                                             "entry": a, "contracts": n, "dollars": n * a,
                                             "outcome": res.get(tk)})
            elif r.get("p_fair", 0) > a:
                gskip[t["name"]].append({"tk": tk, "p_fair": r["p_fair"], "ask": a,
                                         "outcome": res.get(tk)})

    # in-play
    last = 95 if espn_id else 0
    for minute in range(1, last + 1, STEP):
        h, a_sc = bt.score_at(goals, minute)
        gs = {"status": "in", "minute": minute, "home_score": h, "away_score": a_sc,
              "home_team": ev["home"], "away_team": ev["away"]}
        for leg in legs:
            bid, ask = bt._price_at(paths[leg["ticker"]], minute)
            if bid is None:
                continue
            mkt = _mkt(leg, ev["home"], ev["away"], bid, ask)
            phase, r, comp = price(cat_cfg, brain, mkt, matchup, mtotal, gs)
            if r is None:
                continue
            for t in teams:
                tk = leg["ticker"]
                ctx = {"market": mkt, "pregame": None, "live": r, "ask": ask, "bid": bid,
                       "balance": t["account"].cash, "tau_days": 0, "game_state": gs}
                if tk not in t["account"].positions:
                    d = t["strategy"].entry_dollars(ctx)
                    if d > 0 and ask:
                        n = max(1, int(d / ask))
                        if t["account"].buy(tk, n, ask, {"sub": leg["sub"]}):
                            gbets[t["name"]].append({"tk": tk, "p_fair": r["live_p_fair"],
                                                     "comp": comp, "entry": ask, "contracts": n,
                                                     "dollars": n * ask, "outcome": res.get(tk)})
                else:
                    action, frac = t["strategy"].exit_decision(t["account"].positions[tk], ctx)
                    if action != "HOLD" and frac > 0:
                        pos = t["account"].positions[tk]
                        want = max(1, min(int(round(pos["contracts"] * frac)), pos["contracts"]))
                        t["account"].sell(tk, want, bid)
                        if action == "OVERPRICED":
                            pos["_op_armed"] = False
                        elif action == "TAKE_PROFIT":
                            pos["_tp_done"] = True

    for t in teams:
        for tk in list(t["account"].positions):
            if res.get(tk) is not None:
                t["account"].settle(tk, res[tk])

    for t in teams:
        for bset in gbets[t["name"]]:
            o = bset["outcome"]
            bset["pnl"] = bset["contracts"] * ((o if o is not None else bset["entry"]) - bset["entry"])
        t["ledger"]["bets"].extend(gbets[t["name"]])
        t["ledger"]["skipped"].extend(gskip[t["name"]])
    return {t["name"]: {"delta": t["account"].realized_pnl - deltas[t["name"]],
                        "bets": gbets[t["name"]], "skipped": gskip[t["name"]]} for t in teams}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--catchup-only", action="store_true", help="only replay resolved games")
    ap.add_argument("--backfill-cat", metavar="CAT",
                    help="one-shot: seed + replay resolved games for a NEW category only")
    args = ap.parse_args()

    load_keys()
    base_cfg = json.load(open(GCONFIG))
    tcfg = json.load(open(TCONFIG))
    acfg = json.load(open(ACONFIG))
    client = KalshiClient()

    if args.backfill_cat:
        backfill_cat(base_cfg, tcfg, acfg, client, args.backfill_cat)
        return

    if args.reset or not os.path.exists(PLAYED):
        reset_and_seed(base_cfg, tcfg, acfg)

    brains, teams = load_arena(base_cfg, acfg)
    played = set(_load(PLAYED, []))
    managed = set(_load(MANAGED, []))
    pregame_ts = _load(PREGAME_TS, {})

    # 1. resolved-but-unplayed → catch-up replay (or settle+learn if live-managed)
    events = resolved_events(client, acfg)
    for ev in events:
        if ev["prefix"] in played:
            continue
        if ev["prefix"] in managed:
            gres = settle_and_learn(client, ev, teams)
            learn_and_tally(brains, teams, ev["prefix"], gres, played, "(live-traded)", acfg)
        else:
            gres = play_event(client, ev, acfg, brains, teams)
            learn_and_tally(brains, teams, ev["prefix"], gres, played, "(replay)", acfg)

    # 2. live + upcoming (skipped in --catchup-only). Drive timing off ESPN's
    #    scoreboard (fetched ONCE), NOT Kalshi's occurrence_datetime — Kalshi's
    #    kickoff times are unreliable (often weeks off). ESPN's board only lists
    #    near-term games, so "pre" on it == inside the pre-game window.
    if not args.catchup_only:
        sb = live_feed.scoreboard_events("fifa.world")
        events_open = _open_events(client, acfg)
        for prefix, ev in events_open.items():
            if prefix in played or not ev["home"]:
                continue
            gs = live_feed.state_from_events(sb, ev["home"], ev["away"])
            if not gs:
                continue   # not on ESPN's near-term board → far out, skip
            if gs["status"] == "in":
                acted = live_step(client, acfg, brains, teams, prefix, ev, gs)
                managed.add(prefix)
                print(f"  [IN-PLAY] {ev['home']} {gs['home_score']}-{gs['away_score']} "
                      f"{ev['away']} {gs['minute']}': " + ("; ".join(acted) if acted else "no action"),
                      flush=True)
            elif gs["status"] == "pre":
                if time.time() - pregame_ts.get(prefix, 0) > PREGAME_EVERY:
                    acted = pre_step(client, acfg, brains, teams, prefix, ev)
                    pregame_ts[prefix] = time.time()
                    if acted:
                        print(f"  [PRE-GAME] {prefix}: " + "; ".join(acted), flush=True)

    persist(brains, teams)
    _save(PLAYED, sorted(played))
    _save(MANAGED, sorted(managed))
    _save(PREGAME_TS, pregame_ts)
    print_tally(teams)


if __name__ == "__main__":
    main()
