"""
elo_index.py — Shared ELO resolution for v4 brains.

Extracted from brain_model.py's Brain class: loads national team + club ELO
ratings from models/, canonicalises team names, and fuzzy-matches Kalshi / ESPN
spellings to ELO keys. Stateless after init — safe to share one instance across
all per-league BrainV4 instances.
"""
import difflib
import json
import os
import re
import unicodedata

from wc import paths

TEAM_ELO_JSON = os.path.join(paths.MODELS_DIR, "team_elo.json")
CLUB_ELO_JSON = os.path.join(paths.MODELS_DIR, "club_elo.json")


class EloIndex:
    def __init__(self):
        self.team_elo = self._load_team_elo()
        self.team_elo.update(self._load_club_elo())
        self._elo_index = {self._canon(k): k for k in self.team_elo}
        self._resolve_cache = {}

    # ── Loading ─────────────────────────────────────────────────────────────

    def _load_team_elo(self):
        if os.path.exists(TEAM_ELO_JSON):
            try:
                with open(TEAM_ELO_JSON) as f:
                    return json.load(f)
            except Exception as e:
                print(f"  [ELO WARN] team_elo load failed: {e}", flush=True)
        else:
            print("  [ELO WARN] team_elo.json not found — run models/fetch_stats.py",
                  flush=True)
        return {}

    def _load_club_elo(self):
        if os.path.exists(CLUB_ELO_JSON):
            try:
                with open(CLUB_ELO_JSON) as f:
                    return json.load(f)
            except Exception as e:
                print(f"  [ELO WARN] club_elo load failed: {e}", flush=True)
        return {}

    # ── Name canonicalisation ───────────────────────────────────────────────

    @staticmethod
    def _canon(s):
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", s.lower())

    # Kalshi spelling -> team_elo.json key.
    _ALIASES = {
        "czechia": "Czech Republic",
        "turkiye": "Turkey",
        "korea republic": "South Korea",
        "republic of korea": "South Korea",
        "ir iran": "Iran",
        "usa": "USA",
        "united states": "USA",
    }

    # Canonicalised Kalshi/ESPN name -> canonicalised clubelo key.
    _CLUB_ALIASES = {
        "psg": "parissg", "parissaintgermain": "parissg",
        "intermilan": "inter", "internazionale": "inter",
        "manchesterunited": "manunited", "manunited": "manunited",
        "manchestercity": "mancity", "mancity": "mancity",
        "tottenhamhotspur": "tottenham", "spurs": "tottenham",
        "wolverhampton": "wolves", "atleticomadrid": "atletico",
        "hellasverona": "verona", "sportingcp": "sporting",
        "sportinglisbon": "sporting",
    }

    # ── Resolution ──────────────────────────────────────────────────────────

    def _resolve_team(self, name):
        """Match a parsed/sub-title team name to a key in team_elo."""
        if not name:
            return None
        if name in self._resolve_cache:
            return self._resolve_cache[name]
        low = name.strip().strip("?.,").lower()
        result = None
        if low in self._ALIASES:
            result = self._ALIASES[low]
        if result is None:
            c = self._canon(name)
            c = self._CLUB_ALIASES.get(c, c)
            if c in self._elo_index:                          # exact (canonical)
                result = self._elo_index[c]
        if result is None:
            for team in self.team_elo:                        # substring either way
                tl = self._canon(team)
                if tl and (tl in c or c in tl):
                    result = team
                    break
        if result is None:                                    # fuzzy last resort
            m = difflib.get_close_matches(c, list(self._elo_index), n=1, cutoff=0.86)
            if m:
                result = self._elo_index[m[0]]
        self._resolve_cache[name] = result
        return result

    # ── Public API ──────────────────────────────────────────────────────────

    def elo(self, team_name):
        """Return the ELO rating for `team_name`, or None if unresolvable."""
        key = self._resolve_team(team_name)
        return self.team_elo.get(key) if key else None

    def elo_diff(self, home, away):
        """Home ELO minus away ELO. Returns 0 if either team is unknown."""
        eh = self.elo(home)
        ea = self.elo(away)
        return (eh - ea) if (eh is not None and ea is not None) else 0.0


# ── process-wide singleton ──────────────────────────────────────────────────
# The index is read-only after init, so the six per-league BrainV4 instances
# should share one — otherwise each re-loads and re-parses 688 teams and keeps
# its own _resolve_cache.

_SHARED = None


def get_elo_index():
    """The shared EloIndex for this process. Constructed on first use."""
    global _SHARED
    if _SHARED is None:
        _SHARED = EloIndex()
    return _SHARED
