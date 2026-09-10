"""The merged match table: one row per real-world fixture, from every source.

    openfootball -+
    HF datalake --+--> resolve team identity --> merge --> matches
    Kalshi -------+

Columns are left NULL where a source does not carry them. openfootball has
half-time scores and nobody else does; the HF lake has shots, corners and odds
and nobody else does; Kalshi contributes the market linkage. Nothing is
invented to fill a gap.

THE FAILURE MODE THIS FILE EXISTS TO PREVENT. A failed join does not lose a
fixture — it DUPLICATES one. Two rows for the same match means derived.py walks
both: Elo updates twice for one result, form counts it twice, head-to-head
double-counts. Nothing errors and every number stays plausible.

So three guards run before the table is trusted, and like quality.py they
report rather than abort:

  1. DUPLICATES  — no two rows share (league, +/-1 day, both teams)
  2. CONFLICTS   — where two sources both have a score, they must agree
  3. UNRESOLVED  — names that failed to canonicalise, listed, not dropped

The +/-1 day window is not slack, it is necessary: a 20:00 UTC kickoff falls on
the next calendar day in some feeds, and strict date equality would fail to
join a meaningful slice of fixtures.
"""
from __future__ import annotations

import os
import sqlite3

from wc import paths
from wc.firm.sources import teams as team_mod

