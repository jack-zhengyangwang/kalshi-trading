"""Canonical team identity across sources.

Three feeds spell the same club three ways:

    Kalshi        "Manchester United"      (parsed out of the rules prose)
    openfootball  "Manchester United FC"
    HF datalake   "Manchester Utd"
    ESPN          "Man United"

Resolving those to one identity is the load-bearing part of the merge, and the
failure mode is worse than it sounds.

    A FAILED MATCH DOES NOT LOSE A FIXTURE — IT DUPLICATES ONE.

Two rows for the same real match means `derived.py` walks both: Elo updates
twice for one result, form counts it twice, head-to-head double-counts. Nothing
errors, every number stays plausible, and the knowledge base is quietly wrong.
So this module is deliberately conservative — it would rather report a name it
could not resolve than guess and merge two different clubs.

Resolution is ALWAYS scoped to a league. Without that, "Arsenal" (England) and
"Arsenal de Sarandí" (Argentina) collapse into one club, as do the several
"Serie A"s.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata

from wc import paths

LEAGUES_MAP = os.path.join(paths.CONFIG_DIR, "leagues_map.json")

# Club-name furniture that carries no identity. Order matters: longer tokens
# first, so "AFC" is not left behind as "FC".
_AFFIXES = [
    "afc", "fc", "cf", "sc", "ac", "cd", "ca", "sv", "bsc", "vfb", "vfl",
    "tsg", "fsv", "sd", "ud", "rcd", "cfr", "if", "bk", "aik", "sk", "us",
    "ss", "as", "ssc", "nk", "hnk", "fk", "csd", "de", "futbol",
    "football", "calcio", "esporte", "clube",
]
# NOT furniture, however much it looks like it: "atletico" distinguishes
# Atletico Madrid from Real Madrid and Atletico Mineiro from every other
# Mineiro. Stripping it collapsed both into "madrid" — two different clubs
# sharing one identity, which is precisely the duplicate that poisons Elo.
# "club" is out for the same reason (Club Brugge, Club America).
_AFFIX_RE = re.compile(r"\b(" + "|".join(sorted(_AFFIXES, key=len, reverse=True)) + r")\b")

# Cases normalisation cannot reach: genuinely different words for one club.
ALIASES = {
    "manutd": "manchesterunited", "manutd": "manchesterunited",
    "manunited": "manchesterunited", "mancity": "manchestercity",
    "spurs": "tottenham", "tottenhamhotspur": "tottenham",
    "wolves": "wolverhampton", "wolverhamptonwanderers": "wolverhampton",
    "brighton": "brightonhovealbion", "newcastle": "newcastleunited",
    "westham": "westhamunited", "leeds": "leedsunited",
    "nottmforest": "nottinghamforest", "sheffutd": "sheffieldunited",
    "psg": "parissaintgermain", "parissg": "parissaintgermain",
    "inter": "internazionale", "intermilan": "internazionale",
    "acmilan": "milan", "bayern": "bayernmunich", "bayernmunchen": "bayernmunich",
    "dortmund": "borussiadortmund", "gladbach": "borussiamonchengladbach",
    "leverkusen": "bayerleverkusen", "atleti": "atleticomadrid",
    "barca": "barcelona", "realsociedad": "sociedad",
    "nyrb": "newyorkredbulls", "redbullnewyork": "newyorkredbulls",
    "nycfc": "newyorkcity", "lafc": "losangelesfc",
    "laglaxy": "lagalaxy", "losangelesgalaxy": "lagalaxy",
}


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def normalise(name):
    """A club name reduced to its identifying core.

    Drops accents, punctuation, and the FC/AC/SV furniture that differs between
    feeds for the same club. Aggressive on purpose — the alternative is a
    registry where 'Bayern Munich' and 'FC Bayern München' are two clubs.
    """
    s = strip_accents(str(name or "")).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _AFFIX_RE.sub(" ", s)
    s = re.sub(r"\s+", "", s)
    return ALIASES.get(s, s)


def load_leagues(path=LEAGUES_MAP):
    with open(path) as f:
        cfg = json.load(f)
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


class TeamRegistry:
    """League-scoped canonical identity, with everything it could not resolve
    kept for inspection rather than discarded."""

    def __init__(self, cutoff=0.90):
        self._canon = {}            # (league, normalised) -> display name
        self._norms = {}            # league -> [normalised]
        self.unresolved = []        # [(league, raw)] — reported, never silent
        self.cutoff = cutoff

    def add(self, league, name):
        """Register a name, returning its canonical key."""
        n = normalise(name)
        if not n:
            return None
        key = (league, n)
        if key not in self._canon:
            self._canon[key] = str(name).strip()
            self._norms.setdefault(league, []).append(n)
        return n

    def resolve(self, league, name, record_miss=True):
        """The canonical key for `name` within `league`, or None.

        Exact normalised match first, then a high-cutoff fuzzy match INSIDE the
        same league. Never across leagues: that is how two different Arsenals
        become one club.
        """
        n = normalise(name)
        if not n:
            return None
        if (league, n) in self._canon:
            return n
        pool = self._norms.get(league) or []
        close = difflib.get_close_matches(n, pool, n=1, cutoff=self.cutoff)
        if close:
            return close[0]
        # substring fallback, but only when unambiguous
        hits = [p for p in pool if p and (p in n or n in p)]
        if len(hits) == 1:
            return hits[0]
        if record_miss:
            self.unresolved.append((league, str(name)))
        return None

    def display(self, league, key):
        return self._canon.get((league, key), key)

    def teams(self, league=None):
        if league is None:
            return dict(self._canon)
        return {k[1]: v for k, v in self._canon.items() if k[0] == league}

    def report(self):
        """What the registry knows, and what defeated it."""
        by_league = {}
        for (lg, _n) in self._canon:
            by_league[lg] = by_league.get(lg, 0) + 1
        misses = {}
        for lg, raw in self.unresolved:
            misses.setdefault(lg, set()).add(raw)
        return {
            "leagues": len(by_league),
            "teams": len(self._canon),
            "per_league": by_league,
            "unresolved": {k: sorted(v) for k, v in misses.items()},
            "unresolved_count": len(self.unresolved),
        }
