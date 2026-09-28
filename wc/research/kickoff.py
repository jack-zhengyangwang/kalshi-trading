"""Kickoff time for every Kalshi soccer game: ESPN is the truth, Kalshi the check.

    python -m wc.research.kickoff [--db data/market_history.db]

WHY BOTH. The research question is how prices move between time t and
kickoff, so kickoff has to be right to the minute. Kalshi's market payload has
no kickoff field: `occurrence_datetime` is the game's expected END (for
Göteborg–Halmstad on 2026-09-12 it says 18:30Z; ESPN says kickoff was 15:30Z).
So ESPN's scoreboard is the source of truth, and Kalshi's timestamp — shifted
by the median offset between the two — is the cross-check. Every row says
which case it is:

    agree        both found, Kalshi minus ESPN within 30 min of the usual offset
    disagree     both found, they differ — ESPN kept, row flagged for a look
    espn_only    ESPN found it, Kalshi returned nothing
    kalshi_only  ESPN had no match; kickoff = Kalshi time minus the offset
    missing      neither

Raw responses are cached under data/espn_cache/ and data/kalshi_cache/ so a
rebuild is reproducible and costs no API calls.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import statistics
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests

from wc import paths

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/events/{}"
DATA = os.path.join(paths.ROOT, "data")
ESPN_CACHE = os.path.join(DATA, "espn_cache")
KALSHI_CACHE = os.path.join(DATA, "kalshi_cache", "events")
TOLERANCE_S = 30 * 60
DEFAULT_OFFSET_S = 3 * 3600

# Words that name the KIND of club, not the club. Dropped before matching so
# "IFK Göteborg" matches "Goteborg". "United"/"City" are deliberately NOT here:
# they are what tells Manchester City from Manchester United.
GENERIC = {"fc", "cf", "sc", "ac", "afc", "bk", "sk", "if", "ifk", "fk", "cd", "ca",
           "club", "de", "del", "la", "el", "the", "sv", "ss", "as", "us", "cs", "nk"}

_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# ── matching ──────────────────────────────────────────────────────────────────

def _tokens(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t and t not in GENERIC]


def _tok_eq(a, b):
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short)


def names_match(a, b):
    """True when every significant word of the shorter name is found in the
    longer one ("Halmstad" ~ "Halmstads BK"; "Leeds United" !~ "Newcastle United")."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    short, long_ = sorted((ta, tb), key=len)
    return all(any(_tok_eq(s, t) for t in long_) for s in short)


def _side(event, home_away):
    for c in (event.get("competitions") or [{}])[0].get("competitors") or []:
        if c.get("homeAway") == home_away:
            team = c.get("team") or {}
            return [n for n in (team.get("displayName"), team.get("shortDisplayName"),
                                team.get("name")) if n]
    return []


def _any_match(name, candidates):
    return any(names_match(name, c) for c in candidates)


