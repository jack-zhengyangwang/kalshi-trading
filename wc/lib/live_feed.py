"""
live_feed.py — live World Cup game state from ESPN's free scoreboard API.

No API key required. Used by the Keeper agent to drive in-play exit decisions.

    state = get_game_state("Czechia", "Mexico")
    # -> {"status": "in", "minute": 63, "home_score": 1, "away_score": 1,
    #     "home_team": "Czechia", "away_team": "Mexico"}  (None if not found)
"""
import re

import requests

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
)

_API_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"


def _scoreboard_url(league="fifa.world"):
    return f"{_API_BASE}/{league}/scoreboard"


def _summary_url(event_id, league="fifa.world"):
    return f"{_API_BASE}/{league}/summary?event={event_id}"

# Normalize the various spellings (ESPN / Kalshi / team_elo) to one token.
_ALIASES = {
    "unitedstates": "usa", "us": "usa",
    "czechrepublic": "czechia",
    "korearepublic": "southkorea", "republicofkorea": "southkorea",
    "bosniaherzegovina": "bosnia", "bosniaandherzegovina": "bosnia",
    "turkiye": "turkey",
    "irian": "iran", "iriran": "iran",
    "congodr": "drcongo", "drcongo": "drcongo",
}


def _canon(name):
    """Lowercase, strip non-alphanumerics, fold known aliases."""
    if not name:
        return ""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return _ALIASES.get(key, key)


def _names_match(a, b):
    ca, cb = _canon(a), _canon(b)
    if not ca or not cb:
        return False
    return ca == cb or ca in cb or cb in ca


def _parse_minute(status):
    """
    Best-effort current match minute as an int. Handles halftime (no numeric
    clock) and a seconds-based clock, so the in-play model's time-left and the
    Keeper's halftime hook behave correctly.
    """
    desc = (status.get("type", {}).get("description") or "").lower()
    detail = (status.get("type", {}).get("detail") or "").lower()
    if "halftime" in desc or "halftime" in detail or "half time" in desc:
        return 45

    disp = status.get("displayClock") or ""
    m = re.search(r"(\d+)", disp)
    if m:
        val = int(m.group(1))
        return val if val <= 130 else val // 60   # guard against seconds
    clock = status.get("clock")
    try:
        c = float(clock)
        return int(c // 60) if c > 130 else int(c)  # seconds → minutes
    except (TypeError, ValueError):
        return 0


def scoreboard_events(league="fifa.world", timeout=10):
    """Fetch ESPN's scoreboard ONCE and return the raw events list (it only lists
    near-term games — today/imminent — which is exactly the live+pregame window).
    Call this once per cycle and feed it to state_from_events to avoid one HTTP
    fetch per game."""
    try:
        r = requests.get(_scoreboard_url(league), timeout=timeout)
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception as e:
        print(f"  [LIVE WARN] scoreboard fetch failed: {e}", flush=True)
        return []


def state_from_events(events, home, away):
    """Match home-vs-away against pre-fetched scoreboard events (no network).
    Returns the state dict (status pre|in|post + minute + scores) or None."""
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue
        by_side = {}
        for c in competitors:
            by_side[c.get("homeAway")] = {
                "name": c.get("team", {}).get("displayName", ""),
                "score": int(c.get("score") or 0),
            }
        h, a = by_side.get("home"), by_side.get("away")
        if not h or not a:
            continue
        if _names_match(home, h["name"]) and _names_match(away, a["name"]):
            home_d, away_d = h, a
        elif _names_match(home, a["name"]) and _names_match(away, h["name"]):
            home_d, away_d = a, h   # ESPN listed them reversed
        else:
            continue
        state = (ev.get("status", {}).get("type", {}).get("state") or "pre").lower()
        return {
            "status": state, "minute": _parse_minute(ev.get("status", {})),
            "home_score": home_d["score"], "away_score": away_d["score"],
            "home_team": home, "away_team": away,
        }
    return None


def get_game_state(home, away, timeout=10, league="fifa.world"):
    """
    Return live state for the home-vs-away match, or None if not found.

    status: "pre" (not started) | "in" (live) | "post" (final).
    Team-name matching is fuzzy across ESPN/Kalshi/elo spellings.
    `league` selects the ESPN competition slug (default the FIFA World Cup).
    """
    return state_from_events(scoreboard_events(league, timeout), home, away)


def _find_event(home, away, timeout=10, league="fifa.world"):
    """
    Locate the scoreboard event for home-vs-away and return
    (event_id, espn_home_name, espn_away_name) oriented to the *requested*
    home/away, or None if not found. ESPN may list the teams in either order;
    we normalize that here (same fuzzy logic as get_game_state).
    """
    try:
        r = requests.get(_scoreboard_url(league), timeout=timeout)
        r.raise_for_status()
        events = r.json().get("events", [])
    except Exception as e:
        print(f"  [LIVE WARN] scoreboard fetch failed: {e}", flush=True)
        return None

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue

        by_side = {}
        for c in competitors:
            by_side[c.get("homeAway")] = c.get("team", {}).get("displayName", "")
        h, a = by_side.get("home"), by_side.get("away")
        if not h or not a:
            continue

        # Accept either ESPN ordering; return ESPN names oriented to request.
        if _names_match(home, h) and _names_match(away, a):
            return ev.get("id"), h, a
        if _names_match(home, a) and _names_match(away, h):
            return ev.get("id"), a, h  # ESPN listed them reversed

    return None


def _stat_value(stats_by_name, key, default):
    """Pull one ESPN statistic (string) and coerce, falling back to default."""
    raw = stats_by_name.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(str(raw).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def _empty_team_stats():
    return {"possession": 50.0, "shots": 0, "sot": 0, "corners": 0}


def get_game_stats(home, away, timeout=10, league="fifa.world"):
    """
    Return live box-score stats for the home-vs-away match, oriented to the
    requested home/away, or None if the match / stats can't be found.

        {"home": {"possession": float, "shots": int, "sot": int, "corners": int},
         "away": {"possession": float, "shots": int, "sot": int, "corners": int}}

    Stats are sparse early in a match; missing values default to 0 (and 50%
    possession). `league` selects the ESPN competition slug.
    """
    found = _find_event(home, away, timeout=timeout, league=league)
    if not found:
        return None
    event_id, espn_home, espn_away = found

    try:
        r = requests.get(_summary_url(event_id, league), timeout=timeout)
        r.raise_for_status()
        teams = (r.json().get("boxscore", {}) or {}).get("teams", []) or []
    except Exception as e:
        print(f"  [LIVE WARN] summary fetch failed: {e}", flush=True)
        return None

    if not teams:
        return None

    home_stats, away_stats = None, None
    for t in teams:
        name = (t.get("team", {}) or {}).get("displayName", "")
        stats_by_name = {
            s.get("name"): s.get("displayValue", s.get("value"))
            for s in (t.get("statistics") or [])
        }
        parsed = {
            "possession": _stat_value(stats_by_name, "possessionPct", 50.0),
            "shots": int(_stat_value(stats_by_name, "totalShots", 0)),
            "sot": int(_stat_value(stats_by_name, "shotsOnTarget", 0)),
            "corners": int(_stat_value(stats_by_name, "wonCorners", 0)),
        }
        # Orient by name match against the ESPN home/away names we resolved.
        if _names_match(name, espn_home):
            home_stats = parsed
        elif _names_match(name, espn_away):
            away_stats = parsed

    if home_stats is None and away_stats is None:
        return None

    return {
        "home": home_stats or _empty_team_stats(),
        "away": away_stats or _empty_team_stats(),
    }
