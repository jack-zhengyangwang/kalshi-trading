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
from wc import paths
import re
import sys

from wc.kalshi.client_ext import KalshiClientV2
from wc.brain import BrainV2
from wc.lib import live_feed as lf
import wc.markets as mv

HERE = os.path.dirname(__file__)


def _cfg(super_cat):
    cfg = json.load(open(paths.CATS_FILE))
    return cfg["super_categories"][super_cat]["series"]


_SERIES_CACHE = os.path.join(paths.STATE_V3, "kxwc_series_cache.json")
_KXWC_RE = re.compile(r"^KXWC")


def discover_kxwc_series(client, ttl=3600, refresh=False, now=None):
    """SCAN THE WILD: every open World Cup series Kalshi lists, found by the KXWC
    prefix — no hardcoded menu, no picker. New knockout/outright markets show up
    automatically the moment Kalshi opens them. Cached `ttl` seconds (the full
    Sports catalog is large) in arena_v3_state/kxwc_series_cache.json.

    `now` lets callers pass a timestamp (tests / replay); defaults to time.time()."""
    import time as _t
    now = _t.time() if now is None else now
    if not refresh:
        try:
            c = json.load(open(_SERIES_CACHE))
            if now - c.get("ts", 0) < ttl and c.get("series"):
                return c["series"]
        except Exception:
            pass
    found = []
    try:
        d = client._get("/series", {"category": "Sports"})
        for s in (d.get("series") or d.get("series_list") or []):
            t = s.get("ticker", "")
            if _KXWC_RE.match(t):
                found.append(t)
    except Exception as e:
        print("[discover] /series catalog failed:", e)
    found = sorted(set(found))
    try:
        os.makedirs(os.path.dirname(_SERIES_CACHE), exist_ok=True)
        json.dump({"ts": now, "series": found}, open(_SERIES_CACHE, "w"))
    except Exception:
        pass
    return found


def _all_curated_series():
    cfg = json.load(open(paths.CATS_FILE))["super_categories"]
    s = set()
    for v in cfg.values():
        s |= set(v.get("series", []))
    return s


def series_for(super_cat, client=None, refresh=False):
    """Series universe to PRICE for a super-category. Curated cats return their
    configured series (proven, validated). The 'discovered' bucket returns every
    open KXWC series NOT already claimed by a curated cat — the wild surface, found
    by regex (discover_kxwc_series), no hardcoded list. This is how new
    knockout/outright markets get scanned the moment Kalshi opens them."""
    if super_cat == "all":
        # the unified pool: the ENTIRE live KXWC surface (curated + everything else)
        return discover_kxwc_series(client, refresh=refresh) if client is not None else []
    if super_cat != "discovered":
        return _cfg(super_cat)
    if client is None:
        return []
    found = discover_kxwc_series(client, refresh=refresh)
    skip = _all_curated_series() | set(CONTEXT_SERIES)
    return [s for s in found if s not in skip]


_GAME_SERIES_CACHE = os.path.join(paths.STATE_V3, "kxwc_game_series_cache.json")
_GAMECODE_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}[A-Z]{4,}$")


def game_series(client, ttl=43200, refresh=False, now=None):
    """Cached list of GAME-LEVEL KXWC series — those with per-game events (winner,
    spread, total, corners, ADVANCE, goalscorer, ...). Lets the per-game scanner fetch
    only these (~24) instead of all ~110 series. Discovered by probing each discovered
    series for an event on the soonest game; cached `ttl` seconds (refresh occasionally
    so new market types are picked up). Falls back to the full discovered set if empty."""
    import time as _t
    now = _t.time() if now is None else now
    if not refresh:
        try:
            c = json.load(open(_GAME_SERIES_CACHE))
            if now - c.get("ts", 0) < ttl and c.get("series"):
                return c["series"]
        except Exception:
            pass
    allser = discover_kxwc_series(client)
    sample = None
    for m in client.list_markets_by_series("KXWCGAME"):
        ec = event_code(m["ticker"])
        if ec and _GAMECODE_RE.match(ec):
            sample = ec
            break
    game_lvl = []
    if sample:
        for s in allser:
            try:
                r = client._get("/markets", {"event_ticker": f"{s}-{sample}", "limit": 5})
                if r.get("markets"):
                    game_lvl.append(s)
            except Exception:
                pass
    game_lvl = sorted(set(game_lvl)) or allser        # fall back to all if probe found nothing
    try:
        os.makedirs(os.path.dirname(_GAME_SERIES_CACHE), exist_ok=True)
        json.dump({"ts": now, "series": game_lvl, "sample": sample},
                  open(_GAME_SERIES_CACHE, "w"))
    except Exception:
        pass
    return game_lvl


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


_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _event_when(ec):
    """Chronological key from an event code like '26JUN29BRAJPN' -> (yy, mm, dd).
    A plain string sort is WRONG (e.g. 'JUL' < 'JUN' lexically), which would push
    today's games behind later ones and out of the max_events window."""
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", ec or "")
    if not m:
        return (99, 99, 99)
    return (int(m.group(1)), _MON.get(m.group(2), 99), int(m.group(3)))


