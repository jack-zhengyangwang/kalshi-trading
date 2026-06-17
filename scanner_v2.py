"""
scanner_v2.py — Arena v2 opportunity scanner (data-model path).

For each upcoming game it:
  1. pulls every leg of every configured series in a few batched calls
     (kalshi_client_v2.list_markets_by_series — throttled, 429-safe)
  2. recovers home/away from the KXWCGAME title
  3. ANCHORS the expected goal total to the market's own over/under ladder
     (E[total] = sum of P(over k.5) mids) and corners to the N+ ladder
  4. prices every leg with brain_v2 (data model) and ranks edge vs the live ask

No LLM, no orders — pure read + price + rank. This is the end-to-end proof that
the v2 client + markets_v2 + brain_v2 pipeline finds edges on the real book.
"""
import json
import os
import sys

from kalshi_client_v2 import KalshiClientV2
from brain_v2 import BrainV2
from group import live_feed as lf
import markets_v2 as mv

HERE = os.path.dirname(__file__)


def _cfg(super_cat):
    cfg = json.load(open(os.path.join(HERE, "categories_v2.json")))
    return cfg["super_categories"][super_cat]["series"]


def event_code(ticker):
    parts = ticker.split("-")
    return parts[1] if len(parts) > 1 else None


def home_away(title):
    t = (title or "").split(" Winner?")[0]
    if " vs " in t:
        h, a = t.split(" vs ", 1)
        return h.strip(), a.strip()
    return None, None


def _norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _same_team(a, b):
    na, nb = _norm(a), _norm(b)
    return bool(na) and bool(nb) and (na in nb or nb in na)


def mid(client, m):
    b, a = client.quote_cents(m)
    if b and a:
        return (b + a) / 200.0
    if a:
        return a / 100.0
    if b:
        return b / 100.0
    return None


def implied_total(client, legs, min_vol=0):
    """E[total goals] = sum over (liquid) KXWCTOTAL legs of P(over k.5) mid."""
    s, n = 0.0, 0
    for m in legs:
        if m["ticker"].split("-")[0] == "KXWCTOTAL":
            if client.liquidity(m)["volume"] < min_vol:
                continue
            p = mid(client, m)
            if p is not None:
                s += p
                n += 1
    return s if n else None


def implied_corner_mean(client, legs, min_vol=0):
    """E[corners] from the N+ ladder over LIQUID legs only: (min_N-1) + sum of
    P(N+) mids. Falls back to None (-> base corner total) if too few liquid points,
    since thin corner books gave unreliable anchors (e.g. 6.7 vs base 10)."""
    pts = []
    for m in legs:
        if m["ticker"].split("-")[0] == "KXWCCORNERS":
            if client.liquidity(m)["volume"] < min_vol:
                continue
            parsed = mv.parse_market_v2(m["ticker"], m.get("yes_sub_title"))
            p = mid(client, m)
            if parsed["threshold"] is not None and p is not None:
                pts.append((parsed["threshold"], p))
    if len(pts) < 3:
        return None
    min_n = min(t for t, _ in pts)
    return (min_n - 1) + sum(p for _, p in pts)


def leg_is_home(parsed, home, away):
    """Orient a leg to the home team. For score legs, 'home' means the WINNER is
    home (winner_text). Tie/None legs return True (unused by pricing)."""
    if parsed["type"] == "score":
        wt = (parsed["score"] or {}).get("winner_text", "")
        return _same_team(wt.replace(" wins", "").replace(" 1H", "").replace(" 2H", ""), home)
    team = parsed.get("team")
    if team is None:
        return True
    if _same_team(team, home):
        return True
    if _same_team(team, away):
        return False
    return True


# Always fetched for metadata/anchoring even if not in the scanned category:
# KXWCGAME -> home/away names; KXWCTOTAL -> goal-total anchor; KXWCCORNERS -> corner mean.
CONTEXT_SERIES = ["KXWCGAME", "KXWCTOTAL", "KXWCCORNERS"]


