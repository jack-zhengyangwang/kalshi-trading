"""
arena_v3.py — Arena v3 trade-tape replay + evolving population.

The redesign that follows the "stop betting Haiti at 6c" post-mortem:

  • TEAMS  — strategy_v3 strategists (price band + hard unit cap + favorite-tilt
             allocation + market_focus specialists). ~13 per category, all start
             at $100. Seeds = the archetypes (favorite / totals / value / flow /
             classics) + random fill.
  • BRAIN  — brain_v2 (data + market + optional LLM) PLUS a WC base-rate totals
             prior baked in pre-game (Over-1.5 ~85%, the proven WC totals edge).
  • FILLS  — REAL trade tape, not candlesticks. An order for N contracts at minute
             t fills only up to the volume the tape actually shows near t; the rest
             FAILS. This is the realism the candlestick sim lacked.
  • TIME   — each game is stepped pre-game -> 0/15/30/45/60/75/90 in order. No
             jump-ahead: a leg is priced at the quote that existed at that minute.
  • EVOLVE — every EVOLVE_EVERY games, per category: breed the top pairs (1x2,
             3x4), and PUNISH the bottom 4 (params nudged toward the winners +
             a fund haircut) — nobody is eliminated.

game_props (corners / correct-score) has no historical minute data, so the replay
covers winner + game_lines; the game_props population is seeded for forward-live.

    python3 arena_v3.py [max_games] [--llm]
"""
import datetime as _dt
import json
import math
import os
from wc import paths
import random
import sys

import wc.core.arena_base as A
import wc.match_sim as MS
import wc.markets as mv
import wc.prediction_db as pdb
import wc.strategy as s3
import wc.tradetape as tt
from wc.lib.paper import PaperAccount
from wc.lib import kelly

# ONE unified pool — the super-category BARRIER is removed. A single population of
# agents (each with its own paper wallet, so they can't interfere) scans the WHOLE
# KXWC surface; per-agent market_focus still gives specialization + diversity. The
# old per-cat silos (winner/game_lines/game_props/discovered) collapse into "all".
REPLAY_CATS = ["all"]
SEED_CATS = ["all"]
LLM_CATS = {"all"}                          # the unified pool prices novel types via LLM