def price_games(super_cat, client, brain, max_events=3, min_volume=100,
                min_ask_size=5, live=False, series=None, only_games=None, llm_cap=30):
    """Price EVERY tradeable leg of the soonest `max_events` games in a category.
    Returns [{event, home, away, market_total, corner_mean, elo_known, in_play,
    legs:[...]}] where each leg = {ticker, sub, type, period, leg_is_home, parsed,
    p_fair, sources, bid, ask, vol, in_play, minute}. No edge filter.

    When live=True, games ESPN reports as in-progress are re-priced on the live
    score/minute/corners (full-period legs only); pre-game games price as usual.

    `series` overrides the series universe (e.g. the game-level set from game_series()
    for a per-game full-surface scan). `only_games` (set of event codes) restricts
    pricing to those games — used by the per-game dual-frequency scanner."""
    series = series if series is not None else series_for(super_cat, client)
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

    # Order events IN-PLAY first, then truly chronologically, BEFORE the max_events
    # cut, so live/soonest games are always priced (a string sort drops them — see
    # _event_when). state_from_events is cheap (sb_events already fetched once).
    def _order_key(ec):
        info = events[ec]
        st = lf.state_from_events(sb_events, info["home"], info["away"]) if live else None
        in_play = bool(st and st.get("status") == "in")
        return (not in_play, _event_when(ec), ec)

    results = []
    llm_n = 0                                   # per-scan LLM-call budget (see llm_cap)
    _cands = [e for e in legs_by_event if e in events
              and (only_games is None or e in only_games)]
    for ec in sorted(_cands, key=_order_key)[:max_events]:
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
        # CAUSAL in-play LLM at the current minute (score-throttled), not just at HT.
        llm_ht = (brain.llm_live(home, away, ec, minute or 0,
                                 state["home_score"], state["away_score"])
                  if (use_llm and in_play) else None)
        ev = {"event": ec, "home": home, "away": away, "in_play": in_play,
              "market_total": mt, "model_mu": round(prior["mu_home"] + prior["mu_away"], 2),
              "corner_mean": round(mc, 1) if mc else None,
              "elo_known": prior["elo_known"], "legs": []}
        for m in legs:
            parsed = mv.parse_market_v2(m["ticker"], m.get("yes_sub_title"))
            if parsed["type"] == "first_goalscorer":
                continue                       # no model + noisy book; skip
            unknown = parsed["type"] == "unknown"
            liq = client.liquidity(m)
            if liq["volume"] < min_volume or liq["ask_size"] < min_ask_size:
                continue                       # drop thin/stale books (junk-edge guard)
            lh = leg_is_home(parsed, home, away)
            mkt = mid(client, m)
            bid, ask = client.quote_cents(m)
            if not ask:
                continue
            if unknown:
                # No structural model (goalscorer / player props — advance/margin are
                # now priced structurally) -> LLM-only fallback, CAPPED at llm_cap per
                # scan so a firehose of exotic legs can't stall the cycle. Silently
                # skipped if the LLM is disabled for this brain.
                if llm_n >= llm_cap:
                    continue
                mm = dict(m); mm["yes_bid_cents"] = bid
                p, sources = brain.llm_price_market(mm, market_mid=mkt)
                if p is not None:
                    llm_n += 1
                src = "llm"
            elif in_play:
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
                src = "blend" if "llm" in sources else "data"
            else:
                lpf = (brain.llm_pfair(parsed, prior, lh, llm_data, ticker=m["ticker"])
                       if llm_data else None)
                p, sources = brain.pfair(parsed, prior, lh, market_mid=mkt, llm=lpf)
                src = ("blend" if ("data" in sources and "llm" in sources)
                       else ("data" if "data" in sources else "llm"))
            if p is None:
                continue
            ev["legs"].append({
                "ticker": m["ticker"], "sub": m.get("yes_sub_title"),
                "type": parsed["type"], "period": parsed["period"],
                "leg_is_home": lh, "parsed": parsed, "p_fair": round(p, 4),
                "sources": {k: round(v, 4) for k, v in sources.items() if v is not None},
                "source": src,
                "bid": bid, "ask": ask, "vol": int(liq["volume"]),
                "ask_size": int(liq.get("ask_size", 0)), "bid_size": int(liq.get("bid_size", 0)),
                "close_time": m.get("close_time"),
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
    if "--discover" in sys.argv:
        # SCAN THE WILD: list every open KXWC series + its open-market count.
        cl = KalshiClientV2(req_per_sec=4)
        ser = discover_kxwc_series(cl, refresh=True)
        known = set()
        try:
            cfg = json.load(open(paths.CATS_FILE))
            for sc in cfg["super_categories"].values():
                known |= set(sc.get("series", []))
        except Exception:
            pass
        print(f"discovered {len(ser)} open KXWC series (regex ^KXWC); "
              f"{len(known)} are in the old hardcoded menu\n")
        tot = 0
        for s in ser:
            n = len(cl.list_markets_by_series(s))
            tot += n
            tag = "" if s in known else "  <-- NEW (was invisible)"
            if n:
                print(f"  {s:<24} {n:4d} open{tag}")
        print(f"\nTOTAL open markets across the wild: {tot}")
        sys.exit(0)
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
