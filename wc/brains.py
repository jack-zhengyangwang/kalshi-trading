"""
brains.py — lifecycle for the v4 per-league brain set.

v3 kept ONE brain per betting category (SEED_CATS == ["all"]), so the unit of
learning was the category. v4 prices per league: scanner.price_games() routes
each game to brains[league] via league_from_game_series(), so the unit of
learning is now the LEAGUE.

Everything that builds, persists or trains that dict goes through here, so
cycle.py / arena.py / promote.py cannot drift apart on state-file naming or on
how a resolved bet is attributed back to a brain.

State layout (in paths.STATE_V3):
    brain_<league>.json       versioned stacker weights
    llm_cache_<league>.json   per-league LLM cache

v3 wrote brain_all.json / llm_cache_all.json. load() migrates from those on
first run so the season's learned weights are not thrown away.
"""
import json
import os

from wc import paths
from wc.brain_v4 import BrainV4, LEAGUES
from wc.scanner import league_from_game_series

# leagues.json carries a "_doc" note key alongside the real leagues. Never build
# a brain for it — its value is a string, so calibration lookups would blow up.
LEAGUE_KEYS = [k for k in LEAGUES if not k.startswith("_")]

LEGACY_CAT = "all"          # the single v3 category these brains replace


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def weights_path(league, state_dir=None):
    return os.path.join(state_dir or paths.STATE_V3, f"brain_{league}.json")


def cache_path(league, state_dir=None):
    return os.path.join(state_dir or paths.STATE_V3, f"llm_cache_{league}.json")


def league_for_ticker(ticker):
    """Attribute a settled bet back to the brain that priced it.

    Kalshi tickers are SERIES-EVENT-OUTCOME, so the series ticker is the prefix
    and league_from_game_series() maps it the same way the scanner did on the
    way in. Anything unrecognised lands in 'Other', which is also where the
    scanner sent it — so a bet can never train a brain that did not price it.
    """
    if not ticker:
        return "Other"
    return league_from_game_series(str(ticker).split("-")[0])


def load(use_llm=True, state_dir=None):
    """Build the full {league: BrainV4} set, restoring weights and LLM cache.

    Migrates from the v3 brain_all.json / llm_cache_all.json when no per-league
    file exists yet, so the first v4 cycle starts warm instead of at defaults.
    """
    state_dir = state_dir or paths.STATE_V3
    legacy_w = _read(weights_path(LEGACY_CAT, state_dir), None)
    legacy_c = _read(cache_path(LEGACY_CAT, state_dir), None)
    migrated = []
    brains = {}
    for lg in LEAGUE_KEYS:
        w = _read(weights_path(lg, state_dir), None)
        c = _read(cache_path(lg, state_dir), None)
        if w is None and legacy_w is not None:
            w = legacy_w                      # bare v3 dict; _read_weights accepts it
            migrated.append(lg)
        if c is None and legacy_c is not None:
            c = legacy_c
        conf = {"use_llm": use_llm, "llm_cache": c or {}}
        if w is not None:
            conf["stacker_weights"] = w
        brains[lg] = BrainV4(lg, conf)
    if migrated:
        print(f"  [brains] migrated v3 '{LEGACY_CAT}' weights -> {len(migrated)} league brains")
    return brains


def new(use_llm=False):
    """A fresh brain set at default weights, touching no state files.

    The replay harness starts cold on purpose so a backtest is not seeded by
    weights the live loop happened to have learned.
    """
    return {lg: BrainV4(lg, {"use_llm": use_llm}) for lg in LEAGUE_KEYS}


def save(brains, state_dir=None):
    """Persist versioned weights + LLM cache for every league brain."""
    state_dir = state_dir or paths.STATE_V3
    for lg, br in brains.items():
        _write(weights_path(lg, state_dir), br.weights_payload())
        _write(cache_path(lg, state_dir), br.llm_cache)


def set_llm(brains, on):
    """Force the LLM leg on/off across the set (real-money passes price with it)."""
    for br in brains.values():
        br.use_llm = bool(on)
        if on and br.weights.get("llm", 0) <= 0:
            br.weights["llm"] = 0.2
    return brains


def train(brains, rows, min_sample=15):
    """Re-tune stacker weights from resolved bets, grouped by league.

    `rows` = [{ticker, sources, outcome}, ...]. Returns {league: new_weights}
    for the brains that actually moved (update_stacker no-ops below min_sample),
    so callers can log exactly what changed.
    """
    by_league = {}
    for r in rows:
        if r.get("outcome") is None or not r.get("sources"):
            continue
        by_league.setdefault(league_for_ticker(r.get("ticker")), []).append(
            {"sources": r["sources"], "outcome": r["outcome"]})
    changed = {}
    for lg, resolved in by_league.items():
        br = brains.get(lg)
        if br is None:
            continue
        old = dict(br.weights)
        updated = br.update_stacker(resolved, min_sample=min_sample)
        if updated != old:
            changed[lg] = dict(updated)
    return changed