def parse_ts(iso):
    try:
        return int(dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def find_kickoff(home, away, events):
    """The one ESPN event with both teams, or None if there are none or several.
    Straight (home=home) matches win over swapped ones."""
    straight, swapped = {}, {}
    for e in events:
        eh, ea = _side(e, "home"), _side(e, "away")
        if _any_match(home, eh) and _any_match(away, ea):
            straight[e["id"]] = e
        elif _any_match(home, ea) and _any_match(away, eh):
            swapped[e["id"]] = e
    hits, was_swapped = (straight, False) if straight else (swapped, True)
    if len(hits) != 1:
        return None
    e = next(iter(hits.values()))
    return {"espn_id": e["id"], "kickoff_ts": parse_ts(e.get("date")),
            "espn_home": (_side(e, "home") or [None])[0],
            "espn_away": (_side(e, "away") or [None])[0], "swapped": was_swapped}


def ticker_date(event_ticker):
    """'KX...-26SEP12IFKHAL' -> '2026-09-12' (the scheduled date in the ticker)."""
    m = _TICKER_DATE.search(event_ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    return f"20{m.group(1)}-{_MONTHS[m.group(2)]:02d}-{m.group(3)}"


def reconcile(espn_ts, kalshi_ts, offset, tol=TOLERANCE_S):
    """Combine the two sources. ESPN wins whenever it has an answer."""
    if espn_ts is not None and kalshi_ts is not None:
        diff = kalshi_ts - espn_ts
        return {"kickoff_ts": espn_ts, "kalshi_minus_espn_s": diff,
                "status": "agree" if abs(diff - offset) <= tol else "disagree"}
    if espn_ts is not None:
        return {"kickoff_ts": espn_ts, "kalshi_minus_espn_s": None, "status": "espn_only"}
    if kalshi_ts is not None:
        return {"kickoff_ts": kalshi_ts - offset, "kalshi_minus_espn_s": None,
                "status": "kalshi_only"}
    return {"kickoff_ts": None, "kalshi_minus_espn_s": None, "status": "missing"}


# ── fetching (cached) ─────────────────────────────────────────────────────────

def _cached_get(path, url, params=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    for attempt in range(8):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 429:
            break
        time.sleep(2 ** attempt * 0.5)          # rate-limited: back off and retry
    if r.status_code == 404:
        data = {}
    else:
        r.raise_for_status()
        data = r.json()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def espn_day(date):
    """Every soccer game ESPN lists on a UTC date ('2026-09-12')."""
    ymd = date.replace("-", "")
    data = _cached_get(os.path.join(ESPN_CACHE, f"{ymd}.json"), ESPN,
                       {"dates": ymd, "limit": 1000})
    return data.get("events") or []


def kalshi_occurrence(event_ticker):
    data = _cached_get(os.path.join(KALSHI_CACHE, f"{event_ticker}.json"),
                       KALSHI.format(event_ticker))
    for m in data.get("markets") or []:
        ts = parse_ts(m.get("occurrence_datetime"))
        if ts:
            return ts
    return None


# ── build ─────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS kickoffs (
  event_ticker         TEXT PRIMARY KEY,
  kickoff_ts           INTEGER,     -- UTC epoch seconds; NULL when status='missing'
  status               TEXT NOT NULL,
  espn_ts              INTEGER,
  espn_id              TEXT,
  espn_home            TEXT,
  espn_away            TEXT,
  swapped              INTEGER,     -- 1 if ESPN lists the teams the other way round
  kalshi_occurrence_ts INTEGER,
  kalshi_minus_espn_s  INTEGER,
  built_at             INTEGER NOT NULL
)"""


def _days_around(date):
    d = dt.date.fromisoformat(date)
    return [(d + dt.timedelta(days=k)).isoformat() for k in (-1, 0, 1)]


def build(con, limit=None, workers=2):
    events = con.execute(
        "SELECT event_ticker, MIN(home), MIN(away) FROM markets "
        "WHERE series LIKE '%GAME' AND home IS NOT NULL AND away IS NOT NULL "
        "GROUP BY event_ticker ORDER BY event_ticker").fetchall()
    if limit:
        events = events[:limit]

    espn_hits = {}
    for ev, home, away in events:
        date = ticker_date(ev)
        if not date:
            continue
        day_events = [e for d in _days_around(date) for e in espn_day(d)]
        espn_hits[ev] = find_kickoff(home, away, day_events)

    with ThreadPoolExecutor(workers) as pool:
        kalshi = dict(zip([e[0] for e in events],
                          pool.map(kalshi_occurrence, [e[0] for e in events])))

    diffs = [kalshi[ev] - h["kickoff_ts"] for ev, h in espn_hits.items()
             if h and h["kickoff_ts"] and kalshi.get(ev)]
    offset = int(statistics.median(diffs)) if diffs else DEFAULT_OFFSET_S

    con.execute(SCHEMA)
    now = int(time.time())
    rows = []
    for ev, _, _ in events:
        h = espn_hits.get(ev) or {}
        r = reconcile(h.get("kickoff_ts"), kalshi.get(ev), offset)
        rows.append((ev, r["kickoff_ts"], r["status"], h.get("kickoff_ts"), h.get("espn_id"),
                     h.get("espn_home"), h.get("espn_away"),
                     int(h["swapped"]) if h else None, kalshi.get(ev),
                     r["kalshi_minus_espn_s"], now))
    con.executemany("INSERT OR REPLACE INTO kickoffs VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return offset, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.path.join(DATA, "market_history.db"))
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    offset, rows = build(con, a.limit)
    counts = {}
    for r in rows:
        counts[r[2]] = counts.get(r[2], 0) + 1
    print(f"{len(rows)} games; Kalshi occurrence = ESPN kickoff + {offset / 3600:.2f}h (median)")
    for k in ("agree", "disagree", "espn_only", "kalshi_only", "missing"):
        print(f"  {k:12} {counts.get(k, 0)}")


if __name__ == "__main__":
    main()
