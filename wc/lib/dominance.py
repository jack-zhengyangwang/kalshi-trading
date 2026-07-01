"""
dominance.py — turn live box-score stats into Poisson scoring multipliers.

The in-play win-probability model (group/brain.py `_inplay_wp`) projects each
team's *remaining* goals as a Poisson rate. The score alone is blind to a team
that is camped in the opponent's half creating chances but hasn't converted yet.
`dominance_multipliers` reads the live stats (group/live_feed.get_game_stats)
and returns a pair of multipliers that scale each team's expected remaining
goals up (for the dominant side) or down (for the dominated side).

    home_mult, away_mult = dominance_multipliers(stats["home"], stats["away"])

Both multipliers are bounded to roughly [0.7, 1.4] and centered on ~1.0 for an
even game, so the adjustment nudges the model without overwhelming the score.

Integration is handled separately in the Brain — this module only does the math.
"""

# Relative weight each stat carries in the blended "dominance share".
# Shots on target is the strongest signal of genuine threat.
_WEIGHTS = {
    "sot": 0.45,         # shots on target — best chance-quality signal
    "shots": 0.25,       # total shots — territory / volume of chances
    "possession": 0.15,  # possession share — control, weaker than chances
    "corners": 0.15,     # won corners — sustained attacking pressure
}

# Multiplier band: an even game (share 0.5) maps to exactly 1.0; total
# dominance (share 1.0) -> 1.35 and total submission (share 0.0) -> 0.65.
_MULT_LO = 0.65
_MULT_SPAN = 0.70  # mult = _MULT_LO + _MULT_SPAN * share


def _share(home_val, away_val):
    """Home's share of a stat in [0,1]; 0.5 when both are zero/equal."""
    total = home_val + away_val
    if total <= 0:
        return 0.5
    return home_val / total


def dominance_multipliers(home_stats, away_stats):
    """
    Compute (home_mult, away_mult) scoring multipliers from live team stats.

    Args:
        home_stats, away_stats: per-team dicts as returned by
            live_feed.get_game_stats, each with keys
            possession (float %), shots (int), sot (int), corners (int).
            Either may be None / empty.

    Returns:
        (home_mult, away_mult): floats in ~[0.7, 1.4], centered on ~1.0.
        The team dominating shots-on-target / possession / corners gets the
        higher multiplier. Returns (1.0, 1.0) when stats are missing (no
        information => no adjustment).
    """
    if not home_stats or not away_stats:
        return (1.0, 1.0)

    # Per-stat home share in [0,1].
    shares = {
        "sot": _share(home_stats.get("sot", 0), away_stats.get("sot", 0)),
        "shots": _share(home_stats.get("shots", 0), away_stats.get("shots", 0)),
        "possession": _share(
            home_stats.get("possession", 50.0), away_stats.get("possession", 50.0)
        ),
        "corners": _share(
            home_stats.get("corners", 0), away_stats.get("corners", 0)
        ),
    }

    # Weighted blend -> a single home dominance share in [0,1] (0.5 = even).
    home_share = sum(_WEIGHTS[k] * shares[k] for k in _WEIGHTS)
    away_share = 1.0 - home_share  # shares are complementary by construction

    home_mult = _MULT_LO + _MULT_SPAN * home_share
    away_mult = _MULT_LO + _MULT_SPAN * away_share
    return (home_mult, away_mult)
