"""
arena_v2.py — Arena v2 paper tournament with an EVOLVING population.

Per active super-category (game_lines, game_props) it runs a population of teams.
Each team = a strategy-param point (strategy_v2) + a PaperAccount. A cycle:

  1. SETTLE   — fetch held tickers, read Kalshi `result`, pay out $1/$0, record
                closed bets, and feed the Brain Assistant (stacker re-tune).
  2. SCAN+BET — price every leg (scanner_v2 -> brain_v2), each team's Strategist
                decides size; place paper buys (pregame). Zero LLM, no real orders.
  3. EVOLVE   — fitness = realized_pnl / sqrt(staked); cull the worst eligible
                teams and breed replacements (crossover+mutate) from the winners.

State in arena_v2_state/ ; tally in logs/arena_v2.jsonl. Run `--once` per cron
tick (mirrors arena.py). NOTHING here ever places a real order.

  python3 arena_v2.py --reset      # seed populations
  python3 arena_v2.py --once       # one cycle
  python3 arena_v2.py --status     # leaderboard
"""
import datetime as dt
import json
import math
import os
from wc import paths
import random
import sys

from wc.kalshi.client_ext import KalshiClientV2
from wc.brain import BrainV2
from wc.lib.paper import PaperAccount
from wc.lib import kelly
import wc.core.strategy_base as sv
import wc.scanner as scn

BASE = os.path.dirname(__file__)
STATE = paths.STATE_V2
TALLY = os.path.join(paths.LOGS_DIR, "arena_v2.jsonl")
EVO_LOG = os.path.join(paths.LOGS_DIR, "evolution_v2.jsonl")
CATS_FILE = paths.CATS_FILE

POP_SIZE = 8                 # teams per category (4 seeds + 4 random at start)
MAX_OPEN = 12                # max open positions a team adds per category
MIN_CLOSED_FOR_CULL = 4      # a team needs this many resolved bets to be cullable
                             # (low so evolution can act from the early rounds)
CULL_FRACTION = 0.25
ROUND_GAMES = 4              # evolve every 4 resolved games per category
_SEED_OFFSET = 0             # bootstrap: vary RNG (team init + evolution) across runs
STARTING_CASH = 200.0
CLOSED_KEEP = 300            # cap stored closed-bet history per team


def kalshi_maintenance():
    """True during Kalshi's daily maintenance window (3:00–5:00am ET) — the API is
    down then, so cron cycles should skip rather than spin on retries."""
    try:
        from zoneinfo import ZoneInfo
        h = dt.datetime.now(ZoneInfo("America/New_York")).hour
        return 3 <= h < 5
    except Exception:
        return False


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def active_cats():
    cfg = json.load(open(CATS_FILE))
    return {k: v for k, v in cfg["super_categories"].items() if v.get("active")}


# ── team helpers ──────────────────────────────────────────────────────────────

def _new_team(cat, params, lineage, cycle, tid, generation=0):
    return {"id": tid, "cat": cat, "lineage": lineage, "params": sv.clip(params),
            "account": PaperAccount(f"{cat}:{tid}", STARTING_CASH).to_dict(),
            "bets": {}, "closed": [], "staked": 0.0, "n_closed": 0,
            "fitness": 0.0, "born": cycle, "generation": generation}


def _seed_population(cat, cycle, next_id):
    rng = random.Random(1000 + cycle + next_id[0] + _SEED_OFFSET)
    teams = []
    for name, p in sv.seed_params().items():
        teams.append(_new_team(cat, p, name, cycle, next_id[0])); next_id[0] += 1
    while len(teams) < POP_SIZE:
        teams.append(_new_team(cat, sv.random_params(rng), "random", cycle, next_id[0]))
        next_id[0] += 1
    return teams


def _fitness(team):
    acc = team["account"]
    return acc["realized_pnl"] / math.sqrt(team["staked"] + 1.0)


# ── settlement ────────────────────────────────────────────────────────────────