DB_PATH = os.path.join(paths.ROOT, "data", "soccer.db")
JOIN_WINDOW = 36 * 3600          # +/-1 day, generously

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  match_id   TEXT PRIMARY KEY,
  league     TEXT NOT NULL,
  season     TEXT,
  kickoff_ts INTEGER NOT NULL,
  home       TEXT NOT NULL,
  away       TEXT NOT NULL,
  home_key   TEXT NOT NULL,
  away_key   TEXT NOT NULL,
  home_goals INTEGER, away_goals INTEGER,
  ht_home_goals INTEGER, ht_away_goals INTEGER,
  shots_home REAL, shots_away REAL,
  shots_in_box_home REAL, shots_in_box_away REAL,
  shots_out_box_home REAL, shots_out_box_away REAL,
  corners_home REAL, corners_away REAL,
  odds_home REAL, odds_draw REAL, odds_away REAL,
  kalshi_event TEXT,
  known_at   INTEGER NOT NULL,
  sources    TEXT NOT NULL,
  conflict   TEXT
);
CREATE INDEX IF NOT EXISTS idx_matches_time   ON matches(kickoff_ts);
CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league, kickoff_ts);
CREATE INDEX IF NOT EXISTS idx_matches_home   ON matches(home_key);
"""

FILLABLE = (
    "season", "ht_home_goals", "ht_away_goals",
    "shots_home", "shots_away", "shots_in_box_home", "shots_in_box_away",
    "shots_out_box_home", "shots_out_box_away", "corners_home", "corners_away",
    "odds_home", "odds_draw", "odds_away", "kalshi_event",
)

# Who wins a score disagreement. openfootball is closer to source and actively
# maintained; the lake is an aggregation of aggregations.
SCORE_PRECEDENCE = ("openfootball", "hf_datalake", "kalshi")


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def build_registry(*row_sets):
    """One registry over every source, so resolution has the full name pool.

    Register openfootball first: its names are the fullest ("Manchester United
    FC"), which makes them the best canonical display.
    """
    reg = team_mod.TeamRegistry()
    for rows in row_sets:
        for r in rows:
            reg.add(r["league"], r["home_raw"])
            reg.add(r["league"], r["away_raw"])
    return reg


def _key(league, home_key, away_key, ts):
    return f"{league}|{int(ts // 86400)}|{home_key}|{away_key}"


def _rank(source):
    try:
        return SCORE_PRECEDENCE.index(source)
    except ValueError:
        return len(SCORE_PRECEDENCE)


class Merger:
    """Accumulates rows into one match per real fixture."""

    def __init__(self, registry):
        self.reg = registry
        self.rows = {}
        self._index = {}
        self.stats = {"merged": 0, "new": 0, "unresolved": 0, "conflicts": 0}
        self.conflicts = []

    def _find(self, league, hk, ak, ts):
        for cand_ts, mid in self._index.get((league, hk, ak), []):
            if abs(cand_ts - ts) <= JOIN_WINDOW:
                return mid
        return None

    def add(self, row):
        league = row["league"]
        hk = self.reg.resolve(league, row["home_raw"])
        ak = self.reg.resolve(league, row["away_raw"])
        if not hk or not ak or hk == ak:
            self.stats["unresolved"] += 1
            return None

        ts = int(row["kickoff_ts"])
        mid = self._find(league, hk, ak, ts)

        if mid is None:
            mid = _key(league, hk, ak, ts)
            rec = {
                "match_id": mid, "league": league, "kickoff_ts": ts,
                "home": self.reg.display(league, hk),
                "away": self.reg.display(league, ak),
                "home_key": hk, "away_key": ak,
                "home_goals": row.get("home_goals"),
                "away_goals": row.get("away_goals"),
                "_score_source": row.get("source"),
                # A result becomes knowable about two hours after kickoff.
                "known_at": ts + 2 * 3600,
                "sources": {row.get("source")},
                "conflict": None,
            }
            for col in FILLABLE:
                rec[col] = row.get(col)
            self.rows[mid] = rec
            self._index.setdefault((league, hk, ak), []).append((ts, mid))
            self.stats["new"] += 1
            return mid

        cur = self.rows[mid]
        cur["sources"].add(row.get("source"))
        self.stats["merged"] += 1

        # fill only what is missing — never overwrite a real value with None
        for col in FILLABLE:
            if cur.get(col) is None and row.get(col) is not None:
                cur[col] = row[col]

        new_score = (row.get("home_goals"), row.get("away_goals"))
        old_score = (cur.get("home_goals"), cur.get("away_goals"))
        if None not in new_score:
            if None in old_score:
                cur["home_goals"], cur["away_goals"] = new_score
                cur["_score_source"] = row.get("source")
            elif new_score != old_score:
                # Logged, never silently reconciled. A disagreement is a data
                # quality signal about the feeds, not noise to average away.
                self.stats["conflicts"] += 1
                note = (f"{cur['_score_source']}={old_score[0]}-{old_score[1]} "
                        f"vs {row.get('source')}={new_score[0]}-{new_score[1]}")
                cur["conflict"] = note
                self.conflicts.append({"match_id": mid, "league": league,
                                       "home": cur["home"], "away": cur["away"],
                                       "detail": note})
                if _rank(row.get("source")) < _rank(cur["_score_source"]):
                    cur["home_goals"], cur["away_goals"] = new_score
                    cur["_score_source"] = row.get("source")
        return mid

    def finish(self):
        out = []
        for r in self.rows.values():
            r = dict(r)
            r["sources"] = ",".join(sorted(s for s in r["sources"] if s))
            r.pop("_score_source", None)
            out.append(r)
        out.sort(key=lambda x: x["kickoff_ts"])
        return out


COLUMNS = ("match_id", "league", "season", "kickoff_ts", "home", "away",
           "home_key", "away_key", "home_goals", "away_goals",
           "ht_home_goals", "ht_away_goals", "shots_home", "shots_away",
           "shots_in_box_home", "shots_in_box_away", "shots_out_box_home",
           "shots_out_box_away", "corners_home", "corners_away",
           "odds_home", "odds_draw", "odds_away", "kalshi_event",
           "known_at", "sources", "conflict")


def write(con, rows):
    con.executemany(
        f"INSERT OR REPLACE INTO matches ({','.join(COLUMNS)}) "
        f"VALUES ({','.join('?' * len(COLUMNS))})",
        [tuple(r.get(c) for c in COLUMNS) for r in rows])
    con.commit()
    return len(rows)


# ── the guards ────────────────────────────────────────────────────────────────

def find_duplicates(con):
    """Rows that look like the same fixture within a day of each other.

    The guard that matters most: a duplicate is not a missing match, it is a
    match counted twice, absorbed silently by Elo, form and head-to-head.
    """
    rows = con.execute(
        "SELECT match_id, league, home_key, away_key, kickoff_ts, home, away "
        "FROM matches ORDER BY league, home_key, away_key, kickoff_ts").fetchall()
    dupes, prev = [], None
    for r in rows:
        if prev and (r["league"], r["home_key"], r["away_key"]) == \
                (prev["league"], prev["home_key"], prev["away_key"]) and \
                abs(r["kickoff_ts"] - prev["kickoff_ts"]) <= JOIN_WINDOW:
            dupes.append({"league": r["league"], "home": r["home"],
                          "away": r["away"],
                          "ids": [prev["match_id"], r["match_id"]]})
        prev = r
    return dupes


def report(con, registry=None, conflicts=None):
    """What the merged table holds, and what defeated the merge."""
    total = con.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]
    by_source = {r["sources"]: r["n"] for r in con.execute(
        "SELECT sources, COUNT(*) n FROM matches GROUP BY sources ORDER BY n DESC")}
    by_league = {r["league"]: r["n"] for r in con.execute(
        "SELECT league, COUNT(*) n FROM matches GROUP BY league ORDER BY n DESC")}
    span = con.execute("SELECT MIN(kickoff_ts) lo, MAX(kickoff_ts) hi "
                       "FROM matches").fetchone()

    coverage = {}
    for col in ("ht_home_goals", "shots_home", "odds_home", "corners_home",
                "kalshi_event"):
        n = con.execute(f"SELECT COUNT(*) n FROM matches "
                        f"WHERE {col} IS NOT NULL").fetchone()["n"]
        coverage[col] = {"n": n, "pct": round(n / total, 4) if total else 0.0}

    dupes = find_duplicates(con)
    out = {"matches": total, "by_source": by_source, "by_league": by_league,
           "span": [span["lo"], span["hi"]], "column_coverage": coverage,
           "duplicates": len(dupes), "duplicate_examples": dupes[:5],
           "conflicts": len(conflicts or []),
           "conflict_examples": (conflicts or [])[:5]}
    if registry is not None:
        out["teams"] = registry.report()
    out["blocking"] = _blocking(out)
    return out


def _blocking(r):
    """Problems that make this table untrustworthy, as opposed to incomplete.
    Incompleteness is expected; these are not."""
    out = []
    if r["duplicates"]:
        out.append(f"{r['duplicates']} duplicate fixtures — each one is a match "
                   f"counted TWICE by Elo, form and head-to-head")
    if not r["matches"]:
        out.append("no matches merged")
    unresolved = (r.get("teams") or {}).get("unresolved_count", 0)
    if r["matches"] and unresolved > 0.05 * r["matches"]:
        out.append(f"{unresolved} unresolved team names — the registry is "
                   f"failing often enough to be dropping real fixtures")
    return out
