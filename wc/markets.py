"""
markets_v2.py — Arena v2 pricing layer for the full Kalshi WC soccer surface.

Builds on group/markets.py (goals Poisson primitives) and adds everything the v2
super-categories need that the original didn't cover:

  • a SERIES-PREFIX parser for every live WC series (authoritative — the segment
    before the first '-' of a ticker uniquely identifies type + period)
  • 3-way result probabilities (home / tie / away) for the WINNER and 1st-HALF
    WINNER legs, summed from the same independent-Poisson goal grid
  • first-half scaling of goal rates (1H legs reuse the goals primitives on
    half-match mu)
  • Negative-Binomial TOTAL CORNERS pricing from models/corners_model.json
    (corner totals are over-dispersed; lines are "N+" = P(total >= N))

All functions are pure math on goal/corner rates; brain_v2 supplies mu_home/mu_away
(from ELO + market-anchored totals) and orients each leg.

Live WC series (probed 2026-06-16):
  KXWCGAME winner | KXWCSPREAD margin | KXWCTOTAL goals | KXWCTEAMTOTAL team goals
  KXWCBTTS | KXWCSCORE correct-score | KXWCCORNERS total corners (N+)
  KXWC1H 1H-winner | KXWC1HTOTAL | KXWC1HSPREAD | KXWC1HBTTS
  KXWCFIRSTGOAL first-goalscorer (PLAYER PROP — not priced here)
"""
import json
import math
import os
from wc import paths
import re

from wc.lib import market_data as m

HERE = os.path.dirname(__file__)
CORNERS_MODEL = paths.CORNERS_MODEL

# Share of full-match goals scored in the 1st half (2nd halves run slightly higher).
# Empirical ~0.45-0.46 across major leagues; refine in Phase 2 from data.
HALF_GOAL_FRACTION = 0.46

# series prefix -> (leg type, period). Period "1H"/"2H" => that half only.
SERIES_SPEC = {
    "KXWCGAME":      ("winner", "full"),
    "KXWCSPREAD":    ("spread", "full"),
    "KXWCTOTAL":     ("total", "full"),
    "KXWCTEAMTOTAL": ("team_total", "full"),
    "KXWCBTTS":      ("btts", "full"),
    "KXWCSCORE":     ("score", "full"),
    "KXWCCORNERS":   ("corners", "full"),
    "KXWCTCORNERS":  ("team_corners", "full"),
    "KXWCFTTS":      ("first_to_score", "full"),
    "KXWCTTSF":      ("first_to_score", "full"),   # alt ticker (currently dormant)
    "KXWC1H":        ("winner", "1H"),
    "KXWC1HTOTAL":   ("total", "1H"),
    "KXWC1HSPREAD":  ("spread", "1H"),
    "KXWC1HBTTS":    ("btts", "1H"),
    "KXWC1HSCORE":   ("score", "1H"),
    "KXWC2H":        ("winner", "2H"),
    "KXWC2HTOTAL":   ("total", "2H"),
    "KXWC2HSPREAD":  ("spread", "2H"),
    "KXWC2HBTTS":    ("btts", "2H"),
    "KXWCFIRSTGOAL": ("first_goalscorer", "full"),
    "KXWCADVANCE":   ("advance", "full"),        # "USA advances" (knockout progression)
    "KXWCMOV":       ("win_method", "full"),     # "USA to win in Regulation/ET/Penalties"
    "KXWCMOF":       ("decide_phase", "full"),   # "Either team advances in Reg/ET/Pen"
}

_OVER_RE = re.compile(r"over\s+(\d+(?:\.\d+)?)", re.I)          # "over 2.5 goals"
_ATLEAST_RE = re.compile(r"(\d+)\+\s*corners", re.I)           # "9+ corners" (total)
_TEAMCORNER_RE = re.compile(r"^(.*?):\s*(\d+)\+", re.I)        # "South Africa: 8+"
_WINS_BY_RE = re.compile(r"\s+wins?\s+(?:the\s+[12]H\s+)?by", re.I)
_TEAM_OVER_RE = re.compile(r"\s+over\s+", re.I)
# spread sub-titles say "wins by more than N.5" (not "over N.5"), so the generic
# _OVER_RE misses the line — extract it here so spread legs actually get priced.
_SPREAD_LINE_RE = re.compile(r"by\s+more\s+than\s+(\d+(?:\.\d+)?)", re.I)
_SPREAD_ORMORE_RE = re.compile(r"by\s+(\d+)\s+or\s+more", re.I)   # "by 2 or more" -> 1.5
_SCORE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")                    # "5-1"

# ── parsing ──────────────────────────────────────────────────────────────────