def _settle(client, teams_by_cat, brains, cycle):
    open_tickers = set()
    for teams in teams_by_cat.values():
        for t in teams:
            open_tickers |= set(t["bets"].keys())
    if not open_tickers:
        return {}
    fetched = client.list_markets_by_tickers(list(open_tickers))
    results = {}
    for m in fetched:
        r = (m.get("result") or "").lower()
        if m.get("status") in ("settled", "finalized") and r in ("yes", "no"):
            results[m["ticker"]] = 1 if r == "yes" else 0

    settled_count = 0
    events_by_cat = {}                  # cat -> # distinct games resolved this cycle
    for cat, teams in teams_by_cat.items():
        resolved_for_brain = {}        # ticker -> {sources, outcome} (unique per cat)
        cat_events = set()
        for t in teams:
            acc = PaperAccount.from_dict(t["account"])
            for tk in list(t["bets"]):
                if tk in results:
                    bet = t["bets"].pop(tk)
                    cat_events.add(bet.get("event"))
                    pnl = acc.settle(tk, results[tk])
                    t["closed"].append({"ticker": tk, "outcome": results[tk],
                                        "pnl": round(pnl, 3), "edge": bet.get("edge"),
                                        "staked": bet.get("cost")})
                    t["closed"] = t["closed"][-CLOSED_KEEP:]
                    t["n_closed"] += 1
                    settled_count += 1
                    resolved_for_brain[tk] = {"sources": bet.get("sources", {}),
                                              "outcome": results[tk]}
            t["account"] = acc.to_dict()
            t["fitness"] = _fitness(t)
        # Brain Assistant: re-tune the stacker on this cat's freshly resolved legs
        if resolved_for_brain:
            brains[cat].update_stacker(list(resolved_for_brain.values()))
        events_by_cat[cat] = len(cat_events)
    return {"settled": settled_count, "events": events_by_cat}


# ── betting ───────────────────────────────────────────────────────────────────

def _enter(teams_by_cat, games_by_cat):
    """Each team's Strategist sizes every priced leg (pregame or in-play) and
    places paper buys. in_play/minute flow through so scalp/momentum teams act."""
    placed = 0
    for cat, teams in teams_by_cat.items():
        for game in games_by_cat[cat]:
            for leg in game["legs"]:
                tk, ask = leg["ticker"], leg["ask"]
                if not ask:
                    continue
                edge = leg["p_fair"] - ask / 100.0
                ctx = {"p_fair": leg["p_fair"], "ask": ask,
                       "in_play": leg.get("in_play", False), "minute": leg.get("minute")}
                for t in teams:
                    if tk in t["bets"] or len(t["bets"]) >= MAX_OPEN:
                        continue
                    acc = PaperAccount.from_dict(t["account"])
                    bet = sv.Strategist(t["params"]).entry_size(ctx, acc.cash)
                    if bet <= 0:
                        continue
                    n = kelly.to_contracts(bet, ask / 100.0)
                    if not acc.buy(tk, n, ask / 100.0,
                                   {"sub": leg["sub"], "event": game["event"]}):
                        continue
                    cost = n * ask / 100.0
                    t["staked"] += cost
                    t["bets"][tk] = {"sources": leg["sources"], "p_fair": leg["p_fair"],
                                     "ask": ask, "edge": round(edge, 4),
                                     "contracts": n, "cost": round(cost, 3),
                                     "event": game["event"], "flags": {}}
                    t["account"] = acc.to_dict()
                    placed += 1
    return {"placed": placed}


