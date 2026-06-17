#!/usr/bin/env python3
"""
backtest.py — replay RESOLVED soccer games (any Kalshi 3-way "Winner?" series)
through the paper-tournament strategies, using historical data:
  • Kalshi candlesticks  → minute-by-minute YES bid/ask price path per leg
  • ESPN keyEvents       → goal timeline → score at each minute
  • final settle         → market.result ($1/$0)

Generalized over competitions:
    python3 backtest.py                                   # World Cup (default)
    python3 backtest.py --series KXUCLGAME --league uefa.champions
    python3 backtest.py --series KXEPLGAME --league eng.1

Games are auto-discovered from the settled markets in the series. LLM is ON but
throttled (re-fires only on a score change). NOTE: the trained ELO model + the
in-play Poisson model only fire when team ELOs are known (national teams); for
club competitions without club ELO they go silent and the system runs on
LLM + market price only (a degraded, LLM-vs-market test).
"""
import argparse
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.dirname(BASE_DIR))   # repo root: shared group/, kalshi_client
from run import _load_env_from_zshenv  # noqa: E402  (run.py lives alongside in v1/)
_load_env_from_zshenv()
from kalshi_client import KalshiClient        # noqa: E402
from group.brain import Brain                 # noqa: E402
from group.paper import PaperAccount          # noqa: E402
from group.strategy import build_strategy     # noqa: E402
from group.live_feed import _ALIASES as _NAT_ALIAS  # noqa: E402  (share one alias map)
import json                                    # noqa: E402

_VS_RE = re.compile(
    r"([A-Z][A-Za-z .'-]+?)\s+(?:vs\.?|v\.?)\s+([A-Z][A-Za-z .'-]+?)(?:\s+Winner)?\??$",
    re.IGNORECASE)
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
ALIAS = {"psg": "paris", "bayern": "bayern", "atletico": "atletico",
         "sporting": "sporting", "manutd": "manchesterunited", "spurs": "tottenham"}
_LLM_CACHE = {}


def _canon(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    key = re.sub(r"[^a-z0-9]", "", s.lower())
    # Fold national-team spelling variants (Korea Republic↔South Korea,
    # Bosnia and Herzegovina↔Bosnia-Herzegovina, United States↔USA, …) so the
    # ESPN timeline matches the Kalshi names. Shares live_feed's alias map.
    return _NAT_ALIAS.get(key, key)


def _teams_match(kalshi_name, espn_name):
    a, b = _canon(kalshi_name), _canon(espn_name)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ak = ALIAS.get(a, a)
    return ak in b or b in ak


def _epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def discover_games(client, series):
    """All settled games in a series, with their legs (one query, no per-game refetch)."""
    d = client._get("/markets", params={"series_ticker": series, "status": "settled",
                                        "limit": 1000}, auth=False)
    by_event = {}
    for m in d.get("markets", []):
        prefix = m["ticker"].rsplit("-", 1)[0]
        res = (m.get("result") or "").lower()
        leg = {"ticker": m["ticker"], "sub": m.get("yes_sub_title", ""),
               "result": 1 if res == "yes" else (0 if res == "no" else None)}
        by_event.setdefault(prefix, {"title": m.get("title", ""), "legs": []})["legs"].append(leg)
    games = []
    for prefix, info in by_event.items():
        mm = _VS_RE.search(info["title"])
        tag = prefix.split("-")[1] if "-" in prefix else ""
        dm = re.match(r"(\d{2})([A-Z]{3})(\d{2})", tag)
        if not mm or not dm:
            continue
        yyyymmdd = f"20{dm.group(1)}{MONTHS[dm.group(2)]:02d}{int(dm.group(3)):02d}"
        games.append({"prefix": prefix, "home": mm.group(1).strip(),
                      "away": mm.group(2).strip(), "yyyymmdd": yyyymmdd, "legs": info["legs"]})
    return sorted(games, key=lambda g: g["yyyymmdd"])


def find_espn(league, yyyymmdd, home, away):
    """Find the ESPN event id + kickoff iso for a game (tries the day and ±1)."""
    base = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
    d0 = datetime.strptime(yyyymmdd, "%Y%m%d")
    for delta in (0, -1, 1):
        day = (d0 + timedelta(days=delta)).strftime("%Y%m%d")
        try:
            evs = requests.get(base, params={"dates": day}, timeout=12).json().get("events", [])
        except Exception:
            continue
        for e in evs:
            names = [x["team"]["displayName"] for x in e["competitions"][0]["competitors"]]
            if (any(_teams_match(home, n) for n in names) and
                    any(_teams_match(away, n) for n in names)):
                return e["id"], e.get("date")
    return None, None


def get_legs(client, series, prefix):
    legs = []
    for st in ("settled", "closed"):
        try:
            d = client._get("/markets", params={"series_ticker": series,
                            "status": st, "limit": 1000}, auth=False)
        except Exception:
            d = {}
        for m in d.get("markets", []):
            if m["ticker"].rsplit("-", 1)[0] == prefix and not any(l["ticker"] == m["ticker"] for l in legs):
                res = (m.get("result") or "").lower()
                legs.append({"ticker": m["ticker"], "sub": m.get("yes_sub_title", ""),
                             "result": 1 if res == "yes" else (0 if res == "no" else None)})
    return legs


def get_goals(league, espn_id):
    try:
        ke = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/summary",
                          params={"event": espn_id}, timeout=15).json().get("keyEvents", [])
    except Exception:
        return []
    goals = []
    for e in ke:
        if "Goal" not in (e.get("type", {}).get("text") or "") or "Disallowed" in str(e.get("type", {})):
            continue
        mm = re.search(r"(\d+)", e.get("clock", {}).get("displayValue") or "")
        minute = int(mm.group(1)) if mm else 0
        hs, as_ = e.get("homeScore"), e.get("awayScore")
        if hs is None or as_ is None:
            nums = re.findall(r"(\d+)", e.get("text", ""))
            if len(nums) >= 2:
                hs, as_ = nums[0], nums[1]
        try:
            goals.append((minute, int(hs), int(as_)))
        except (TypeError, ValueError):
            continue
    return sorted(goals)