def parse_market_v2(ticker, sub_title):
    """Classify any WC leg from its ticker series prefix + yes_sub_title.

    Returns {type, period, line, threshold, team, score} where:
      type      ∈ {winner, spread, total, team_total, btts, score, corners,
                   first_goalscorer, unknown}
      period    ∈ {full, 1H}
      line      goals line (e.g. 2.5) for over-style legs, else None
      threshold integer N for corners "N+" (P(total >= N)), else None
      team      leg's team text for spread/team_total/winner, else None
      score     (home_goals, away_goals) for correct-score, else None
    """
    series = (ticker or "").split("-")[0].upper()
    sub = sub_title or ""
    typ, period = SERIES_SPEC.get(series, ("unknown", "full"))

    line = None
    mo = _OVER_RE.search(sub)
    if mo:
        line = float(mo.group(1))

    threshold = None
    if typ == "corners":
        ma = _ATLEAST_RE.search(sub)
        threshold = int(ma.group(1)) if ma else None

    team = None
    if typ == "spread":
        team = _WINS_BY_RE.split(sub, 1)[0].strip() if _WINS_BY_RE.search(sub) else None
        if line is None:                              # pull the spread line from the sub
            msp = _SPREAD_LINE_RE.search(sub)
            if msp:
                line = float(msp.group(1))
            else:
                mor = _SPREAD_ORMORE_RE.search(sub)
                if mor:
                    line = int(mor.group(1)) - 0.5
    elif typ == "team_total":
        team = _TEAM_OVER_RE.split(sub, 1)[0].strip() if _TEAM_OVER_RE.search(sub) else None
    elif typ == "winner":
        team = None if sub.strip().lower() in ("tie", "draw") else sub.strip()
    elif typ == "team_corners":
        mc = _TEAMCORNER_RE.search(sub)
        if mc:
            team, threshold = mc.group(1).strip(), int(mc.group(2))
    elif typ == "first_to_score":
        team = None if sub.strip().lower() in ("no goal", "none", "no") else sub.strip()
    elif typ == "advance":                              # "USA advances"
        team = re.sub(r"\s+advances?\s*$", "", sub, flags=re.I).strip() or None
    elif typ == "win_method":                           # "USA to win in Regulation Time"
        team = sub.split(" to win")[0].strip() or None

    phase = None                                        # reg / et / pen for win_method + decide_phase
    if typ in ("win_method", "decide_phase"):
        s = sub.lower()
        phase = ("reg" if "regulation" in s else "et" if "extra" in s
                 else "pen" if ("penalt" in s or "shootout" in s) else None)

    score = None
    if typ == "score":
        ms = _SCORE_RE.search(sub)
        if ms:
            a, b = int(ms.group(1)), int(ms.group(2))
            # "X wins a-b" lists the winner's goals first; orient to home/away later
            # in the brain. Here keep raw (higher, lower) tagged by winner text.
            score = {"a": a, "b": b, "winner_text": _SCORE_RE.split(sub, 1)[0].strip()}
    return {"type": typ, "period": period, "line": line,
            "threshold": threshold, "team": team, "score": score, "phase": phase}


# ── goal-rate helpers ────────────────────────────────────────────────────────


def half_rates(mu_home, mu_away):
    """Scale full-match expected goals to the 1st half."""
    return mu_home * HALF_GOAL_FRACTION, mu_away * HALF_GOAL_FRACTION


def period_rates(mu_home, mu_away, period):
    """Scale full-match goal rates to the requested period (full/1H/2H)."""
    if period == "1H":
        f = HALF_GOAL_FRACTION
    elif period == "2H":
        f = 1.0 - HALF_GOAL_FRACTION
    else:
        return mu_home, mu_away
    return mu_home * f, mu_away * f


def first_to_score_probs(mu_home, mu_away):
    """(p_home_first, p_away_first, p_no_goal) over the full match, treating each
    side's goals as an independent Poisson process. The first scorer among the two
    processes is home w.p. mu_home/(mu_home+mu_away); no goal w.p. e^-(total)."""
    tot = mu_home + mu_away
    if tot <= 0:
        return 0.0, 0.0, 1.0
    none = math.exp(-tot)
    return (1 - none) * mu_home / tot, (1 - none) * mu_away / tot, none


def result_probs(mu_home, mu_away, margin_so_far=0):
    """(p_home_win, p_tie, p_away_win) from independent Poisson goal counts.
    Works for full match or 1H (pass half rates). margin_so_far for live use."""
    ph = m._pmf(mu_home, m._K)
    pa = m._pmf(mu_away, m._K)
    p_h = p_t = p_a = 0.0
    for gh in range(m._K + 1):
        for ga in range(m._K + 1):
            d = margin_so_far + gh - ga
            w = ph[gh] * pa[ga]
            if d > 0:
                p_h += w
            elif d == 0:
                p_t += w
            else:
                p_a += w
    return p_h, p_t, p_a


# ── corners (Negative-Binomial) ──────────────────────────────────────────────

_CM = None


def _corner_model():
    global _CM
    if _CM is None:
        with open(CORNERS_MODEL) as f:
            _CM = json.load(f)
    return _CM