def price_games(super_cat, client, brain, max_events=3, min_volume=100,
                min_ask_size=5, live=False):
    """Price EVERY tradeable leg of the soonest `max_events` games in a category.
    Returns [{event, home, away, market_total, corner_mean, elo_known, in_play,
    legs:[...]}] where each leg = {ticker, sub, type, period, leg_is_home, parsed,
    p_fair, sources, bid, ask, vol, in_play, minute}. No edge filter.

    When live=True, games ESPN reports as in-progress are re-priced on the live
    score/minute/corners (full-period legs only); pre-game games price as usual."""
    series = _cfg(super_cat)
    fetch = list(dict.fromkeys(series + CONTEXT_SERIES))
    by_series = {s: client.list_markets_by_series(s) for s in fetch}
    sb_events = lf.scoreboard_events() if live else []

    # event -> home/away from the winner series
    events = {}
    for m in by_series.get("KXWCGAME", []):
        ec = event_code(m["ticker"])
        h, a = home_away(m.get("title"))
        if ec and h:
            events[ec] = {"home": h, "away": a}

    # legs to PRICE = only the scanned category's series; context legs used for anchor
    cat_set = set(series)
    legs_by_event = {}          # category legs to price
    ctx_by_event = {}           # all fetched legs, for anchoring
    for s, mks in by_series.items():
        for m in mks:
            ec = event_code(m["ticker"])
            ctx_by_event.setdefault(ec, []).append(m)
            if s in cat_set:
                legs_by_event.setdefault(ec, []).append(m)

    results = []
    for ec in sorted(e for e in legs_by_event if e in events)[:max_events]:
        info = events[ec]
        home, away = info["home"], info["away"]
        legs = legs_by_event[ec]
        ctx = ctx_by_event.get(ec, legs)
        mt = implied_total(client, ctx, min_vol=min_volume)
        mc = implied_corner_mean(client, ctx, min_vol=min_volume)
        prior = brain.game_prior(home, away, market_total=mt, market_corner_total=mc)
        # live state (in-play re-pricing)
        state = lf.state_from_events(sb_events, home, away) if live else None
        in_play = bool(state and state.get("status") == "in")
        lprior = corners_so_far = minute = None
        if in_play:
            minute = state.get("minute") or 0
            stats = lf.get_game_stats(home, away)
            corners_so_far = ((stats["home"]["corners"] + stats["away"]["corners"])
                              if stats else 0)
            lprior = brain.live_prior(home, away, minute, state["home_score"],
                                      state["away_score"], stats=stats, market_total=mt,
                                      market_corner_total=mc)
        # one LLM call per game (cached): pre-game priors, or a halftime re-estimate
        use_llm = getattr(brain, "use_llm", False)
        llm_data = brain.llm_for_game(home, away, ec, prior) if (use_llm and not in_play) else None
        llm_ht = (brain.llm_halftime(home, away, ec, lprior)
                  if (use_llm and in_play and (minute or 0) >= 45) else None)
        ev = {"event": ec, "home": home, "away": away, "in_play": in_play,
              "market_total": mt, "model_mu": round(prior["mu_home"] + prior["mu_away"], 2),
              "corner_mean": round(mc, 1) if mc else None,
              "elo_known": prior["elo_known"], "legs": []}
        for m in legs:
            parsed = mv.parse_market_v2(m["ticker"], m.get("yes_sub_title"))
            if parsed["type"] in ("unknown", "first_goalscorer"):
                continue
            liq = client.liquidity(m)
            if liq["volume"] < min_volume or liq["ask_size"] < min_ask_size:
                continue                       # drop thin/stale books (junk-edge guard)
            lh = leg_is_home(parsed, home, away)
            mkt = mid(client, m)
            if in_play:
                pdata = brain.live_pfair(parsed, lprior, lh, corners_so_far)
                if pdata is None:
                    continue                   # half-period / score legs: skip live
                sources = {"data": pdata}
                if mkt is not None:
                    sources["market"] = mkt
                if llm_ht:                     # halftime LLM re-estimate
                    lpf = brain.llm_live_pfair(parsed, lprior, lh, llm_ht, corners_so_far)
                    if lpf is not None:
                        sources["llm"] = lpf
                p = brain._stack(sources)
            else:
                lpf = (brain.llm_pfair(parsed, prior, lh, llm_data, ticker=m["ticker"])
                       if llm_data else None)
                p, sources = brain.pfair(parsed, prior, lh, market_mid=mkt, llm=lpf)
            if p is None:
                continue
            bid, ask = client.quote_cents(m)
            if not ask:
                continue
            ev["legs"].append({
                "ticker": m["ticker"], "sub": m.get("yes_sub_title"),
                "type": parsed["type"], "period": parsed["period"],
                "leg_is_home": lh, "parsed": parsed, "p_fair": round(p, 4),
                "sources": {k: round(v, 4) for k, v in sources.items() if v is not None},
                "bid": bid, "ask": ask, "vol": int(liq["volume"]),
                "in_play": in_play, "minute": minute})
        results.append(ev)
    return results


def scan(super_cat, max_events=3, min_edge=0.03, min_volume=100, min_ask_size=5):
    """CLI wrapper: price games then surface edges above threshold for display."""
    client = KalshiClientV2(req_per_sec=6)
    brain = BrainV2()
    out = []
    for ev in price_games(super_cat, client, brain, max_events, min_volume, min_ask_size):
        edges = []
        for lg in ev["legs"]:
            edge = lg["p_fair"] - lg["ask"] / 100.0
            if edge >= min_edge:
                edges.append({**lg, "edge": round(edge, 3),
                              "p_data": lg["sources"].get("data", 0.0)})
        edges.sort(key=lambda x: -x["edge"])
        out.append({**ev, "edges": edges})
    return out


if __name__ == "__main__":
    cat = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "game_lines"
    me = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--events=")), "3"))
    print(f"Scanning '{cat}' (data model only, no orders)...\n")
    for ev in scan(cat, max_events=me):
        print(f"=== {ev['home']} vs {ev['away']}  [{ev['event']}]  "
              f"mkt_total={ev['market_total']:.2f} model_total={ev['model_mu']} "
              f"corner_mean={ev['corner_mean']} elo={'Y' if ev['elo_known'] else 'N'}"
              if ev['market_total'] else
              f"=== {ev['home']} vs {ev['away']}  [{ev['event']}]  (no market total)")
        for e in ev["edges"][:10]:
            print(f"   +{e['edge']:.3f}  {e['type']:<12}{e['period']:<4} "
                  f"p={e['p_fair']:.3f}(data {e['p_data']:.3f}) ask={e['ask']}¢ "
                  f"vol={e['vol']}  {e['sub']}")
        if not ev["edges"]:
            print("   (no edges above threshold)")
        print()
