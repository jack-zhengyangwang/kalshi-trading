"""The firm's shared fact store.

    SHARE OBSERVATIONS. NEVER SHARE BELIEFS.

Facts live here — xG, recent form, head-to-head, bookmaker odds, lineups. They
are the same for every desk, because they are the same for everyone. What a PM
DOES with them (how much it trusts form over the table, how fast it decays
recency, whether a missing striker matters) is a belief, and belongs in that
PM's view. This file is the line between the two.

THE ONE THING THAT MATTERS: every fact carries `as_of`, the moment it became
KNOWN, and the store is queried as-of, never "current".

"Arsenal's last-5 form" is not a fact about Arsenal. It is a fact about Arsenal
ON A DATE. A store that answers "what is their form" rather than "what was known
on 2026-08-14" will hand a backtest results from matches that had not been
played yet — and unlike a bad price, that number looks entirely plausible. It is
the same leak as a present-day Elo snapshot, one level deeper and much harder to
see.

So: the table is APPEND-ONLY. A fact is never updated in place, because that
would destroy the record of what was known earlier. A revision is a new row with
a later `as_of`, and the old row stays.
"""
from __future__ import annotations

import os
import sqlite3
import time

from wc import paths

DB_PATH = os.path.join(paths.ROOT, "data", "facts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  entity   TEXT    NOT NULL,      -- team name, or an event ticker
  feature  TEXT    NOT NULL,      -- 'form_last5_ppg', 'h2h_win_rate', ...
  as_of    INTEGER NOT NULL,      -- when this became KNOWN, UTC epoch seconds
  value    REAL,                  -- numeric facts only; see `text_value`
  text_value TEXT,                -- for non-numeric facts (a headline, a name)
  source   TEXT    NOT NULL,      -- which fetcher produced it, for auditing
  PRIMARY KEY (entity, feature, as_of)
);

CREATE INDEX IF NOT EXISTS idx_facts_lookup ON facts(entity, feature, as_of);
CREATE INDEX IF NOT EXISTS idx_facts_asof   ON facts(as_of);
"""


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# ── writing ───────────────────────────────────────────────────────────────────

def put(con, entity, feature, value, as_of=None, source="unknown",
        text_value=None):
    """Record one fact as known at `as_of`.

    Append-only by design. Re-recording the same (entity, feature, as_of)
    replaces that row — which is correct, since it is the same observation — but
    a LATER observation is a new row, and the earlier one survives so a backtest
    can still ask what was known before the revision.
    """
    as_of = int(as_of if as_of is not None else time.time())
    con.execute(
        "INSERT OR REPLACE INTO facts "
        "(entity, feature, as_of, value, text_value, source) VALUES (?,?,?,?,?,?)",
        (entity, feature, as_of,
         None if value is None else float(value), text_value, source))
    return 1


def put_many(con, rows, source="unknown"):
    """rows: [{entity, feature, value, as_of, text_value?}]"""
    payload = [(r["entity"], r["feature"], int(r["as_of"]),
                None if r.get("value") is None else float(r["value"]),
                r.get("text_value"), r.get("source", source))
               for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO facts "
        "(entity, feature, as_of, value, text_value, source) VALUES (?,?,?,?,?,?)",
        payload)
    con.commit()
    return len(payload)


# ── reading, always as-of ─────────────────────────────────────────────────────

def get(con, entity, feature, at_ts):
    """The value of `feature` for `entity` AS KNOWN AT `at_ts`.

    Returns None when nothing was known yet — which a view must treat as "I
    cannot price this", not as zero. A fabricated zero is a belief the data did
    not support.
    """
    row = con.execute(
        "SELECT value FROM facts WHERE entity = ? AND feature = ? "
        "AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (entity, feature, int(at_ts))).fetchone()
    return None if row is None else row["value"]


def get_text(con, entity, feature, at_ts):
    row = con.execute(
        "SELECT text_value FROM facts WHERE entity = ? AND feature = ? "
        "AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (entity, feature, int(at_ts))).fetchone()
    return None if row is None else row["text_value"]


def snapshot(con, entity, at_ts, features=None):
    """{feature: value} for one entity as known at `at_ts`.

    One query rather than one per feature, because a view prices thousands of
    bars and a round-trip each would dominate the run.
    """
    q = ["""SELECT feature, value FROM facts f
            WHERE entity = ? AND as_of <= ?
              AND as_of = (SELECT MAX(as_of) FROM facts
                           WHERE entity = f.entity AND feature = f.feature
                             AND as_of <= ?)"""]
    args = [entity, int(at_ts), int(at_ts)]
    if features:
        q.append(f"AND feature IN ({','.join('?' * len(features))})")
        args += list(features)
    return {r["feature"]: r["value"] for r in con.execute(" ".join(q), args)}


def load_timeline(con, feature, entities=None):
    """[(entity, as_of, value)] for one feature, chronological.

    What a view uses to build its own as-of index in memory: one pass over the
    facts, then O(1) lookups per bar. Querying per bar would be correct but far
    too slow over a season.
    """
    q = ["SELECT entity, as_of, value FROM facts WHERE feature = ?"]
    args = [feature]
    if entities:
        q.append(f"AND entity IN ({','.join('?' * len(entities))})")
        args += list(entities)
    q.append("ORDER BY as_of ASC")
    return [(r["entity"], r["as_of"], r["value"])
            for r in con.execute(" ".join(q), args)]


class AsOfIndex:
    """In-memory as-of lookup for one feature, built once per backtest.

    Holds each entity's observations in time order and answers "what was known
    at t" by binary search. The class exists so a view CANNOT accidentally read
    a later value: there is no method that returns "the current value".
    """

    __slots__ = ("_by_entity",)

    def __init__(self, rows):
        self._by_entity = {}
        for entity, as_of, value in rows:
            self._by_entity.setdefault(entity, []).append((as_of, value))
        for series in self._by_entity.values():
            series.sort(key=lambda x: x[0])

    def at(self, entity, ts):
        import bisect
        series = self._by_entity.get(entity)
        if not series:
            return None
        i = bisect.bisect_right([s[0] for s in series], int(ts))
        return series[i - 1][1] if i else None

    def entities(self):
        return set(self._by_entity)

    def __len__(self):
        return sum(len(v) for v in self._by_entity.values())


def index(con, feature, entities=None):
    return AsOfIndex(load_timeline(con, feature, entities))


# ── introspection ─────────────────────────────────────────────────────────────

def coverage(con):
    """What the firm actually knows, and from where."""
    rows = con.execute(
        "SELECT feature, source, COUNT(*) n, COUNT(DISTINCT entity) entities, "
        "       MIN(as_of) lo, MAX(as_of) hi "
        "FROM facts GROUP BY feature, source ORDER BY n DESC").fetchall()
    return [dict(r) for r in rows]


def fact_count(con):
    return con.execute("SELECT COUNT(*) n FROM facts").fetchone()["n"]