def score_at(goals, minute):
    h = a = 0
    for m, hs, as_ in goals:
        if m <= minute:
            h, a = hs, as_
    return h, a


def get_price_path(client, series, ticker, ko):
    path = f"/series/{series}/markets/{ticker}/candlesticks"
    try:
        r = client.session.get(client.base_url + path,
                               headers=client._auth_headers("GET", path),
                               params={"start_ts": ko - 1800, "end_ts": ko + 130 * 60,
                                       "period_interval": 1}, timeout=20)
        candles = r.json().get("candlesticks", []) if r.ok else []
    except Exception:
        candles = []
    out = {}
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        minute = int((ts - ko) / 60)
        bid = (c.get("yes_bid") or {}).get("close_dollars")
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        out[minute] = (float(bid) if bid not in (None, "") else None,
                       float(ask) if ask not in (None, "") else None)
    return out


def _price_at(path, minute):
    best = None
    for m in sorted(path):
        if m <= minute:
            best = path[m]
        else:
            break
    return best or (None, None)


def replay_game(client, brain, series, league, game, teams):
    legs = game["legs"]
    espn_id, ko_iso = find_espn(league, game["yyyymmdd"], game["home"], game["away"])
    goals = get_goals(league, espn_id) if espn_id else []
    ko = _epoch(ko_iso) if ko_iso else _epoch(game["yyyymmdd"][:4] + "-" +
            game["yyyymmdd"][4:6] + "-" + game["yyyymmdd"][6:] + "T19:00:00Z")
    _LLM_CACHE.clear()
    paths = {}
    for l in legs:
        paths[l["ticker"]] = get_price_path(client, series, l["ticker"], ko)
        time.sleep(0.2)   # space Kalshi calls (avoid 429)
    fh, fa = score_at(goals, 130)
    flag = "" if espn_id else "  [no ESPN match — pre-game only]"
    print(f"\n── {game['home']} {fh}-{fa} {game['away']}  ({game['yyyymmdd']}, "
          f"{len(goals)} goals){flag}")

    def mkt_at(leg, minute):
        bid, ask = _price_at(paths[leg["ticker"]], minute)
        return ({"ticker": leg["ticker"], "title": f"{game['home']} vs {game['away']} Winner?",
                 "yes_sub_title": leg["sub"],
                 "yes_bid_cents": round(bid * 100) if bid else None,
                 "yes_ask_cents": round(ask * 100) if ask else None,
                 "volume_yes": 0, "volume_no": 0, "category": "sports"}, bid, ask)

    last_minute = 95 if espn_id else 1   # no ESPN → only the pre-game step
    for minute in range(0, last_minute + 1):
        h, a = score_at(goals, minute)
        gs = {"status": "pre" if minute == 0 else "in", "minute": minute,
              "home_score": h, "away_score": a,
              "home_team": game["home"], "away_team": game["away"]}
        for leg in legs:
            mkt, bid, ask = mkt_at(leg, minute)
            pregame = brain.evaluate(mkt) if minute == 0 else None
            live = None
            if minute > 0:
                prev = _LLM_CACHE.get(leg["ticker"], (None, None))
                live = brain.evaluate_live(mkt, gs, prev_score=prev[0], prev_llm=prev[1])
                _LLM_CACHE[leg["ticker"]] = (live["score_key"], live["llm_for_cache"])
            for team in teams:
                acct, strat = team["account"], team["strategy"]
                tk = leg["ticker"]
                ctx = {"market": mkt, "pregame": pregame, "live": live, "ask": ask,
                       "bid": bid, "balance": acct.cash, "tau_days": 0.05, "game_state": gs}
                if tk not in acct.positions:
                    dollars = strat.entry_dollars(ctx)
                    if dollars > 0 and ask:
                        acct.buy(tk, max(1, int(dollars / ask)), ask,
                                 {"sub": leg["sub"], "game": game["prefix"]})
                elif minute > 0 and bid:
                    action, frac = strat.exit_decision(acct.positions[tk], ctx)
                    if action != "HOLD" and frac > 0:
                        pos = acct.positions[tk]
                        want = max(1, min(int(round(pos["contracts"] * frac)), pos["contracts"]))
                        acct.sell(tk, want, bid)
                        if action == "OVERPRICED":
                            pos["_op_armed"] = False
                        elif action == "TAKE_PROFIT":
                            pos["_tp_done"] = True

    res = {l["ticker"]: l["result"] for l in legs}
    for team in teams:
        for tk in list(team["account"].positions):
            if res.get(tk) is not None:
                team["account"].settle(tk, res[tk])