def _tau_days(close_time):
    """Days until a market resolves, from its Kalshi close_time (0 if unknown)."""
    if not close_time:
        return 0.0
    try:
        ct = _dt.datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
        return max(0.0, (ct - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 86400.0)
    except Exception:
        return 0.0
POP_PER_CAT = 13
START_CASH = 100.0
EVOLVE_EVERY = 12                           # games between evolution events (72 group
                                            #   games / 12 -> 6 generations)
PUNISH_N = 4                               # bottom teams nudged + haircut each event
HAIRCUT = 0.85                             # bottom teams keep 85% of their cash
MAX_OPEN = 12
SLIP = 0.02                                # half-spread (2c): pay up on entry, give up
                                           #   on exit. WC books are thin → 1c was too
                                           #   generous both ways (replay realism guard).
PREGAME_WIN = 30                           # fill window (min) for pre-game orders (was
                                           #   120 = 2h cumulative, far too generous)
INPLAY_WIN = 15                            # fill window (min) for in-play orders

# In-play near-certainty guard: once a leg is already near-resolved, the live
# "edge" is an artifact (the brain prices a decided Over at ~0.999 while the tape
# still shows closing trades near the price) and you couldn't really get filled at
# size. Block in-play entries above this price / fair. Pre-game is unaffected.
NC_HI = {"winner": 0.96, "game_lines": 0.96}   # raised: only skip legs that are
NC_DEFAULT = 0.96                               # essentially RESOLVED, so scalpers can
                                               # still operate in their 0.85-0.95 range

# Bet FULL-MATCH legs only. 1H/2H period markets lose consistently (in-sample AND
# forward) — our model is tuned to full-match dynamics and the period books are
# thin/noisy. Reversible: add "1H"/"2H" here once period pricing is validated.
BET_PERIODS = {"full"}

# type names must match what markets_v2.parse_market_v2 actually returns
CAT_TYPES = {
    "winner": {"winner"},
    "game_lines": {"spread", "total", "team_total", "btts"},
    "game_props": {"corners", "team_corners", "score", "first_to_score"},
    # unified pool: every leg type is allowed (market_focus still specializes per agent)
    "all": {"winner", "spread", "total", "team_total", "btts",
            "corners", "team_corners", "score", "first_to_score"},
}

# WC group-stage base rates for total goals (Over X.5 -> P). The YouTube research
# edge: markets chronically under-price low totals. Blended toward (logit) pre-game.
WC_OVER_BASE = {0.5: 0.97, 1.5: 0.85, 2.5: 0.58, 3.5: 0.33, 4.5: 0.16, 5.5: 0.08}
WC_BASE_WEIGHT = 0.25


# ── population ────────────────────────────────────────────────────────────────

def _new_team(cat, params, focus, lineage, cid, gen=0):
    return {"id": cid, "cat": cat, "lineage": lineage,
            "params": s3.clip(params), "market_focus": focus,
            "account": PaperAccount(f"{cat}:{cid}", START_CASH).to_dict(),
            "bets": {}, "closed": [], "staked": 0.0, "n_closed": 0,
            "fitness": 0.0, "generation": gen}


def seed_population(cat, next_id, rng):
    teams = []
    for name, (params, focus) in s3.seed_params().items():
        # drop a market_focus that this category can't satisfy (e.g. a totals
        # specialist in the winner category) so the slot isn't wasted.
        if focus and not (set(focus) & CAT_TYPES.get(cat, set())):
            focus = None
        teams.append(_new_team(cat, params, focus, name, next_id[0])); next_id[0] += 1
    while len(teams) < POP_PER_CAT:
        teams.append(_new_team(cat, s3.random_params(rng), None, "random", next_id[0]))
        next_id[0] += 1
    return teams


def _fitness(t):
    return t["account"]["realized_pnl"] / math.sqrt(t["staked"] + 1.0)


def _base_name(lineage):
    """Archetype root of a lineage, dropping a leading 'gN_' generation tag:
    'flow'->'flow', 'g3_flow'->'flow'. Lets children keep a readable gx_name."""
    s = lineage or "agent"
    if s[:1] == "g" and "_" in s:
        head, rest = s.split("_", 1)
        if head[1:].isdigit():
            return rest
    return s


def _window_open(period, minute, in_play):
    """Causal time guardrail: can this period's market still be ENTERED at `minute`?
    A 1st-half line is dead once the half is over (45'), a 2nd-half/full line once
    full time is (90'). Pre-game (not in_play) everything is open. This is what lets
    the replay bet 1H/2H markets honestly — 'as if watching' — instead of banning
    them outright."""
    if not in_play:
        return True
    m = minute or 0
    if period == "1H":
        return m < 45
    return m < 90              # 2H and full both close at full time


EVOLVE_MIN_N = 8        # resolved bets before a team is breedable (was 2 — too noisy)
SHRINK_K = 12.0         # sample-size shrinkage for the breeding fitness


def _evo_fitness(t):
    """Breeding fitness: ROI per $ staked, SHRUNK toward 0 by sample count, so a
    lucky 2-bet micro-staker can't top the board and get bred (audit: fitness was
    selecting luck). Combined pre+in-play (keeps legit in-play edges like corners);
    PROMOTION is separately gated on forward pre-game only."""
    n = t["n_closed"]
    if n == 0:
        return 0.0
    roi = t["account"]["realized_pnl"] / (t["staked"] + 1.0)
    return roi * n / (n + SHRINK_K)


# ── WC base-rate totals prior ──────────────────────────────────────────────────

def _wc_total_pf(parsed, leg_sub, pf):
    """Blend a pre-game total leg's p_fair toward the WC base rate (logit space).
    Returns the (possibly) adjusted p_fair. No-op for non-total / unparseable."""
    # ONLY full-game totals — the WC_OVER_BASE rates are full-match; applying them
    # to 1H/2H totals massively over-priced first-half overs (the −$25 forward bleed).
    if parsed.get("type") != "total" or parsed.get("period") != "full" or pf is None:
        return pf
    thr = parsed.get("threshold")
    if thr is None:                                   # parse_market_v2 leaves it None;
        import re                                      # pull "X.5" straight from the sub
        m = re.search(r"(\d+(?:\.\d+)?)", leg_sub or "")
        thr = float(m.group(1)) if m else None
    if thr is None:
        return pf
    base_over = WC_OVER_BASE.get(round(float(thr) * 2) / 2)
    if base_over is None:
        return pf
    is_over = "over" in (leg_sub or "").lower()
    base = base_over if is_over else (1 - base_over)
    base = min(0.98, max(0.02, base))
    p = min(0.98, max(0.02, pf))
    lg = (1 - WC_BASE_WEIGHT) * math.log(p / (1 - p)) + \
        WC_BASE_WEIGHT * math.log(base / (1 - base))
    return 1 / (1 + math.exp(-lg))


# ── trade-tape pricing + fills ─────────────────────────────────────────────────

BOOK_CACHE_DIR = paths.CACHE_V3
STATE_V3 = paths.STATE_V3


def _book(client, ticker, ko, cache):
    """Per-minute trade book for a leg. Cached in-memory and on disk (settled
    games never change), so re-runs / tuning passes don't re-fetch the tape."""
    if ticker in cache:
        return cache[ticker]
    path = os.path.join(BOOK_CACHE_DIR, f"{ticker}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                book = {int(k): v for k, v in json.load(f).items()}
            cache[ticker] = book
            return book
        except Exception:
            pass
    tr = tt.fetch_trades(client, ticker, min_ts=ko - 86400)
    book = tt.minute_book(tr, ko)
    cache[ticker] = book
    try:
        os.makedirs(BOOK_CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(book, f)
    except Exception:
        pass
    return book


def _enter(teams, cat, tk, parsed, leg_sub, pf, src, book, minute, ec, in_play, fillstat):
    """Each team sizes the leg (strategy_v3) and tries to fill against the tape.
    Bets fail when the order exceeds the volume the tape shows near `minute`."""
    if not _window_open(parsed.get("period", "full"), minute, in_play):
        return                                     # time guardrail: period window closed
    price = tt.price_at(book, minute)
    if price is None or pf is None:
        return
    ask = min(0.98, price + SLIP)
    ask_c = round(ask * 100)
    if ask_c <= 1 or ask_c >= 99:
        return
    # near-certainty guard for in-play: kill the decided-leg "edge" artifact
    if in_play:
        nc = NC_HI.get(cat, NC_DEFAULT)
        if ask >= nc or pf >= nc:
            return
    window = INPLAY_WIN if in_play else PREGAME_WIN
    ctx = {"p_fair": pf, "ask": ask_c, "in_play": in_play, "minute": minute,
           "type": parsed.get("type"), "sigma": 0.12}
    avail = tt.fillable(book, minute, window)   # SHARED tape liquidity, depleted below
    for t in teams:
        if avail < 1:
            break                               # tape exhausted for this leg/window
        if tk in t["bets"] or len(t["bets"]) >= MAX_OPEN:
            continue
        # one leg per event per team (no stacking home+draw+away of one game)
        if any(b.get("event") == ec for b in t["bets"].values()):
            continue
        acc = PaperAccount.from_dict(t["account"])
        stake = s3.Strategist(t["params"], t.get("market_focus")).entry_size(ctx, acc.cash)
        if stake <= 0:
            continue
        want = int(stake / ask)
        if want < 1:
            continue
        filled = min(want, int(avail))          # cap to remaining shared tape volume
        fillstat["met" if filled >= want else "partial"] = \
            fillstat.get("met" if filled >= want else "partial", 0) + 1
        if filled < 1:
            continue
        if not acc.buy(tk, filled, ask, {"sub": leg_sub, "event": ec}):
            continue
        avail -= filled                          # deplete shared liquidity
        cost = filled * ask
        t["staked"] += cost
        t["bets"][tk] = {"sources": {k: v for k, v in src.items() if v is not None},
                         "p_fair": pf, "ask": ask_c, "edge": round(pf - ask, 4),
                         "contracts": filled, "cost": round(cost, 3),
                         "event": ec, "in_play": in_play, "flags": {}}
        t["account"] = acc.to_dict()


def _exit_step(teams, tk, live_pf, book, minute):
    price = tt.price_at(book, minute)
    bid_c = round(max(0.01, price - SLIP) * 100) if price is not None else None
    avail = tt.fillable(book, minute, INPLAY_WIN)   # SHARED exit liquidity, depleted below
    for t in teams:
        if tk not in t["bets"]:
            continue
        acc = PaperAccount.from_dict(t["account"])
        pos = acc.positions.get(tk)
        if not pos:
            continue
        flags = t["bets"][tk].setdefault("flags", {})
        carrier = {"entry": pos["entry"], **flags}
        action, frac = s3.Strategist(t["params"], t.get("market_focus")).exit_decision(
            carrier, live_pf, bid_c)
        # GATE the exit by tape volume (can't dump unlimited size at the last price —
        # this is where the in-play artifact realized on the sell side).
        if action != "HOLD" and bid_c and avail >= 1:
            n = min(max(1, int(pos["contracts"] * frac)), int(avail))
            if n >= 1 and acc.sell(tk, n, bid_c / 100.0):
                avail -= n                          # deplete shared exit liquidity
                s3.disarm(action, carrier)
                # RECORD the realized exit P&L in `closed` so the pre/in-play split and
                # per-market tally see it (was only hitting realized_pnl/fitness before).
                t["closed"].append({"ticker": tk, "outcome": "exit",
                                    "pnl": round(n * (bid_c / 100.0 - pos["entry"]), 3),
                                    "staked": round(n * pos["entry"], 3), "in_play": True})
                t["account"] = acc.to_dict()
                t["fitness"] = _fitness(t)
                if tk not in acc.positions:
                    t["bets"].pop(tk, None)
                    t["n_closed"] += 1
        # RE-ARM FIX: persist flag state every poll (even on HOLD), so the one-shot
        # scale-out can re-arm after the market cools (was discarded on HOLD before).
        if tk in t["bets"]:
            t["bets"][tk]["flags"] = {k: v for k, v in carrier.items() if k != "entry"}


# ── evolution v3 ───────────────────────────────────────────────────────────────

def evolve_v3(teams_by_cat, cat, next_id, gen, rng, log_rows):
    """Breed the top pairs (1x2, 3x4) -> 2 fresh $100 children named gx_<archetype>;
    CULL the absolute-worst #children (removed) and NUDGE the next-worst PUNISH_N
    toward the elite. Steady-state population (cull count == children count)."""
    teams = teams_by_cat[cat]
    eligible = [t for t in teams if t["n_closed"] >= EVOLVE_MIN_N]
    if len(eligible) < 8:
        return f"gen{gen}: <8 eligible, skipped"
    # rank by SHRINKAGE fitness (audit: plain pnl/sqrt(staked) selected luck)
    ranked = sorted(eligible, key=_evo_fitness, reverse=True)
    best = ranked[0]

    # breed the top pairs -> children (child keeps the BETTER parent's archetype
    # name, tagged with the generation: e.g. g3_flow)
    born, children = [], []
    for i, j in ((0, 1), (2, 3)):
        pa, pb = ranked[i], ranked[j]
        params = s3.mutate(s3.crossover(pa["params"], pb["params"], rng), rng)
        focus = pa.get("market_focus") if rng.random() < 0.5 else pb.get("market_focus")
        lineage = f"g{gen}_{_base_name(pa['lineage'])}"
        children.append(_new_team(cat, params, focus, lineage, next_id[0], gen)); next_id[0] += 1
        born.append(lineage)

    # STEADY-STATE: cull the ABSOLUTE worst (= #children) so the population is bounded
    # and capital conserved; NUDGE the next-worst toward the elite (explore). ranked is
    # best→worst, so the absolute worst are at the END of the bottom slice.
    worst = ranked[-(len(children) + PUNISH_N):]
    cull = worst[-len(children):]                       # the very bottom -> removed
    cull_ids = {t["id"] for t in cull}
    nudged = []
    for t in worst[:PUNISH_N]:                          # next-worst -> nudged toward elite
        t["params"] = s3.mutate(s3.crossover(t["params"], best["params"], rng), rng,
                                rate=0.5, scale=0.1)
        nudged.append(t["lineage"])
    culled = [t["lineage"] for t in cull]
    teams_by_cat[cat] = [t for t in teams if t["id"] not in cull_ids] + children

    log_rows.append({"gen": gen, "cat": cat, "born": born, "culled": culled,
                     "nudged": nudged, "elite": best["lineage"],
                     "elite_pnl": round(best["account"]["realized_pnl"], 2)})
    return f"gen{gen}: bred {born}, culled {culled}, nudged {nudged}"


# ── replay ─────────────────────────────────────────────────────────────────────

def simulate(max_games=None, use_llm=False, report=True, persist=True):
    cfg = json.load(open(A.CATS_FILE))["super_categories"]
    next_id = [0]
    rng = random.Random(20260618)
    teams_by_cat = {c: seed_population(c, next_id, rng) for c in SEED_CATS}

    brains = {}
    for c in REPLAY_CATS:
        brains[c] = A.BrainV2({"use_llm": use_llm and (c in LLM_CATS)})

    client = A.KalshiClientV2(req_per_sec=5)
    names = {}
    for m in scn.iter_game_series(client, status="settled"):
        ec = m["ticker"].split("-")[1]
        h, a = scn.home_away(m.get("title"))
        if h:
            names[ec] = (h, a)
    # series universe per pool — for "all" this is the WHOLE discovered Soccer surface
    # (no type allow-list: discover what was open, don't pick). Per-game leg-building
    # below naturally drops tournament outrights (their event codes don't match a
    # game timeline); the structural model prices what it can, the causal LLM the rest.
    cat_series = {c: A.scn.series_for(c, client) for c in REPLAY_CATS}
    need = {"KXWCTOTAL", "KXWCCORNERS"}  # bootstrap — also discovered dynamically at read time
    for c in REPLAY_CATS:
        need |= set(cat_series[c])
    # MEMORY-BOUNDED fetch (the droplet is a 458MB box): pull each series ONE AT A
    # TIME and keep ONLY its per-game legs (events that map to a real game timeline).
    # Discovers the whole surface, but never holds all ~110 series in RAM and drops
    # tournament outrights (no game match) for free.
    settled = {}
    for s in need:
        sd = A._settled_by_event(client, s)
        keep = {ec: legs for ec, legs in sd.items() if ec in names}
        if keep:
            settled[s] = keep
        del sd

    games = sorted(names, key=A._event_date)
    if max_games:
        games = games[:max_games]
    if report:
        print(f"arena_v3 replay: {len(games)} games, steps={MS.STEPS}, "
              f"LLM={'on' if use_llm else 'off'}, fills=trade-tape")

    books, fillstat, evo_rows = {}, {}, []
    cyc = 0
    for ec in games:
        cyc += 1
        books.clear()                 # free the previous game's minute-books (small droplet)
        home, away = names[ec]
        yyyymmdd = "2026" + f"{A._MON.get(ec[2:5].upper(), 0):02d}" + ec[5:7]
        timeline = MS.goal_timeline(home, away, yyyymmdd)

        # this game's legs per category (+ their per-minute trade books)
        legs = {c: [] for c in REPLAY_CATS}
        for cat in REPLAY_CATS:
            for s in cat_series[cat]:
                for leg in settled.get(s, {}).get(ec, []):
                    parsed = mv.parse_market_v2(leg["ticker"], leg["sub"])
                    if parsed["type"] == "first_goalscorer":
                        continue                  # no model + noisy book; genuinely unpriceable
                    ko = leg["ko"]
                    if not ko:
                        continue
                    book = _book(client, leg["ticker"], ko, books)
                    if not book:
                        continue
                    legs[cat].append((leg, parsed, book))

        # market total anchor from this game's total legs (pre-game tape mids)
        mt = None
        tot_pts = []
        for leg in scn.get_settled_total_legs(settled, ec):
            if not leg["ko"]:
                continue
            b = _book(client, leg["ticker"], leg["ko"], books)
            p = tt.price_at(b, -1)
            if p is not None:
                tot_pts.append(p)
        if tot_pts:
            mt = sum(tot_pts)

        # PREWARM novel-market LLM prices CONCURRENTLY (the parallelism) so the
        # per-leg calls below hit cache instead of blocking one-at-a-time.
        if use_llm:
            for cat in REPLAY_CATS:
                novel = []
                for leg, parsed, book in legs[cat]:
                    if parsed["type"] == "unknown":
                        m = dict(leg)
                        m["title"] = leg.get("title") or f"{home} vs {away}"
                        m["yes_bid_cents"] = round((tt.price_at(book, -1) or 0.5) * 100)
                        novel.append(m)
                brains[cat].prewarm_leg_llm(novel)

        # ---- PRE-GAME ----
        for cat in REPLAY_CATS:
            prior = brains[cat].game_prior(home, away, market_total=mt)
            llm = brains[cat].llm_for_game(home, away, ec, prior) if use_llm else None
            for leg, parsed, book in legs[cat]:
                mid = tt.price_at(book, -1)
                if mid is None:
                    continue
                lh = A.scn.leg_is_home(parsed, home, away)
                lpf = (brains[cat].llm_pfair(parsed, prior, lh, llm, ticker=leg["ticker"])
                       if llm else None)
                pf, src = brains[cat].pfair(parsed, prior, lh, market_mid=mid, llm=lpf)
                if pf is None and use_llm:        # no structural model -> causal LLM price
                    m2 = dict(leg); m2["title"] = leg.get("title") or f"{home} vs {away}"
                    m2["yes_bid_cents"] = round(mid * 100)
                    pf, src = brains[cat].llm_price_market(m2, market_mid=mid)
                if pf is None:
                    continue
                pf = _wc_total_pf(parsed, leg["sub"], pf)
                _enter(teams_by_cat[cat], cat, leg["ticker"], parsed, leg["sub"], pf, src,
                       book, -1, ec, in_play=False, fillstat=fillstat)

        # ---- IN-PLAY (stepped, no jump-ahead) ----
        for t_min in MS.STEPS:
            hs, as_ = MS.score_at(timeline, t_min)
            for cat in REPLAY_CATS:
                lprior = brains[cat].live_prior(home, away, t_min, hs, as_, market_total=mt)
                # CAUSAL in-play LLM at this minute (score-throttled cache); never
                # sees the result — only (teams, minute, score-so-far).
                llm_live = (brains[cat].llm_live(home, away, ec, t_min, hs, as_)
                            if use_llm else None)
                for leg, parsed, book in legs[cat]:
                    lh = A.scn.leg_is_home(parsed, home, away)
                    mid = tt.price_at(book, t_min)
                    srcs = {}
                    pdata = brains[cat].live_pfair(parsed, lprior, lh)
                    if pdata is not None:
                        srcs["data"] = pdata
                    if mid is not None:
                        srcs["market"] = mid
                    if llm_live:
                        lp2 = brains[cat].llm_live_pfair(parsed, lprior, lh, llm_live)
                        if lp2 is not None:
                            srcs["llm"] = lp2
                    # generic causal LLM for types with no structural live model
                    # (advance, regulation, margin, matchup, …) — priced from the
                    # ticker/title + current price; never sees the result.
                    if "data" not in srcs and "llm" not in srcs and use_llm:
                        m2 = dict(leg); m2["title"] = leg.get("title") or f"{home} vs {away}"
                        m2["yes_bid_cents"] = round((mid or 0) * 100)
                        _, s2 = brains[cat].llm_price_market(m2, market_mid=mid)
                        srcs.update(s2)
                    if not srcs:
                        continue
                    pf = brains[cat]._stack(srcs)
                    if pf is None:
                        continue
                    if t_min < 90:
                        _enter(teams_by_cat[cat], cat, leg["ticker"], parsed, leg["sub"],
                               pf, srcs, book, t_min, ec, in_play=True, fillstat=fillstat)
                    _exit_step(teams_by_cat[cat], leg["ticker"], pf, book, t_min)

        # ---- SETTLE ----
        for cat in REPLAY_CATS:
            res = {leg["ticker"]: leg["result"] for leg, _, _ in legs[cat]}
            rb = {}
            for t in teams_by_cat[cat]:
                acc = PaperAccount.from_dict(t["account"])
                for tk in list(t["bets"]):
                    if tk in res:
                        bet = t["bets"].pop(tk)
                        pnl = acc.settle(tk, res[tk])
                        t["closed"].append({"ticker": tk, "outcome": res[tk],
                                            "pnl": round(pnl, 3), "edge": bet.get("edge"),
                                            "staked": bet.get("cost"),
                                            "in_play": bet.get("in_play", False)})
                        t["n_closed"] += 1
                        rb[tk] = {"sources": bet.get("sources", {}), "outcome": res[tk]}
                t["account"] = acc.to_dict()
                t["fitness"] = _fitness(t)
            if rb:
                brains[cat].update_stacker(list(rb.values()))

        # ---- EVOLVE ----
        if cyc % EVOLVE_EVERY == 0:
            for cat in REPLAY_CATS:
                g = cyc // EVOLVE_EVERY
                evolve_v3(teams_by_cat, cat, next_id, g, rng, evo_rows)

        if report:
            h9, a9 = MS.score_at(timeline, 90)
            print(f"  [{cyc}/{len(games)}] {home} {h9}-{a9} {away}  goals={len(timeline)}")

    if persist:                                  # in-memory only when False (re-grade
        for cat in SEED_CATS:                    #   runs that must NOT touch live state)
            A._save(os.path.join(STATE_V3, f"teams_{cat}.json"), teams_by_cat[cat])
        for cat in REPLAY_CATS:                  # tuned stacker weights for the live path
            A._save(os.path.join(STATE_V3, f"brain_{cat}.json"), brains[cat].weights)
        if evo_rows:                             # seed the evolution track from the replay
            os.makedirs(os.path.dirname(EVO_LOG_V3), exist_ok=True)
            with open(EVO_LOG_V3, "w") as f:
                for r in evo_rows:
                    f.write(json.dumps(r) + "\n")
    if report:
        _report(teams_by_cat, fillstat, evo_rows)
    return teams_by_cat, fillstat, evo_rows


def _split_stats(t):
    """((pre_pnl, pre_n, pre_win, pre_fit), (inplay_...)) from a team's closed bets."""
    def agg(rows):
        p = sum(cb["pnl"] for cb in rows)
        st = sum(cb.get("staked") or 0 for cb in rows)
        w = sum(1 for cb in rows if cb.get("outcome") == 1)
        return p, len(rows), w, p / math.sqrt(st + 1.0)
    pre = [cb for cb in t["closed"] if cb.get("pnl") is not None and not cb.get("in_play")]
    inp = [cb for cb in t["closed"] if cb.get("pnl") is not None and cb.get("in_play")]
    return agg(pre), agg(inp)


def pregame_board(teams):
    """game_lines teams ranked by PRE-GAME fitness — the promotion-relevant signal."""
    rows = []
    for t in teams:
        (pp, pn, pw, pf), _ = _split_stats(t)
        if pn:
            rows.append((t, pp, pn, pw, pf))
    return sorted(rows, key=lambda r: r[4], reverse=True)


def _report(teams_by_cat, fillstat, evo_rows):
    print(f"\n=== FILL REALISM (trade-tape) ===  {fillstat}")
    print(f"=== EVOLUTION EVENTS === {len(evo_rows)}")
    for r in evo_rows:
        print(f"  {r['cat']:<11} {r['gen']}: born {r['born']} "
              f"culled {r.get('culled', r.get('punished', []))} "
              f"elite={r['elite']} (${r['elite_pnl']:+.2f})")
    print("\n=== ARENA v3 STANDINGS ===")
    for cat in REPLAY_CATS:
        teams = sorted(teams_by_cat[cat], key=lambda t: t["fitness"], reverse=True)
        print(f"\n--- {cat} --- ({len(teams)} teams)")
        for t in teams:
            acc = t["account"]
            mf = (",".join(t["market_focus"]) if t.get("market_focus") else "all")
            print(f"  {t['lineage']:<16} fit={t['fitness']:+.3f} "
                  f"pnl=${acc['realized_pnl']:+7.2f} cash=${acc['cash']:6.2f} "
                  f"closed={t['n_closed']:3d} focus={mf}")
        mk = A._per_market_pnl(teams)
        for s, (p, n, w) in sorted(mk.items(), key=lambda x: -x[1][0]):
            if n:
                print(f"     {s:<14} ${p:+8.2f} ({n} bets, {100*w/n:.0f}% win)")
        # PRE-GAME vs IN-PLAY split — pre-game is the trustworthy, investable read;
        # in-play still carries a settlement-front-running component.
        for label, flag in (("PRE-GAME", False), ("IN-PLAY", True)):
            p = n = w = 0
            for t in teams:
                for cb in t["closed"]:
                    if cb.get("pnl") is None or bool(cb.get("in_play")) != flag:
                        continue
                    p += cb["pnl"]; n += 1; w += 1 if cb.get("outcome") == 1 else 0
            if n:
                print(f"   [{label:<8}] ${p:+8.2f} ({n} bets, {100*w/n:.0f}% win)")

    # promotion board: unified pool ranked by PRE-GAME fitness only
    print("\n=== PRE-GAME PROMOTION BOARD (rank by pre-game fitness) ===")
    board_teams = teams_by_cat.get("all") or next(iter(teams_by_cat.values()), [])
    for t, pp, pn, pw, pf in pregame_board(board_teams)[:8]:
        p = t["params"]
        print(f"  {t['lineage']:<16} pre_fit={pf:+.3f} pre_pnl=${pp:+7.2f} "
              f"({pn} bets, {100*pw/pn:.0f}% win)  band=[{p['price_floor']:.2f},"
              f"{p['price_ceiling']:.2f}] tilt={p['alloc_tilt']} cap={p['unit_cap_frac']} "
              f"kelly={p['kelly_fraction']} min_edge={p['min_edge']} "
              f"inplay={p.get('inplay')} focus={t.get('market_focus') or 'all'}")


# ── forward cron loop (paper, evolving) — "keep the arena running" ─────────────
TALLY_V3 = os.path.join(paths.LOGS_DIR, "arena_v3.jsonl")
EVO_LOG_V3 = os.path.join(paths.LOGS_DIR, "evolution_v3.jsonl")


def _base_rate_legs(games):
    """Apply the WC base-rate totals prior to every total leg, in place."""
    for g in games:
        for lg in g["legs"]:
            parsed = mv.parse_market_v2(lg["ticker"], lg.get("sub"))
            lg["_type"] = parsed.get("type")
            lg["_period"] = parsed.get("period")
            # base-rate prior is a PRE-GAME prior only; applying it in-play double-
            # counts (the live model already saw the score) and fabricated late edges.
            if parsed.get("type") == "total" and not lg.get("in_play"):
                lg["p_fair"] = _wc_total_pf(parsed, lg.get("sub"), lg["p_fair"])


def _enter_live(teams_by_cat, games_by_cat):
    placed = 0
    for cat, teams in teams_by_cat.items():
        for g in games_by_cat.get(cat, []):
            for lg in g["legs"]:
                tk, ask = lg["ticker"], lg["ask"]
                in_play = lg.get("in_play", False)
                if not ask or not _window_open(lg.get("_period", "full"),
                                               lg.get("minute"), in_play):
                    continue                       # time guardrail: period window closed
                # IN-PLAY ENABLED for live games — this is a REAL forward test of whether
                # momentum/scalper work (live bets fill at the real book; the artifact was
                # a BACKTEST-only problem, fixed there via the trade tape). The only guard
                # kept is the near-certainty cap: skip legs that are essentially RESOLVED
                # (>=NC) where there's no room to trade — not a ban on in-play betting.
                if in_play:
                    nc = NC_HI.get(cat, NC_DEFAULT)
                    if ask / 100.0 >= nc or lg["p_fair"] >= nc:
                        continue
                # REAL FILL CAP: the live top-of-book ask depth, SHARED across teams
                # (depletes as they fill) — no more frictionless infinite paper fills.
                depth = int(lg.get("ask_size", 0))
                if depth < 1:
                    continue
                ctx = {"p_fair": lg["p_fair"], "ask": ask, "in_play": in_play,
                       "minute": lg.get("minute"), "type": lg.get("_type"), "sigma": 0.12}
                tau = _tau_days(lg.get("close_time"))    # resolution-time -> TVM discount
                for t in teams:
                    if depth < 1:
                        break
                    if tk in t["bets"] or len(t["bets"]) >= MAX_OPEN:
                        continue
                    # one leg per event per team (no stacking all legs of one game)
                    if any(b.get("event") == g["event"] for b in t["bets"].values()):
                        continue
                    acc = PaperAccount.from_dict(t["account"])
                    stake = s3.Strategist(t["params"], t.get("market_focus")).entry_size(
                        ctx, acc.cash, tau_days=tau)
                    if stake <= 0:
                        continue
                    n = min(kelly.to_contracts(stake, ask / 100.0), depth)   # cap to live depth
                    if n < 1 or not acc.buy(tk, n, ask / 100.0, {"sub": lg["sub"], "event": g["event"]}):
                        continue
                    depth -= n                       # deplete shared liquidity
                    cost = n * ask / 100.0
                    t["staked"] += cost
                    t["bets"][tk] = {"sources": lg.get("sources", {}), "p_fair": lg["p_fair"],
                                     "ask": ask, "edge": round(lg["p_fair"] - ask / 100.0, 4),
                                     "contracts": n, "cost": round(cost, 3), "event": g["event"],
                                     "in_play": lg.get("in_play", False), "flags": {}}
                    t["account"] = acc.to_dict()
                    placed += 1
    return placed


def _exit_live(teams_by_cat, games_by_cat):
    exited = 0
    for cat, teams in teams_by_cat.items():
        live_legs = {lg["ticker"]: lg for g in games_by_cat.get(cat, [])
                     for lg in g["legs"] if lg.get("in_play")}
        if not live_legs:
            continue
        # live bid depth per leg, SHARED across teams (can't all dump into one bid)
        bid_left = {tk: int(lg.get("bid_size", 0)) for tk, lg in live_legs.items()}
        for t in teams:
            acc = PaperAccount.from_dict(t["account"])
            changed = False
            for tk, lg in live_legs.items():
                if tk not in t["bets"]:
                    continue
                pos = acc.positions.get(tk)
                if not pos:
                    continue
                flags = t["bets"][tk].setdefault("flags", {})
                carrier = {"entry": pos["entry"], **flags}
                action, frac = s3.Strategist(t["params"], t.get("market_focus")).exit_decision(
                    carrier, lg["p_fair"], lg.get("bid"))
                if action != "HOLD" and lg.get("bid"):
                    n = min(max(1, int(pos["contracts"] * frac)), bid_left.get(tk, 0))
                    if n >= 1 and acc.sell(tk, n, lg["bid"] / 100.0):
                        bid_left[tk] -= n            # deplete shared bid depth
                        s3.disarm(action, carrier)
                        changed = True
                        exited += 1
                if tk in t["bets"]:
                    if tk not in acc.positions:
                        t["bets"].pop(tk, None)
                        t["closed"].append({"ticker": tk, "outcome": "exit", "pnl": None,
                                            "in_play": True})
                        t["n_closed"] += 1
                    else:
                        t["bets"][tk]["flags"] = {k: v for k, v in carrier.items() if k != "entry"}
            if changed:
                t["account"] = acc.to_dict()
                t["fitness"] = _fitness(t)
    return exited


def _settle_live(client, teams_by_cat, brains, cycle):
    """Settle held tickers on Kalshi result; tag closed bets in_play for the
    promotion pre-game/in-play split; feed the Brain Assistant."""
    open_tk = set()
    for teams in teams_by_cat.values():
        for t in teams:
            open_tk |= set(t["bets"])
    if not open_tk:
        return 0, {}
    results = {}
    for m in client.list_markets_by_tickers(list(open_tk)):
        r = (m.get("result") or "").lower()
        if m.get("status") in ("settled", "finalized") and r in ("yes", "no"):
            results[m["ticker"]] = 1 if r == "yes" else 0
    n_settled, events_by_cat = 0, {}
    for cat, teams in teams_by_cat.items():
        rb, ev = {}, set()
        for t in teams:
            acc = PaperAccount.from_dict(t["account"])
            for tk in list(t["bets"]):
                if tk in results:
                    bet = t["bets"].pop(tk)
                    ev.add(bet.get("event"))
                    pnl = acc.settle(tk, results[tk])
                    t["closed"].append({"ticker": tk, "outcome": results[tk], "pnl": round(pnl, 3),
                                        "edge": bet.get("edge"), "staked": bet.get("cost"),
                                        "in_play": bet.get("in_play", False)})
                    t["n_closed"] += 1
                    n_settled += 1
                    rb[tk] = {"sources": bet.get("sources", {}), "outcome": results[tk]}
            t["account"] = acc.to_dict()
            t["fitness"] = _fitness(t)
        if rb:
            brains[cat].update_stacker(list(rb.values()))
        events_by_cat[cat] = len(ev)
    return n_settled, events_by_cat


def cycle_once():
    """One forward tick (paper): settle → price coming/in-play games (base-rate
    prior on) → enter (strategy_v3) → exit → evolve every 8 resolved games."""
    if A.kalshi_maintenance():
        print("kalshi maintenance window (3-5am ET) — skipping cycle")
        return
    cfg = json.load(open(A.CATS_FILE))["super_categories"]
    meta = A._load(os.path.join(STATE_V3, "meta.json"),
                   {"cycle": 0, "next_id": 0, "generation": {}, "resolved_since_evolve": {}})
    next_id = [meta["next_id"]]
    cycle = meta["cycle"] + 1
    rng = random.Random(90000 + cycle)

    brains, teams_by_cat = {}, {}
    for c in SEED_CATS:
        bw = A._load(os.path.join(STATE_V3, f"brain_{c}.json"), None)
        # LLM on only for the wild 'discovered' bucket (no structural model there);
        # curated cats stay model+market for comparable forward samples + no key dep.
        bconf = {"use_llm": c in LLM_CATS}
        if bw:
            bconf["stacker_weights"] = bw
        # persist the per-ticker LLM cache so a discovered market is LLM-priced at
        # most once (cost control), not every cycle.
        bconf["llm_cache"] = A._load(os.path.join(STATE_V3, f"llm_cache_{c}.json"), {})
        brains[c] = A.BrainV2(bconf)
        t = A._load(os.path.join(STATE_V3, f"teams_{c}.json"), None)
        teams_by_cat[c] = t if t else seed_population(c, next_id, rng)

    client = A.KalshiClientV2(req_per_sec=6)
    n_settled, ev = _settle_live(client, teams_by_cat, brains, cycle)
    try:
        pdb.grade(client)                          # settle the predictions ledger
    except Exception as e:
        print(f"  [WARN] grade predictions: {e}")
    games_by_cat = {}
    for c in SEED_CATS:
        try:
            gs = A.scn.price_games(c, client, brains[c], max_events=6, live=True)
            _base_rate_legs(gs)
            games_by_cat[c] = gs
        except Exception as e:
            print(f"  [WARN] price {c}: {e}")
            games_by_cat[c] = []
    # RECORD every priced leg's prediction (the rights/wrongs dataset across the
    # whole surface — graded on resolution above next cycle).
    for c, gs in games_by_cat.items():
        for g in gs:
            for lg in g["legs"]:
                try:
                    pdb.log_prediction({
                        "ticker": lg["ticker"], "series": lg["ticker"].split("-")[0],
                        "event": g.get("event"), "sub": lg.get("sub"),
                        "type": lg.get("_type") or lg.get("type"),
                        "period": lg.get("_period") or lg.get("period"),
                        "in_play": lg.get("in_play"), "minute": lg.get("minute"),
                        "p_fair": lg.get("p_fair"), "source": lg.get("source"),
                        "sources": lg.get("sources"), "ask": lg.get("ask"), "bid": lg.get("bid"),
                        "edge": round((lg.get("p_fair") or 0) - (lg.get("ask") or 0) / 100.0, 4),
                        "cat": c, "close_time": lg.get("close_time")})
                except Exception:
                    pass
    placed = _enter_live(teams_by_cat, games_by_cat)
    exited = _exit_live(teams_by_cat, games_by_cat)

    rse = meta.get("resolved_since_evolve", {})
    gens = meta.get("generation", {})
    evo_rows = []
    for c in SEED_CATS:
        rse[c] = rse.get(c, 0) + ev.get(c, 0)
        if rse[c] >= EVOLVE_EVERY:
            gens[c] = gens.get(c, 0) + 1
            evolve_v3(teams_by_cat, c, next_id, gens[c], rng, evo_rows)
            rse[c] = 0

    for c in SEED_CATS:
        A._save(os.path.join(STATE_V3, f"teams_{c}.json"), teams_by_cat[c])
        A._save(os.path.join(STATE_V3, f"brain_{c}.json"), brains[c].weights)
        A._save(os.path.join(STATE_V3, f"llm_cache_{c}.json"), brains[c].llm_cache)
    meta.update({"cycle": cycle, "next_id": next_id[0], "generation": gens,
                 "resolved_since_evolve": rse})
    A._save(os.path.join(STATE_V3, "meta.json"), meta)
    if evo_rows:
        os.makedirs(os.path.dirname(EVO_LOG_V3), exist_ok=True)
        with open(EVO_LOG_V3, "a") as f:
            for r in evo_rows:
                f.write(json.dumps(r) + "\n")

    tally = {"cycle": cycle, "settled": n_settled, "placed": placed, "exited": exited}
    for c in SEED_CATS:
        teams = teams_by_cat[c]
        bp = pregame_board(teams)
        best = bp[0] if bp else None
        tally[c] = ({"best_pregame": best[0]["lineage"], "pre_pnl": round(best[1], 2),
                     "pre_n": best[2]} if best else {"best_pregame": None})
    os.makedirs(os.path.dirname(TALLY_V3), exist_ok=True)
    with open(TALLY_V3, "a") as f:
        f.write(json.dumps(tally) + "\n")
    print(json.dumps(tally, indent=2))
    return tally


def status():
    """The 'check the arena' report: games resolved, games ongoing now, the
    leaderboard per super-category (PnL + fitness = risk-adjusted PnL), and the
    full evolution tracks (bred children + punished bottom teams)."""
    teams_by_cat = {c: A._load(os.path.join(STATE_V3, f"teams_{c}.json"), []) for c in SEED_CATS}

    # 1) games resolved (distinct events with a settled bet) + games ongoing now
    resolved = set()
    for c in SEED_CATS:
        for t in teams_by_cat[c]:
            for cb in t["closed"]:
                if cb.get("pnl") is not None and "-" in cb["ticker"]:
                    resolved.add(cb["ticker"].split("-")[1])
    print("================  ARENA v3  ================")
    print(f"Games resolved (arena bet on): {len(resolved)}")
    try:
        from wc.lib import live_feed as lf
        live, upcoming = [], []
        for ev in lf.scoreboard_events():
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) != 2:
                continue
            nm = {c.get("homeAway"): (c.get("team", {}).get("displayName", ""),
                                      int(c.get("score") or 0)) for c in cs}
            h, a = nm.get("home"), nm.get("away")
            if not h or not a:
                continue
            st = (ev.get("status", {}).get("type", {}).get("state") or "pre").lower()
            if st == "in":
                live.append(f"{h[0]} {h[1]}-{a[1]} {a[0]} (@{lf._parse_minute(ev.get('status',{}))}')")
            elif st == "pre":
                upcoming.append(f"{h[0]} v {a[0]}")
        print(f"Ongoing now ({len(live)}):" + ("" if live else " none"))
        for ln in live:
            print(f"   🔴 LIVE  {ln}")
        if upcoming:
            print(f"Upcoming soon ({len(upcoming)}): " + ", ".join(upcoming[:8]))
    except Exception as e:
        print(f"(ongoing-games check failed: {e})")

    # 2) leaderboard per super-category (fitness = PnL / sqrt(staked) = risk-adjusted)
    for c in SEED_CATS:
        teams = sorted(teams_by_cat[c], key=lambda t: t["fitness"], reverse=True)
        print(f"\n--- {c} --- ({len(teams)} teams)   fitness = risk-adjusted PnL")
        print(f"  {'team':<17}{'fitness':>9}{'PnL':>10}{'pre-game PnL':>14}{'closed':>8}{'cash':>9}")
        for t in teams[:6]:
            (pp, pn, pw, pf), _ = _split_stats(t)
            acc = t["account"]
            print(f"  {t['lineage']:<17}{t['fitness']:>+9.3f}{acc['realized_pnl']:>+10.2f}"
                  f"{pp:>+14.2f}{t['n_closed']:>8}{acc['cash']:>9.2f}")

    # 3) full evolution tracks — what got bred and what got punished each generation
    print("\n================  EVOLUTION TRACKS  ================")
    rows = []
    try:
        with open(EVO_LOG_V3) as f:
            rows = [json.loads(l) for l in f]
    except FileNotFoundError:
        pass
    if not rows:
        print("  (no evolution events yet — first fires at 8 resolved games per category)")
    for r in rows:
        culled = r.get('culled', r.get('punished', []))
        nudged = r.get('nudged', [])
        print(f"  {r['cat']:<11} gen{r['gen']}: BRED {r['born']} | CULLED {culled} | "
              f"NUDGED {nudged} | elite {r['elite']} (${r['elite_pnl']:+.2f})")


SEED_EVENTS_FILE = os.path.join(STATE_V3, "seed_events.json")


def snapshot_seed_events():
    """Freeze the set of replay-seed event codes (the 28 in-sample games) so the
    forward report can exclude them. Run ONCE after the replay, before forward
    games resolve."""
    evs = set()
    for c in SEED_CATS:
        for t in A._load(os.path.join(STATE_V3, f"teams_{c}.json"), []):
            for cb in t["closed"]:
                if cb.get("pnl") is not None and "-" in cb["ticker"]:
                    evs.add(cb["ticker"].split("-")[1])
    A._save(SEED_EVENTS_FILE, sorted(evs))
    print(f"snapshot: froze {len(evs)} replay-seed events -> {SEED_EVENTS_FILE}")
    return evs


def forward_report():
    """Out-of-sample report: ONLY forward-resolved bets (events not in the seed
    snapshot), so we can compare live forward P&L against the in-sample +$648."""
    seed = set(A._load(SEED_EVENTS_FILE, []))
    if not seed:
        print("no seed snapshot — run `arena_v3.py --snapshot-seed` first")
        return
    print("═══ ARENA · fwd ═══")
    for c in SEED_CATS:
        teams = A._load(os.path.join(STATE_V3, f"teams_{c}.json"), [])
        fwd_events, per_team = set(), {}
        for t in teams:
            for cb in t["closed"]:
                if cb.get("pnl") is None or "-" not in cb["ticker"]:
                    continue
                ec = cb["ticker"].split("-")[1]
                if ec in seed:
                    continue
                fwd_events.add(ec)
                pt = per_team.setdefault(t["lineage"], [0.0, 0])
                pt[0] += cb["pnl"]; pt[1] += 1
        print(f"\n── {c} · {len(fwd_events)}g ──")
        if not per_team:
            print("(no fwd bets yet)")
            continue
        ranked = sorted(per_team.items(), key=lambda x: -x[1][0])
        print("%-10s %7s %4s" % ("Team", "PnL", "N"))
        for ln, (p, n) in ranked[:3]:
            print("%-10s %+7.2f %4d" % (ln[:10], p, n))
        if len(ranked) > 3:
            ln, (p, n) = ranked[-1]
            print("↓worst")
            print("%-10s %+7.2f %4d" % (ln[:10], p, n))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--once":
        from wc import cycle; cycle.run(execute=False)
    elif arg == "--status":
        status()
    elif arg == "--snapshot-seed":
        snapshot_seed_events()
    elif arg == "--forward-report":
        forward_report()
    else:
        mg = None
        use_llm = "--llm" in sys.argv
        for a in sys.argv[1:]:
            if a.isdigit():
                mg = int(a)
        simulate(max_games=mg, use_llm=use_llm)
