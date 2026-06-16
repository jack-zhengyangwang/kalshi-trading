"""
markets.py — fair values for game-adjacent Kalshi soccer markets (Phase 4).

The winner market is priced from an ELO→Poisson goal model (see brain._inplay_wp).
That SAME model yields every adjacent market analytically, so winner / spread /
total / team-total / BTTS are mutually consistent:

    home final goals ~ Poisson(mu_home),  away final goals ~ Poisson(mu_away)

with mu split by ELO supremacy. `goal_rates` mirrors the Brain's constants
exactly (_BASE_GOALS / _SUPREMACY_SCALE), so a spread fair value and the winner
fair value never disagree. All functions accept in-play tallies + residual rates
(pass mins_left<90 and *_so_far), so the same code re-prices live.

Pure math + a market parser — no I/O, no Brain/ELO dependency. The Brain
(`evaluate_market`) supplies the two ELOs and orients the leg to home/away.
"""
import math
import re

# Must match group/brain.py's in-play model constants.
_BASE_GOALS = 1.35
_SUPREMACY_SCALE = 600.0
_K = 12          # per-side goal truncation for the joint (home,away) grid
_KT = 30         # truncation for a single summed-Poisson tail (totals)


def goal_rates(elo_diff, mins_left=90, dom_home=1.0, dom_away=1.0,
               base_goals=_BASE_GOALS, total_goals=None):
    """(mu_home, mu_away) expected REMAINING goals over `mins_left`, split by an
    ELO supremacy and optionally scaled by live dominance multipliers.

    The EXPECTED TOTAL is the model's weakest assumption (a flat 2·base_goals is
    right on average but ignores matchup scoring), so `total_goals` lets the
    caller anchor it to the market's implied total for this game; the ELO
    supremacy then only splits that total into the two sides (which is the edge
    we actually have). When None, falls back to 2·base_goals.
    """
    frac = max(0.0, min(1.0, mins_left / 90.0))
    s = math.tanh(elo_diff / _SUPREMACY_SCALE)
    half = (total_goals / 2.0) if total_goals is not None else base_goals
    mu_home = max(1e-6, half * (1.0 + s) * frac * dom_home)
    mu_away = max(1e-6, half * (1.0 - s) * frac * dom_away)
    return mu_home, mu_away


def _pmf(mu, k):
    return [math.exp(-mu) * mu ** i / math.factorial(i) for i in range(k + 1)]


def total_over_prob(mu_home, mu_away, line, goals_so_far=0):
    """P(total match goals > line). `mu_*` are REMAINING rates; goals_so_far is
    the count already scored. Fractional lines (2.5) never push."""
    mu = mu_home + mu_away
    pm = _pmf(mu, _KT)
    thr = math.floor(line - goals_so_far) + 1   # remaining goals needed to clear
    if thr <= 0:
        return 1.0
    return sum(pm[k] for k in range(thr, _KT + 1))


def team_total_over_prob(mu_team, line, team_so_far=0):
    """P(one team's match goals > line)."""
    pm = _pmf(mu_team, _KT)
    thr = math.floor(line - team_so_far) + 1
    if thr <= 0:
        return 1.0
    return sum(pm[k] for k in range(thr, _KT + 1))


def spread_over_prob(mu_home, mu_away, line, leg_is_home=True, margin_so_far=0):
    """P(leg team wins by more than `line` goals). `line` like 1.5/2.5 (no push).
    margin_so_far = current (home_score - away_score)."""
    ph = _pmf(mu_home, _K)
    pa = _pmf(mu_away, _K)
    p = 0.0
    for gh in range(_K + 1):
        for ga in range(_K + 1):
            final_margin = margin_so_far + gh - ga       # home - away at full time
            diff = final_margin if leg_is_home else -final_margin
            if diff > line:
                p += ph[gh] * pa[ga]
    return p


def btts_prob(mu_home, mu_away, home_so_far=0, away_so_far=0):
    """P(both teams score at least once over the full match)."""
    ph1 = 1.0 if home_so_far >= 1 else (1.0 - math.exp(-mu_home))
    pa1 = 1.0 if away_so_far >= 1 else (1.0 - math.exp(-mu_away))
    return ph1 * pa1


def exact_score_prob(mu_home, mu_away, home_goals, away_goals,
                     home_so_far=0, away_so_far=0):
    """P(the FINAL score is exactly home_goals-away_goals), under independent
    Poisson scoring on the two sides — one cell of the same joint (home,away)
    grid `spread_over_prob` sums over. `mu_*` are REMAINING rates; `*_so_far`
    are goals already scored. Returns 0.0 if a side has already scored MORE than
    its target (that exact score is unreachable)."""
    need_h = home_goals - home_so_far
    need_a = away_goals - away_so_far
    if need_h < 0 or need_a < 0:
        return 0.0
    ph = math.exp(-mu_home) * mu_home ** need_h / math.factorial(need_h)
    pa = math.exp(-mu_away) * mu_away ** need_a / math.factorial(need_a)
    return ph * pa


# ── market identification ───────────────────────────────────────────────────

_OVER_RE = re.compile(r"over\s+(\d+(?:\.\d+)?)", re.I)
_WINS_BY_RE = re.compile(r"\s+wins?\s+by", re.I)
_OVER_SPLIT_RE = re.compile(r"\s+over\s+", re.I)


def parse_market(ticker, sub_title):
    """Classify an adjacent market from its ticker + yes_sub_title.

    Returns {"type", "line", "team"} where type ∈
    {spread, total, team_total, btts, unknown}. `team` is the full team name
    text for spread / team_total (the leg's team), else None.
    """
    t = (ticker or "").upper()
    sub = sub_title or ""
    m = _OVER_RE.search(sub)
    line = float(m.group(1)) if m else None

    # TEAMTOTAL contains the substring "TOTAL" — test it first.
    if "TEAMTOTAL" in t:
        team = _OVER_SPLIT_RE.split(sub, 1)[0].strip() if _OVER_SPLIT_RE.search(sub) else None
        return {"type": "team_total", "line": line, "team": team or None}
    if "SPREAD" in t:
        team = _WINS_BY_RE.split(sub, 1)[0].strip() if _WINS_BY_RE.search(sub) else None
        return {"type": "spread", "line": line, "team": team or None}
    if "BTTS" in t:
        return {"type": "btts", "line": None, "team": None}
    if "TOTAL" in t:
        return {"type": "total", "line": line, "team": None}
    return {"type": "unknown", "line": line, "team": None}


def fair_yes(parsed, mu_home, mu_away, leg_is_home=True,
             margin_so_far=0, home_so_far=0, away_so_far=0):
    """YES fair probability for a parsed adjacent market, given goal rates and
    (for live) the current tallies. Returns None for unpriceable markets."""
    typ, line = parsed["type"], parsed["line"]
    if typ == "total" and line is not None:
        return total_over_prob(mu_home, mu_away, line, home_so_far + away_so_far)
    if typ == "team_total" and line is not None:
        mu_team = mu_home if leg_is_home else mu_away
        team_so_far = home_so_far if leg_is_home else away_so_far
        return team_total_over_prob(mu_team, line, team_so_far)
    if typ == "spread" and line is not None:
        return spread_over_prob(mu_home, mu_away, line, leg_is_home, margin_so_far)
    if typ == "btts":
        return btts_prob(mu_home, mu_away, home_so_far, away_so_far)
    return None