def _exits(teams_by_cat, games_by_cat):
    """Run the shared re-armed exit logic on open positions in LIVE games, using
    each leg's live p_fair + current bid. Sells on the paper account; a fully
    closed position is recorded as a resolved (exited) bet for fitness."""
    exited = 0
    for cat, teams in teams_by_cat.items():
        live_legs = {leg["ticker"]: leg for g in games_by_cat[cat]
                     for leg in g["legs"] if leg.get("in_play")}
        if not live_legs:
            continue
        for t in teams:
            acc = PaperAccount.from_dict(t["account"])
            changed = False
            for tk, leg in live_legs.items():
                if tk not in t["bets"]:
                    continue
                pos = acc.positions.get(tk)
                if not pos:
                    continue
                flags = t["bets"][tk].get("flags", {})
                carrier = {"entry": pos["entry"], **flags}
                action, frac = sv.Strategist(t["params"]).exit_decision(
                    carrier, leg["p_fair"], leg["bid"])
                if action != "HOLD" and leg.get("bid"):
                    n = max(1, int(pos["contracts"] * frac))
                    if acc.sell(tk, n, leg["bid"] / 100.0):
                        sv.disarm(action, carrier)
                        changed = True
                        exited += 1
                if tk in t["bets"]:
                    if tk not in acc.positions:        # fully closed by the exit
                        t["bets"].pop(tk, None)
                        t["closed"].append({"ticker": tk, "outcome": "exit", "pnl": None})
                        t["closed"] = t["closed"][-CLOSED_KEEP:]
                        t["n_closed"] += 1
                    else:                              # persist re-arm flag state
                        t["bets"][tk]["flags"] = {k: v for k, v in carrier.items()
                                                  if k != "entry"}
            if changed:
                t["account"] = acc.to_dict()
                t["fitness"] = _fitness(t)
    return {"exited": exited}


# ── evolution ─────────────────────────────────────────────────────────────────

