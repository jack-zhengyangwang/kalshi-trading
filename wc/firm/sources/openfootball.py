"""openfootball/football.json -> normalised match rows.

Free, no API key, plain JSON on raw.githubusercontent. Covers the five majors
plus Championship, Eredivisie and Primeira Liga, current to within a couple of
days, with complete prior seasons.

What it uniquely carries: HALF-TIME scores, which no other source here has.

What it lacks: the Americas. MLS, Liga MX, Brasileirao and the Argentine
divisions are absent, and those dominate the Kalshi surface — which is why the
HF data lake exists alongside it rather than instead of it.
"""
from __future__ import annotations

import datetime as dt

import requests

BASE = "https://raw.githubusercontent.com/openfootball/football.json/master"
TIMEOUT = 30


def seasons(back=4, today=None):
    """Recent season codes, newest first: '2026-27', '2025-26', ..."""
    today = today or dt.date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return [f"{y}-{str(y + 1)[2:]}" for y in range(start, start - back, -1)]


def fetch(league_code, season):
    r = requests.get(f"{BASE}/{season}/{league_code}.json", timeout=TIMEOUT)
    if r.status_code == 404:
        return None                       # league did not run that season
    r.raise_for_status()
    return r.json()


def _score(score):
    """(full_time, half_time) as [home, away] pairs, or (None, None)."""
    if isinstance(score, dict):
        ft = score.get("ft")
        ht = score.get("ht") or [None, None]
    elif isinstance(score, (list, tuple)):
        ft, ht = list(score), [None, None]
    else:
        return None, None
    if not isinstance(ft, (list, tuple)) or len(ft) != 2:
        return None, None
    try:
        ft = [int(ft[0]), int(ft[1])]
    except (TypeError, ValueError):
        return None, None
    if not isinstance(ht, (list, tuple)) or len(ht) != 2:
        ht = [None, None]
    return ft, ht


def _kickoff(date_str, time_str):
    """UTC epoch seconds. openfootball dates are local and often lack a time;
    noon is a deliberate mid-day anchor so a +/-1 day join window still lands
    on the right fixture regardless of which side of midnight it fell."""
    try:
        d = dt.date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        return None
    hh, mm = 12, 0
    if time_str:
        try:
            hh, mm = (int(x) for x in str(time_str).split(":")[:2])
        except (ValueError, TypeError):
            pass
    return int(dt.datetime(d.year, d.month, d.day, hh, mm,
                           tzinfo=dt.timezone.utc).timestamp())


def matches(league, league_code, season):
    """[{...}] normalised rows for one league-season, played matches only."""
    payload = fetch(league_code, season)
    if not payload:
        return []

    out = []
    for m in (payload.get("matches") or []):
        # `score` is a dict {ft, ht} in most files and a bare [home, away]
        # list in some. Assuming one shape crashes the whole season over a
        # handful of rows.
        ft, ht = _score(m.get("score"))
        if ft is None:
            continue                      # fixture not played yet
        ts = _kickoff(m.get("date"), m.get("time"))
        if ts is None:
            continue
        out.append({
            "league": league,
            "season": season,
            "kickoff_ts": ts,
            "home_raw": m.get("team1"),
            "away_raw": m.get("team2"),
            "home_goals": int(ft[0]),
            "away_goals": int(ft[1]),
            "ht_home_goals": ht[0] if isinstance(ht[0], int) else None,
            "ht_away_goals": ht[1] if isinstance(ht[1], int) else None,
            "source": "openfootball",
        })
    return out


def collect(leagues, back=4, on_progress=None):
    """Every played match across the mapped leagues and recent seasons."""
    rows = []
    for name, cfg in leagues.items():
        code = cfg.get("openfootball")
        if not code:
            continue                      # not carried by this source
        for season in seasons(back):
            try:
                got = matches(name, code, season)
            except Exception as e:
                if on_progress:
                    on_progress(f"  {name} {season}: {str(e)[:60]}")
                continue
            rows += got
            if got and on_progress:
                on_progress(f"  {name:<14} {season}  {len(got):>4} matches")
    return rows