def corner_atleast_prob(mean_total, threshold, corners_so_far=0):
    """P(total match corners >= threshold) under a Negative-Binomial whose
    over-dispersion (var/mean) is taken from the corpus, mean anchored to
    `mean_total` (market-anchored at inference). `threshold` is the "N+" line."""
    cm = _corner_model()
    ratio = cm["dispersion"]["ratio"]
    mean = max(1e-6, mean_total)
    # var = ratio*mean ; r = mean^2/(var-mean) = mean/(ratio-1)
    if ratio <= 1.0:                       # not over-dispersed -> Poisson
        return _poisson_atleast(mean, threshold, corners_so_far)
    r = mean / (ratio - 1.0)
    p = r / (r + mean)
    need = max(0, int(threshold) - int(corners_so_far))
    if need <= 0:
        return 1.0
    # P(X >= need) = 1 - P(X <= need-1)
    cdf = 0.0
    term = p ** r
    for k in range(need):
        if k > 0:
            term *= (k + r - 1) / k * (1 - p)
        cdf += term
    return max(0.0, 1.0 - cdf)


def _poisson_atleast(mean, threshold, so_far=0):
    need = max(0, int(threshold) - int(so_far))
    if need <= 0:
        return 1.0
    cdf, term = 0.0, math.exp(-mean)
    for k in range(need):
        if k > 0:
            term *= mean / k
        cdf += term
    return max(0.0, 1.0 - cdf)


def base_corner_total():
    """Corpus base expected total corners (fallback when no market anchor)."""
    return _corner_model()["base_corners"]["total"]


def team_corner_share(leg_is_home=True):
    """Home/away share of total corners from the corpus base split (~0.55 home).
    Corner share barely responds to ELO (corpus R^2=0.05), so the home/away split
    is the dominant signal; team strength only nudges it (applied in the brain)."""
    bc = _corner_model()["base_corners"]
    tot = bc["home"] + bc["away"]
    return (bc["home"] if leg_is_home else bc["away"]) / tot


# ── unified YES fair value ───────────────────────────────────────────────────


def fair_yes_v2(parsed, mu_home, mu_away, leg_is_home=True,
                mean_corners=None, margin_so_far=0, home_so_far=0, away_so_far=0,
                corners_so_far=0):
    """YES fair probability for a parsed v2 leg. mu_home/mu_away are FULL-MATCH
    expected goals; 1H legs are scaled internally. Returns None for legs priced
    elsewhere (full winner -> brain model+LLM; score/player handled separately)."""
    typ, period, line = parsed["type"], parsed["period"], parsed["line"]
    mh, ma = period_rates(mu_home, mu_away, period)

    if typ == "total" and line is not None:
        return m.total_over_prob(mh, ma, line, home_so_far + away_so_far)
    if typ == "team_total" and line is not None:
        mu_team = mh if leg_is_home else ma
        so_far = home_so_far if leg_is_home else away_so_far
        return m.team_total_over_prob(mu_team, line, so_far)
    if typ == "spread" and line is not None:
        return m.spread_over_prob(mh, ma, line, leg_is_home, margin_so_far)
    if typ == "btts":
        return m.btts_prob(mh, ma, home_so_far, away_so_far)
    if typ == "winner" and period in ("1H", "2H"):
        p_h, p_t, p_a = result_probs(mh, ma, margin_so_far)
        if parsed["team"] is None:          # the Tie leg
            return p_t
        return p_h if leg_is_home else p_a
    if typ == "corners" and parsed["threshold"] is not None:
        mean = mean_corners if mean_corners is not None else base_corner_total()
        return corner_atleast_prob(mean, parsed["threshold"], corners_so_far)
    if typ == "team_corners" and parsed["threshold"] is not None:
        mean = mean_corners if mean_corners is not None else base_corner_total()
        team_mean = mean * team_corner_share(leg_is_home)
        team_so_far = home_so_far if leg_is_home else away_so_far  # corners_so_far per side
        return corner_atleast_prob(team_mean, parsed["threshold"], team_so_far)
    if typ == "first_to_score":
        p_h, p_a, p_none = first_to_score_probs(mh, ma)
        if parsed["team"] is None:          # the "No Goal" leg
            return p_none
        return p_h if leg_is_home else p_a
    # ── knockout progression (simple ELO model; ties split ~50/50 ET vs pens) ──
    if typ in ("advance", "win_method", "decide_phase"):
        p_h, p_t, p_a = result_probs(mh, ma, margin_so_far)
        base = p_h + p_a
        if typ == "advance":                # P(team wins reg) + P(draw)·P(team wins the tie)
            w_h = (p_h / base) if base > 0 else 0.5
            p_home_adv = p_h + p_t * w_h
            return p_home_adv if leg_is_home else (1.0 - p_home_adv)
        ph = parsed.get("phase")
        if typ == "win_method":             # this team wins in reg / ET / pens
            if ph == "reg":
                return p_h if leg_is_home else p_a
            if ph in ("et", "pen"):
                w = ((p_h if leg_is_home else p_a) / base) if base > 0 else 0.5
                return p_t * w * 0.5        # half of tie-wins settle in ET, half in pens
            return None
        if typ == "decide_phase":           # game decided in reg / ET / pens (either team)
            if ph == "reg":
                return p_h + p_a            # = 1 - p_tie (someone wins in regulation)
            if ph in ("et", "pen"):
                return p_t * 0.5
            return None
    # winner(full), score, first_goalscorer -> not priced here
    return None