def _evolve_cat(teams_by_cat, cat, cycle, next_id, gen, log_rows):
    """PBT-style evolution of ONE category, run once per round (ROUND_GAMES):
      • CULL the worst eligible teams (logged as eliminated).
      • EXPLORE: every surviving team except the current elite perturbs its own
        params in place (keeps its wallet) — survivors keep drifting, not frozen.
      • EXPLOIT: refill the culled slots with children = crossover of two top-half
        parents + mutate; each child is tagged with this generation's name.
    Appends an audit row (eliminated / born / perturbed) to log_rows."""
    rng = random.Random(7000 + cycle + (abs(hash(cat)) % 1000) + _SEED_OFFSET)
    teams = teams_by_cat[cat]
    eligible = [t for t in teams if t["n_closed"] >= MIN_CLOSED_FOR_CULL]
    if len(eligible) < 3:                  # need a parent pool + at least one cull
        return "round reached but <3 eligible teams"
    eligible.sort(key=lambda t: t["fitness"], reverse=True)
    n_cull = max(1, int(len(eligible) * CULL_FRACTION))
    pool = eligible[:max(2, len(eligible) // 2)]
    losers = eligible[-n_cull:]
    loser_ids = {t["id"] for t in losers}
    elite_id = eligible[0]["id"]
    eliminated = [{"lineage": t["lineage"], "fitness": round(t["fitness"], 3),
                   "pnl": round(t["account"]["realized_pnl"], 2),
                   "staked": round(t["staked"], 2), "n_closed": t["n_closed"]}
                  for t in losers]

    survivors = [t for t in teams if t["id"] not in loser_ids]
    perturbed = []
    for t in survivors:                       # EXPLORE: survivors (not elite) drift
        if t["id"] != elite_id and t["n_closed"] >= MIN_CLOSED_FOR_CULL:
            t["params"] = sv.perturb(t["params"], rng)
            perturbed.append(t["lineage"])

    born = []
    for _ in range(n_cull):                    # EXPLOIT: breed gen-tagged children
        pa, pb = rng.sample(pool, 2)
        params = sv.mutate(sv.crossover(pa["params"], pb["params"], rng), rng)
        lineage = f"g{gen}<{pa['id']}x{pb['id']}>"
        survivors.append(_new_team(cat, params, lineage, cycle, next_id[0], generation=gen))
        next_id[0] += 1
        born.append(lineage)

    teams_by_cat[cat] = survivors
    log_rows.append({"cycle": cycle, "cat": cat, "generation": gen,
                     "eliminated": eliminated, "born": born, "perturbed": perturbed,
                     "elite": eligible[0]["lineage"],
                     "elite_pnl": round(eligible[0]["account"]["realized_pnl"], 2)})
    return (f"gen{gen}: culled {[e['lineage'] for e in eliminated]}, "
            f"bred {born}, perturbed {len(perturbed)} survivors")


# ── cycle ─────────────────────────────────────────────────────────────────────

def cycle_once():
    if kalshi_maintenance():
        print("kalshi maintenance window (3-5am ET) — skipping cycle")
        return
    meta = _load(os.path.join(STATE, "meta.json"), {"cycle": 0, "next_id": 0})
    cats = active_cats()
    next_id = [meta["next_id"]]
    cycle = meta["cycle"] + 1

    brains, teams_by_cat = {}, {}
    for cat, cfg in cats.items():
        bw = _load(os.path.join(STATE, f"brain_{cat}.json"), None)
        lc = _load(os.path.join(STATE, f"llm_{cat}.json"), {})
        bconf = {"use_llm": cfg.get("use_llm", False), "llm_cache": lc}
        if bw:
            bconf["stacker_weights"] = bw
        brains[cat] = BrainV2(bconf)
        teams = _load(os.path.join(STATE, f"teams_{cat}.json"), None)
        teams_by_cat[cat] = teams if teams else _seed_population(cat, cycle, next_id)

    client = KalshiClientV2(req_per_sec=6)
    s = _settle(client, teams_by_cat, brains, cycle)
    games_by_cat = {cat: scn.price_games(cat, client, brains[cat], max_events=4, live=True)
                    for cat in cats}
    b = _enter(teams_by_cat, games_by_cat)
    x = _exits(teams_by_cat, games_by_cat)

    # round-based evolution: accumulate resolved games; evolve a category only
    # once a full round (~matchday) has resolved, then reset its counter.
    rse = meta.get("resolved_since_evolve", {})
    gens = meta.get("generation", {})
    evo_rows, e = [], {}
    for cat in cats:
        rse[cat] = rse.get(cat, 0) + s.get("events", {}).get(cat, 0)
        if rse[cat] >= ROUND_GAMES:
            gens[cat] = gens.get(cat, 0) + 1
            e[cat] = _evolve_cat(teams_by_cat, cat, cycle, next_id, gens[cat], evo_rows)
            rse[cat] = 0
        else:
            e[cat] = f"{rse[cat]}/{ROUND_GAMES} games to next evolve"
    meta["resolved_since_evolve"] = rse
    meta["generation"] = gens
    if evo_rows:
        os.makedirs(os.path.dirname(EVO_LOG), exist_ok=True)
        with open(EVO_LOG, "a") as f:
            for r in evo_rows:
                f.write(json.dumps(r) + "\n")

    # persist
    for cat in cats:
        _save(os.path.join(STATE, f"teams_{cat}.json"), teams_by_cat[cat])
        _save(os.path.join(STATE, f"brain_{cat}.json"), brains[cat].weights)
        _save(os.path.join(STATE, f"llm_{cat}.json"), brains[cat].llm_cache)
    meta.update({"cycle": cycle, "next_id": next_id[0]})
    _save(os.path.join(STATE, "meta.json"), meta)

    # tally: best team per category
    tally = {"cycle": cycle, "settled": s.get("settled", 0), "placed": b.get("placed", 0),
             "exited": x.get("exited", 0)}
    for cat, teams in teams_by_cat.items():
        best = max(teams, key=lambda t: t["fitness"])
        tally[cat] = {"best": best["lineage"], "fitness": round(best["fitness"], 3),
                      "pnl": round(best["account"]["realized_pnl"], 2),
                      "n_closed": best["n_closed"], "evolve": e.get(cat)}
    os.makedirs(os.path.dirname(TALLY), exist_ok=True)
    with open(TALLY, "a") as f:
        f.write(json.dumps(tally) + "\n")
    print(json.dumps(tally, indent=2))
    return tally


# ── catch-up replay over resolved games ──────────────────────────────────────

_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _event_date(ec):
    """Sort key from an event code like '26JUN17PORCOD' -> (2026, 6, 17)."""
    try:
        return (2000 + int(ec[:2]), _MON.get(ec[2:5].upper(), 0), int(ec[5:7]))
    except (ValueError, IndexError):
        return (0, 0, 0)


def _settled_by_event(client, series):
    """{event_code: [{ticker, sub, result, close_ts}]} for a series' settled markets."""
    out = {}
    for status in ("settled",):
        for m in client.list_markets_by_series(series, status=status):
            tk = m["ticker"]
            r = (m.get("result") or "").lower()
            if r not in ("yes", "no"):
                continue
            ec = tk.split("-")[1] if "-" in tk else None
            ct = m.get("close_time")
            try:
                ko = int(dt.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()) - 7200
            except Exception:
                ko = None
            out.setdefault(ec, []).append(
                {"ticker": tk, "series": series, "sub": m.get("yes_sub_title", ""),
                 "title": m.get("title", ""), "result": 1 if r == "yes" else 0, "ko": ko})
    return out


def _candle_quote(client, series, ticker, ko, cache):
    """(bid, ask) dollars closest to kickoff from candlesticks; skips empty-book
    1c/99c placeholders. Cached per ticker."""
    if ticker in cache:
        return cache[ticker]
    if client is None:                 # re-sim: cache-only, never fetch
        return (None, None)
    res = (None, None)
    if ko:
        path = f"/series/{series}/markets/{ticker}/candlesticks"
        try:
            r = client.session.get(client.base_url + path,
                                   headers=client._auth_headers("GET", path),
                                   params={"start_ts": ko - 86400, "end_ts": ko,
                                           "period_interval": 60}, timeout=20)
            cs = r.json().get("candlesticks", []) if r.ok else []
        except Exception:
            cs = []
        for c in cs:                       # ascending → last good = closest to KO
            b = (c.get("yes_bid") or {}).get("close_dollars")
            a = (c.get("yes_ask") or {}).get("close_dollars")
            if b in (None, "") or a in (None, ""):
                continue
            b, a = float(b), float(a)
            if b <= 0.01 and a >= 0.99:
                continue
            res = (b, a)
    cache[ticker] = res
    return res


def replay(max_games=None, seed=0, persist=True, data=None):
    """Replay every resolved WC game chronologically: price each leg off its
    pre-game candlestick quote, let teams bet, settle on the real result, feed
    the Brain Assistant, and evolve — to SEED the population with history.

    seed     — RNG offset for team init + evolution (bootstrap variance).
    persist  — write state to disk (False for in-memory bootstrap sims).
    data     — reuse a prior run's {names, settled, qcache} (skip all fetching).
    Returns {"teams": teams_by_cat, "data": {...}} for reuse across sims."""
    global _SEED_OFFSET
    _SEED_OFFSET = seed
    if persist:
        reset()
    cats = active_cats()
    next_id = [0]
    brains = {c: BrainV2() for c in cats}
    teams_by_cat = {c: _seed_population(c, 0, next_id) for c in cats}

    if data is None:
        client = KalshiClientV2(req_per_sec=4)
        qcache = {}
        names = {}
        for m in client.list_markets_by_series("KXWCGAME", status="settled"):
            ec = m["ticker"].split("-")[1]
            h, a = scn.home_away(m.get("title"))
            if h:
                names[ec] = (h, a)
        need = set(scn.CONTEXT_SERIES)
        for c in cats:
            need |= set(json.load(open(CATS_FILE))["super_categories"][c]["series"])
        settled = {s: _settled_by_event(client, s) for s in need}
    else:
        client = None                        # cache-only, no fetching
        names, settled, qcache = data["names"], data["settled"], data["qcache"]

    games = sorted([ec for ec in names], key=_event_date)
    if max_games:
        games = games[:max_games]
    if persist:
        print(f"replay: {len(games)} resolved games, {len(cats)} categories")

    cycle = 0
    gens, evo_rows = {}, []
    for ec in games:
        cycle += 1
        home, away = names[ec]
        # market anchors from this game's settled total/corner legs (candlestick mids)
        def _anchor(series, fn):
            pts = []
            for leg in settled.get(series, {}).get(ec, []):
                b, a = _candle_quote(client, series, leg["ticker"], leg["ko"], qcache)
                if b is not None and a is not None:
                    pts.append((leg, (b + a) / 2))
            return fn(pts)
        mt = _anchor("KXWCTOTAL", lambda pts: sum(p for _, p in pts) or None)
        cmean = _anchor("KXWCCORNERS", lambda pts: ((min(int(scn.mv.parse_market_v2(l["ticker"], l["sub"])["threshold"] or 99) for l, _ in pts) - 1)
                        + sum(p for _, p in pts)) if len(pts) >= 3 else None)

        for cat, teams in teams_by_cat.items():
            cseries = json.load(open(CATS_FILE))["super_categories"][cat]["series"]
            prior = brains[cat].game_prior(home, away, market_total=mt, market_corner_total=cmean)
            resolved_for_brain = {}
            for s in cseries:
                for leg in settled.get(s, {}).get(ec, []):
                    parsed = scn.mv.parse_market_v2(leg["ticker"], leg["sub"])
                    if parsed["type"] in ("unknown", "first_goalscorer"):
                        continue
                    b, a = _candle_quote(client, s, leg["ticker"], leg["ko"], qcache)
                    if a is None:
                        continue
                    lh = scn.leg_is_home(parsed, home, away)
                    mkt = (b + a) / 2 if (b is not None) else None
                    pf, sources = brains[cat].pfair(parsed, prior, lh, market_mid=mkt)
                    if pf is None:
                        continue
                    ask_c = int(round(a * 100))
                    edge = pf - a
                    for t in teams:
                        acc = PaperAccount.from_dict(t["account"])
                        bet = sv.Strategist(t["params"]).entry_size(
                            {"p_fair": pf, "ask": ask_c, "in_play": False, "minute": None}, acc.cash)
                        if bet <= 0:
                            continue
                        n = kelly.to_contracts(bet, a)
                        if not acc.buy(leg["ticker"], n, a, {"sub": leg["sub"], "event": ec}):
                            continue
                        cost = n * a
                        pnl = acc.settle(leg["ticker"], leg["result"])   # resolve immediately
                        t["staked"] += cost
                        t["closed"].append({"ticker": leg["ticker"], "outcome": leg["result"],
                                            "pnl": round(pnl, 3), "edge": round(edge, 4),
                                            "staked": round(cost, 3)})
                        t["closed"] = t["closed"][-CLOSED_KEEP:]
                        t["n_closed"] += 1
                        t["account"] = acc.to_dict()
                        t["fitness"] = _fitness(t)
                    resolved_for_brain[leg["ticker"]] = {"sources": sources, "outcome": leg["result"]}
            if resolved_for_brain:
                brains[cat].update_stacker(list(resolved_for_brain.values()))
        if cycle % ROUND_GAMES == 0:                  # evolve every ROUND_GAMES games
            for cat in cats:
                gens[cat] = gens.get(cat, 0) + 1
                _evolve_cat(teams_by_cat, cat, cycle, next_id, gens[cat], evo_rows)
        if persist and cycle % 5 == 0:
            print(f"  replayed {cycle}/{len(games)} ({ec})")

    if persist:
        for cat in cats:
            _save(os.path.join(STATE, f"teams_{cat}.json"), teams_by_cat[cat])
            _save(os.path.join(STATE, f"brain_{cat}.json"), brains[cat].weights)
            _save(os.path.join(STATE, f"llm_{cat}.json"), brains[cat].llm_cache)
        # leave forward evolution one game away so it continues promptly post-replay
        _save(os.path.join(STATE, "meta.json"),
              {"cycle": cycle, "next_id": next_id[0], "generation": gens,
               "resolved_since_evolve": {c: ROUND_GAMES - 1 for c in cats}})
        if evo_rows:
            os.makedirs(os.path.dirname(EVO_LOG), exist_ok=True)
            with open(EVO_LOG, "w") as f:             # fresh evolution log for the replay
                for r in evo_rows:
                    f.write(json.dumps(r) + "\n")
        print(f"replay done: seeded {len(games)} games, "
              f"{sum(gens.values())} evolution generations across {len(cats)} categories.")
        status()
    return {"teams": teams_by_cat,
            "data": {"names": names, "settled": settled, "qcache": qcache}}


def bootstrap(n=5, max_games=None):
    """Re-run the replay N times with different RNG seeds (team init + evolution)
    on the SAME fetched data, to measure how much the leaderboard is luck-of-the-
    draw vs a stable signal. In-memory only — does NOT touch the live state."""
    import statistics as stats
    print(f"Bootstrapping {n} replay runs (fetch once, vary RNG seed)...")
    first = replay(max_games=max_games, seed=0, persist=False, data=None)
    runs = [first["teams"]]
    for s in range(1, n):
        runs.append(replay(max_games=max_games, seed=s, persist=False,
                           data=first["data"])["teams"])
    cats = active_cats()
    print(f"\n=== BOOTSTRAP VARIANCE over {n} runs ===")
    for cat in cats:
        best_pnls, best_fits, winners = [], [], []
        for tbc in runs:
            teams = tbc[cat]
            b = max(teams, key=lambda t: t["fitness"])
            best_pnls.append(b["account"]["realized_pnl"])
            best_fits.append(b["fitness"])
            winners.append(b["lineage"])
        sd = stats.pstdev(best_pnls) if len(best_pnls) > 1 else 0.0
        print(f"\n{cat}: best-team P&L across runs = "
              f"${min(best_pnls):+.0f}..${max(best_pnls):+.0f} "
              f"(mean ${stats.mean(best_pnls):+.0f}, sd ${sd:.0f})")
        print(f"  best fitness: {min(best_fits):+.2f}..{max(best_fits):+.2f}")
        print(f"  winning lineage per run: {winners}")


def reset():
    import shutil
    if os.path.isdir(STATE):
        shutil.rmtree(STATE)
    os.makedirs(STATE, exist_ok=True)
    print("arena_v2 state cleared; next --once will seed populations.")


def _per_market_pnl(teams):
    """Aggregate realized P&L by market series across all teams' resolved bets.
    Returns {series: [pnl, n_settled, n_win]}. Exit-closed bets (pnl=None) skipped."""
    mk = {}
    for t in teams:
        for cb in t["closed"]:
            if cb.get("pnl") is None:
                continue
            s = cb["ticker"].split("-")[0]
            d = mk.setdefault(s, [0.0, 0, 0])
            d[0] += cb["pnl"]
            d[1] += 1
            if cb.get("outcome") == 1:
                d[2] += 1
    return mk


def status():
    cats = active_cats()
    for cat in cats:
        teams = _load(os.path.join(STATE, f"teams_{cat}.json"), [])
        teams.sort(key=lambda t: t["fitness"], reverse=True)
        print(f"\n=== {cat} ===  ({len(teams)} teams)")
        for t in teams:
            acc = t["account"]
            print(f"  {t['lineage']:<22} fit={t['fitness']:+.3f} "
                  f"pnl=${acc['realized_pnl']:+.2f} closed={t['n_closed']} "
                  f"open={len(t['bets'])} kelly={t['params'].get('kelly_fraction')} "
                  f"min_edge={t['params'].get('min_edge')}")
        mk = _per_market_pnl(teams)
        if mk:
            print("  -- per-market P&L (all teams, resolved bets) --")
            for s, (p, n, w) in sorted(mk.items(), key=lambda x: -x[1][0]):
                print(f"     {s:<15} ${p:+8.2f}  ({n} bets, {w} won, "
                      f"{100*w/n:.0f}% win)")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--once"
    if arg == "--reset":
        reset()
    elif arg == "--status":
        status()
    elif arg == "--replay":
        mg = int(sys.argv[2]) if len(sys.argv) > 2 else None
        replay(max_games=mg)
    elif arg == "--bootstrap":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        bootstrap(n=n)
    else:
        cycle_once()
