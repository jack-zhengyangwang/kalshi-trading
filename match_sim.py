"""
match_sim.py — realistic in-play match simulation of the WC group stage.

Unlike arena_v2.replay (which prices each leg ONCE pre-game and jumps to the
result), this simulates each game as if the agents were watching it live:

  • PRE-GAME: agents bet at the pre-kickoff quote (LLM priors on).
  • IN-PLAY: step 0/15/30/45(HALFTIME)/60/75/90 min. At each step we reconstruct
    the score from ESPN goal timings, read the REAL market quote at that minute
    from interval=1 candlesticks, re-price via the live brain (LLM re-queried at
    halftime), let momentum/scalp ENTER and run the hardened EXIT strategy.
  • SETTLE on the real result; chronological through round 1, evolving every 4.

Faithful for score-driven legs (winner/spread/total/team-total/BTTS/first-to-
score) — real per-minute prices + real score progression. Corners are skipped
(no historical minute-by-minute corner data; covered forward by the live arena).

In-memory; reports standings + per-market P&L. Does NOT touch the live droplet
state. Decide the promotion target from THIS, not the noisy pre-game replay seed.

    python3 match_sim.py [max_games]
"""
import datetime as dt
import json
import os
import re
import sys

import requests

import arena_v2 as A
from group.paper import PaperAccount
from group import live_feed as lf, kelly
from group.exit_rules import decide_exit, disarm
import strategy_v2 as sv
import markets_v2 as mv

STEPS = [0, 15, 30, 45, 60, 75, 90]     # in-play minutes; 45 = halftime (LLM re-query)
HALFTIME = 45
PRICED = {"winner", "spread", "total", "team_total", "btts", "first_to_score"}  # score-driven

# module caches so bootstrap re-runs reuse fetched data (fetch once, sim many)
_CANDLE_CACHE = {}
_TL_CACHE = {}
_DATA_CACHE = {}


def _espn_event_id(home, away, yyyymmdd):
    url = ("https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/"
           f"scoreboard?dates={yyyymmdd}")
    try:
        evs = requests.get(url, timeout=15).json().get("events", [])
    except Exception:
        return None
    for ev in evs:
        comp = (ev.get("competitions") or [{}])[0]
        ns = [c.get("team", {}).get("displayName", "") for c in comp.get("competitors", [])]
        if len(ns) == 2 and (
                (lf._names_match(home, ns[0]) and lf._names_match(away, ns[1])) or
                (lf._names_match(home, ns[1]) and lf._names_match(away, ns[0]))):
            return ev.get("id")
    return None


def _minute(clock_str):
    m = re.match(r"(\d+)", str(clock_str or ""))
    return int(m.group(1)) if m else None


def goal_timeline(home, away, yyyymmdd):
    """[(effective_minute, 'home'|'away')] from ESPN keyEvents, or [] if unavailable."""
    ck = f"{yyyymmdd}:{home}:{away}"
    if ck in _TL_CACHE:
        return _TL_CACHE[ck]
    out = _goal_timeline(home, away, yyyymmdd)
    _TL_CACHE[ck] = out
    return out