LEAGUES = [   # well-covered by clubelo (MLS dropped — clubelo lacks US clubs)
    ("KXEPLGAME", "eng.1"), ("KXLALIGAGAME", "esp.1"), ("KXSERIEAGAME", "ita.1"),
    ("KXUELGAME", "uefa.europa"), ("KXUCLGAME", "uefa.champions"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXWCGAME")
    ap.add_argument("--league", default="fifa.world")
    ap.add_argument("--all", action="store_true", help="run all club leagues into one tournament")
    args = ap.parse_args()

    config = json.load(open(os.path.join(BASE_DIR, "group", "config.json")))
    tcfg = json.load(open(os.path.join(BASE_DIR, "tournament_config.json")))
    client = KalshiClient()
    brain = Brain(config)
    # Pre-game LLM stays ON (the edge source for A/B); live LLM OFF for feasibility
    # across many games — the ELO/Poisson in-play model is now active (club ELO),
    # so C/D keep their live signal.
    brain._live_llm_estimate = lambda m, gs: (None, "")

    teams = [{"name": t["name"], "strategy": build_strategy(t["kind"], t["name"], t["params"]),
              "account": PaperAccount(t["name"], tcfg.get("starting_cash", 200.0))}
             for t in tcfg["teams"]]

    pairs = LEAGUES if args.all else [(args.series, args.league)]
    for series, league in pairs:
        games = discover_games(client, series)
        print(f"\n##### {series} ({league}): {len(games)} resolved games #####")
        before = {t["name"]: t["account"].realized_pnl for t in teams}
        for g in games:
            replay_game(client, brain, series, league, g, teams)
            time.sleep(0.25)
        print(f"  -- {series} per-strategy P&L --")
        for t in teams:
            d = t["account"].realized_pnl - before[t["name"]]
            print(f"     {t['name']:<24} {d:+.2f}")

    print("\n=== COMBINED LEADERBOARD (all leagues) ===")
    print(f"{'team':<24}{'realized':>10}{'trades':>8}{'settled':>9}{'cash':>9}")
    for team in sorted(teams, key=lambda t: t["account"].realized_pnl, reverse=True):
        a = team["account"]
        print(f"{a.name:<24}{a.realized_pnl:>+9.2f} {a.trades:>7}{a.settled:>9}{a.cash:>9.2f}")


if __name__ == "__main__":
    main()