def _goal_timeline(home, away, yyyymmdd):
    eid = _espn_event_id(home, away, yyyymmdd)
    if not eid:
        return []
    try:
        d = requests.get("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                         f"fifa.world/summary?event={eid}", timeout=15).json()
    except Exception:
        return []
    out = []
    for e in d.get("keyEvents", []):
        t = (e.get("type") or {}).get("text", "")
        if "Goal" not in t and not e.get("scoringPlay"):
            continue
        if "own goal" in t.lower():
            continue
        mn = _minute((e.get("clock") or {}).get("displayValue"))
        team = (e.get("team") or {}).get("displayName", "")
        if mn is None or not team:
            continue
        side = "home" if lf._names_match(home, team) else (
            "away" if lf._names_match(away, team) else None)
        if side:
            out.append((mn, side))
    return sorted(out)


def score_at(timeline, minute):
    h = sum(1 for m, s in timeline if m <= minute and s == "home")
    a = sum(1 for m, s in timeline if m <= minute and s == "away")
    return h, a


def candle_series(client, series, ticker, ko):
    """{minute_offset: (bid,ask) dollars} per-minute through the game window."""
    if ticker in _CANDLE_CACHE:
        return _CANDLE_CACHE[ticker]
    out = _candle_series(client, series, ticker, ko)
    _CANDLE_CACHE[ticker] = out
    return out


def _candle_series(client, series, ticker, ko):
    path = f"/series/{series}/markets/{ticker}/candlesticks"
    try:
        r = client.session.get(client.base_url + path,
                               headers=client._auth_headers("GET", path),
                               params={"start_ts": ko - 3600, "end_ts": ko + 8000,
                                       "period_interval": 1}, timeout=20)
        cs = r.json().get("candlesticks", []) if r.ok else []
    except Exception:
        return {}
    out = {}
    for c in cs:
        ts = c.get("end_period_ts") or c.get("ts")
        b = (c.get("yes_bid") or {}).get("close_dollars")
        a = (c.get("yes_ask") or {}).get("close_dollars")
        if ts is None or b in (None, "") or a in (None, ""):
            continue
        out[int((int(ts) - ko) / 60)] = (float(b), float(a))
    return out


def quote_at(series_dict, minute):
    """Nearest quote at or before `minute` (pre-game uses the latest <=0)."""
    keys = [k for k in series_dict if k <= minute]
    if not keys:
        keys = sorted(series_dict)[:1]
        if not keys:
            return (None, None)
    return series_dict[max(keys)] if keys else (None, None)


def simulate(max_games=None, seed=0, report=True, llm_shared=None):
    A._SEED_OFFSET = seed
    cats = A.active_cats()
    next_id = [0]
    cfg = json.load(open(A.CATS_FILE))["super_categories"]
    brains = {}
    for c in cats:
        conf = {"use_llm": cfg[c].get("use_llm", False)}
        if llm_shared is not None:
            conf["llm_cache"] = llm_shared.setdefault(c, {})   # shared across runs
        brains[c] = A.BrainV2(conf)
    teams_by_cat = {c: A._seed_population(c, 0, next_id) for c in cats}
    client = A.KalshiClientV2(req_per_sec=5)

    if _DATA_CACHE:
        names, settled = _DATA_CACHE["names"], _DATA_CACHE["settled"]
    else:
        names = {}
        for m in client.list_markets_by_series("KXWCGAME", status="settled"):
            ec = m["ticker"].split("-")[1]
            h, a = A.scn.home_away(m.get("title"))
            if h:
                names[ec] = (h, a)
        need = set(A.scn.CONTEXT_SERIES)
        for c in cats:
            need |= set(cfg[c]["series"])
        settled = {s: A._settled_by_event(client, s) for s in need}
        _DATA_CACHE["names"], _DATA_CACHE["settled"] = names, settled
    games = sorted([e for e in names], key=A._event_date)
    if max_games:
        games = games[:max_games]
    if report:
        print(f"match_sim: {len(games)} games, steps={STEPS}, LLM on")

    gens, cyc = {}, 0
    for ec in games:
        cyc += 1
        home, away = names[ec]
        yyyymmdd = "2026" + f"{A._MON.get(ec[2:5].upper(),0):02d}" + ec[5:7]
        timeline = goal_timeline(home, away, yyyymmdd)
        # market anchors (pre-game candlestick mids)
        qc = {}
        mt = _anchor(client, settled, "KXWCTOTAL", ec, qc)
        cmean = _anchor(client, settled, "KXWCCORNERS", ec, qc, corners=True)

        # collect this game's legs we will simulate (skip corners/unknown)
        legs = {}        # cat -> [(leg, parsed, ko, series_dict)]
        for cat in cats:
            cseries = json.load(open(A.CATS_FILE))["super_categories"][cat]["series"]
            lst = []
            for s in cseries:
                for leg in settled.get(s, {}).get(ec, []):
                    parsed = mv.parse_market_v2(leg["ticker"], leg["sub"])
                    if parsed["type"] not in PRICED:
                        continue
                    sd = candle_series(client, s, leg["ticker"], leg["ko"])
                    lst.append((leg, parsed, sd))
            legs[cat] = lst

        # ---- PRE-GAME entries ----
        for cat in cats:
            prior = brains[cat].game_prior(home, away, market_total=mt, market_corner_total=cmean)
            llm = brains[cat].llm_for_game(home, away, ec, prior)
            for leg, parsed, sd in legs[cat]:
                b, a = quote_at(sd, -1)
                if not a:
                    continue
                lh = A.scn.leg_is_home(parsed, home, away)
                lpf = brains[cat].llm_pfair(parsed, prior, lh, llm, ticker=leg["ticker"]) if llm else None
                pf, src = brains[cat].pfair(parsed, prior, lh, market_mid=((b + a) / 2 if b else a), llm=lpf)
                _enter(teams_by_cat[cat], leg, parsed, pf, src, round(a * 100), ec, cat, in_play=False, minute=None)

        # ---- IN-PLAY steps ----
        for t in STEPS:
            hs, as_ = score_at(timeline, t)
            for cat in cats:
                lprior = brains[cat].live_prior(home, away, t, hs, as_,
                                                market_total=mt, market_corner_total=cmean)
                llm_ht = brains[cat].llm_halftime(home, away, ec, lprior) if t == HALFTIME else None
                for leg, parsed, sd in legs[cat]:
                    b, a = quote_at(sd, t)
                    lh = A.scn.leg_is_home(parsed, home, away)
                    pdata = brains[cat].live_pfair(parsed, lprior, lh)
                    if pdata is None:
                        continue
                    srcs = {"data": pdata}
                    mid = ((b + a) / 2) if (b and a) else (b or a)
                    if mid:
                        srcs["market"] = mid
                    if llm_ht:
                        lp2 = brains[cat].llm_live_pfair(parsed, lprior, lh, llm_ht)
                        if lp2 is not None:
                            srcs["llm"] = lp2
                    pf = brains[cat]._stack(srcs)
                    # entries only while the match is live (no post-whistle bets);
                    # exits run at every step including FT.
                    if a and t < 90:
                        _enter(teams_by_cat[cat], leg, parsed, pf, srcs, round(a * 100), ec,
                               cat, in_play=True, minute=t)
                    _exit_step(teams_by_cat[cat], leg, pf, b)

        # ---- SETTLE ----
        res = {}
        for cat in cats:
            for leg, parsed, sd in legs[cat]:
                res[leg["ticker"]] = leg["result"]
        for cat in cats:
            rb = {}
            for t in teams_by_cat[cat]:
                acc = PaperAccount.from_dict(t["account"])
                for tk in list(t["bets"]):
                    if tk in res:
                        bet = t["bets"].pop(tk)
                        pnl = acc.settle(tk, res[tk])
                        t["closed"].append({"ticker": tk, "outcome": res[tk],
                                            "pnl": round(pnl, 3), "edge": bet.get("edge"),
                                            "staked": bet.get("cost")})
                        t["n_closed"] += 1
                        rb[tk] = {"sources": bet.get("sources", {}), "outcome": res[tk]}
                t["account"] = acc.to_dict()
                t["fitness"] = A._fitness(t)
            if rb:
                brains[cat].update_stacker(list(rb.values()))
        if cyc % A.ROUND_GAMES == 0:
            for cat in cats:
                gens[cat] = gens.get(cat, 0) + 1
                A._evolve_cat(teams_by_cat, cat, cyc, next_id, gens[cat], [])
        if report:
            print(f"  [{cyc}/{len(games)}] {home} {score_at(timeline,90)[0]}-"
                  f"{score_at(timeline,90)[1]} {away}  (goals: {len(timeline)})")

    if report:
        _report(teams_by_cat, cats)
    return teams_by_cat


def bootstrap(n=5, max_games=None):
    """Fetch once, simulate N times with different RNG seeds (team init + evolution)
    on the SAME games/quotes/LLM, to measure how stable the in-play leaderboard is."""
    import statistics as st
    cats = A.active_cats()
    llm_shared = {c: {} for c in cats}     # LLM fixed across runs (only the seed varies)
    print(f"Bootstrapping {n} in-play sim runs (fetch once, vary RNG)...")
    runs = [simulate(max_games=max_games, seed=s, report=False, llm_shared=llm_shared)
            for s in range(n)]
    print(f"\n=== IN-PLAY BOOTSTRAP VARIANCE over {n} runs ===")
    for cat in cats:
        pnls, fits, winners = [], [], []
        for tbc in runs:
            b = max(tbc[cat], key=lambda t: t["fitness"])
            pnls.append(b["account"]["realized_pnl"])
            fits.append(b["fitness"])
            winners.append(b["lineage"])
        sd = st.pstdev(pnls) if len(pnls) > 1 else 0.0
        print(f"\n{cat}: best-team P&L ${min(pnls):+.0f}..${max(pnls):+.0f} "
              f"(mean ${st.mean(pnls):+.0f}, sd ${sd:.0f}); fitness "
              f"{min(fits):+.1f}..{max(fits):+.1f}")
        print(f"  winning lineage per run: {winners}")


def _anchor(client, settled, series, ec, qc, corners=False):
    pts = []
    for leg in settled.get(series, {}).get(ec, []):
        b, a = A._candle_quote(client, series, leg["ticker"], leg["ko"], qc)
        if b is not None and a is not None:
            pts.append((leg, (b + a) / 2))
    if not pts:
        return None
    if corners:
        if len(pts) < 3:
            return None
        mn = min(int(mv.parse_market_v2(l["ticker"], l["sub"])["threshold"] or 99) for l, _ in pts)
        return (mn - 1) + sum(p for _, p in pts)
    return sum(p for _, p in pts) or None


NEAR_CERTAIN_HI = 99    # cents: default near-certainty ceiling
NEAR_CERTAIN_LO = 1     # cents: don't bet at/below — treat as already resolved
GUARD_HI = {"game_lines": 88}   # tighter for game_lines: kills near-certain-over scalping


def _enter(teams, leg, parsed, pf, src, ask, ec, cat, in_play, minute):
    if pf is None or not ask:
        return
    # near-certainty guard: a leg priced near-resolved has no real edge and wouldn't
    # fill at size live. game_lines uses a tighter ceiling (the 90-98c over scalps).
    hi = GUARD_HI.get(cat, NEAR_CERTAIN_HI)
    if ask >= hi or ask <= NEAR_CERTAIN_LO:
        return
    if pf >= hi / 100.0 or pf <= 0.01:
        return
    edge = pf - ask / 100.0
    ctx = {"p_fair": pf, "ask": ask, "in_play": in_play, "minute": minute}
    for t in teams:
        tk = leg["ticker"]
        if tk in t["bets"] or len(t["bets"]) >= A.MAX_OPEN:
            continue
        acc = PaperAccount.from_dict(t["account"])
        bet = sv.Strategist(t["params"]).entry_size(ctx, acc.cash)
        if bet <= 0:
            continue
        n = kelly.to_contracts(bet, ask / 100.0)
        if not acc.buy(tk, n, ask / 100.0, {"sub": leg["sub"], "event": ec}):
            continue
        cost = n * ask / 100.0
        t["staked"] += cost
        t["bets"][tk] = {"sources": {k: v for k, v in src.items() if v is not None},
                         "p_fair": pf, "ask": ask, "edge": round(edge, 4),
                         "contracts": n, "cost": round(cost, 3), "event": ec, "flags": {}}
        t["account"] = acc.to_dict()


def _exit_step(teams, leg, live_fair, bid_c):
    tk = leg["ticker"]
    bid_d = (bid_c) if (bid_c and bid_c <= 1) else ((bid_c / 100.0) if bid_c else None)
    for t in teams:
        if tk not in t["bets"]:
            continue
        acc = PaperAccount.from_dict(t["account"])
        pos = acc.positions.get(tk)
        if not pos:
            continue
        flags = t["bets"][tk].setdefault("flags", {})
        carrier = {"entry": pos["entry"], **flags}
        action, frac = decide_exit(live_fair, bid_d, pos["entry"], carrier, t["params"])
        if action != "HOLD" and bid_d:
            n = max(1, int(pos["contracts"] * frac))
            if acc.sell(tk, n, bid_d):
                disarm(action, carrier)
            t["account"] = acc.to_dict()
            t["fitness"] = A._fitness(t)
            if tk not in acc.positions:
                t["bets"].pop(tk, None)
                t["n_closed"] += 1
            else:
                t["bets"][tk]["flags"] = {k: v for k, v in carrier.items() if k != "entry"}


def _report(teams_by_cat, cats):
    print("\n=== IN-PLAY SIM STANDINGS ===")
    for cat in cats:
        teams = sorted(teams_by_cat[cat], key=lambda t: t["fitness"], reverse=True)
        b = teams[0]
        print(f"\n{cat}: LEADER {b['lineage']} fit={b['fitness']:+.2f} "
              f"pnl=${b['account']['realized_pnl']:+.2f} closed={b['n_closed']}")
        for t in teams[:4]:
            print(f"  {t['lineage']:<16} fit={t['fitness']:+.2f} "
                  f"pnl=${t['account']['realized_pnl']:+.2f} n={t['n_closed']}")
        mk = A._per_market_pnl(teams)
        for s, (p, n, w) in sorted(mk.items(), key=lambda x: -x[1][0]):
            print(f"     {s:<14} ${p:+8.2f} ({n} bets, {100*w/n:.0f}% win)" if n else "")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--bootstrap":
        nn = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        bootstrap(n=nn)
    else:
        mg = int(sys.argv[1]) if len(sys.argv) > 1 else None
        simulate(max_games=mg)
