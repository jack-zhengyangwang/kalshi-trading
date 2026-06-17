"""
strategy_v2.py — Arena v2 strategist + evolving strategy space.

A "team" is a point in a continuous PARAMETER SPACE. The four classic archetypes
(aggressive_hold / conservative_active / late_scalp / momentum) are just seed
points; the arena breeds and mutates new points from the winners (PBT/evolution).

The Strategist decides HOW MUCH to bet and WHEN, from (p_fair, ask/payoff, time,
bankroll). The Executor (arena_v2) does the scanning/placing — zero LLM. Exits
reuse the shared group.exit_rules so paper and live can never drift.

Param schema (all evolvable, clipped to PARAM_SPACE bounds):
  pregame        1.0/0.0  — may enter before kickoff
  inplay         1.0/0.0  — may enter during the match
  min_edge       entry edge floor (p_fair - ask)
  edge_haircut   subtract from p_fair before sizing (conservatism)
  kelly_fraction fractional-Kelly multiplier
  min_entry_price never buy below this ask (penny-longshot guard)
  scalp          1.0/0.0  — require a late, near-certain outcome to enter in-play
  scalp_min_minute / scalp_min_prob — gates when scalp=1
  exit_take_profit_pct / exit_sell_margin / exit_stop_fair / exit_fraction
                 — exit thresholds (a None/absent rule is disabled)
"""
import math
import random

from group import kelly
from group.exit_rules import decide_exit, disarm  # noqa: F401 (re-exported)

# param -> (lo, hi) bounds for mutation + clipping. Binary flags use (0,1) and are
# rounded on mutation.
PARAM_SPACE = {
    "pregame":            (0.0, 1.0),
    "inplay":             (0.0, 1.0),
    "min_edge":           (0.005, 0.12),
    "edge_haircut":       (0.0, 0.06),
    "kelly_fraction":     (0.03, 0.60),
    "min_entry_price":    (0.02, 0.20),
    "scalp":              (0.0, 1.0),
    "scalp_min_minute":   (40.0, 85.0),
    "scalp_min_prob":     (0.60, 0.95),
    "exit_take_profit_pct": (0.10, 1.50),
    "exit_sell_margin":   (0.02, 0.20),
    "exit_stop_fair":     (0.02, 0.30),
    "exit_fraction":      (0.30, 1.0),
}
_FLAGS = {"pregame", "inplay", "scalp"}
# exit rules that may be "disabled" (set to None) for hold-to-resolution teams
_OPTIONAL = {"exit_take_profit_pct", "exit_sell_margin"}

# tvm/cap defaults shared by all teams (not evolved here)
_FIXED = {"tvm_rate": 0.08, "max_bet_dollars": 10.0, "min_bet_dollars": 1.0}


def seed_params():
    """The four archetype seeds, as points in the param space."""
    return {
        "aggressive_hold": {
            "pregame": 1, "inplay": 0, "min_edge": 0.02, "edge_haircut": 0.0,
            "kelly_fraction": 0.50, "min_entry_price": 0.05, "scalp": 0,
            "scalp_min_minute": 70, "scalp_min_prob": 0.85,
            "exit_stop_fair": 0.05, "exit_fraction": 0.65,
        },
        "conservative_active": {
            "pregame": 1, "inplay": 0, "min_edge": 0.05, "edge_haircut": 0.02,
            "kelly_fraction": 0.15, "min_entry_price": 0.05, "scalp": 0,
            "scalp_min_minute": 70, "scalp_min_prob": 0.85,
            "exit_take_profit_pct": 0.50, "exit_sell_margin": 0.05,
            "exit_stop_fair": 0.10, "exit_fraction": 0.65,
        },
        "late_scalp": {
            "pregame": 0, "inplay": 1, "min_edge": 0.01, "edge_haircut": 0.0,
            "kelly_fraction": 0.20, "min_entry_price": 0.10, "scalp": 1,
            "scalp_min_minute": 60, "scalp_min_prob": 0.75,
            "exit_stop_fair": 0.05, "exit_fraction": 0.65,
        },
        "momentum": {
            "pregame": 0, "inplay": 1, "min_edge": 0.03, "edge_haircut": 0.0,
            "kelly_fraction": 0.30, "min_entry_price": 0.05, "scalp": 0,
            "scalp_min_minute": 70, "scalp_min_prob": 0.85,
            "exit_take_profit_pct": 0.40, "exit_sell_margin": 0.05,
            "exit_stop_fair": 0.10, "exit_fraction": 0.65,
        },
    }


def clip(params):
    out = dict(params)
    for k, (lo, hi) in PARAM_SPACE.items():
        if k in out and out[k] is not None:
            v = min(hi, max(lo, out[k]))
            out[k] = round(v) if k in _FLAGS else round(v, 4)
    return out


def random_params(rng):
    p = {}
    for k, (lo, hi) in PARAM_SPACE.items():
        v = rng.uniform(lo, hi)
        p[k] = round(v) if k in _FLAGS else round(v, 4)
    # randomly disable optional exit rules (-> hold-style team)
    for k in _OPTIONAL:
        if rng.random() < 0.4:
            p[k] = None
    return p


def mutate(params, rng, rate=0.25, scale=0.20):
    """Perturb each param with prob `rate` by a Gaussian of `scale`*range."""
    out = dict(params)
    for k, (lo, hi) in PARAM_SPACE.items():
        if rng.random() >= rate:
            continue
        if k in _OPTIONAL and rng.random() < 0.15:
            out[k] = None                      # occasionally toggle a rule off
            continue
        base = out.get(k)
        if base is None:                       # turning a disabled rule back on
            base = rng.uniform(lo, hi)
        out[k] = base + rng.gauss(0, scale * (hi - lo))
    return clip(out)


def crossover(p1, p2, rng):
    """Uniform per-param crossover of two parents."""
    out = {}
    for k in PARAM_SPACE:
        out[k] = p1.get(k) if rng.random() < 0.5 else p2.get(k)
    return clip(out)


class Strategist:
    """Decides entry size + exit actions for one team's params."""

    def __init__(self, params):
        self.p = clip(params)

    def _cfg(self):
        c = dict(_FIXED)
        c.update({"min_edge": self.p.get("min_edge", 0.03),
                  "kelly_fraction": self.p.get("kelly_fraction", 0.25)})
        return c

    def entry_size(self, leg, balance, tau_days=0):
        """Dollars to bet on a priced leg (0 = skip). `leg` carries:
        p_fair, ask (cents), in_play(bool), minute(int|None)."""
        ask_c = leg.get("ask")
        if not ask_c:
            return 0.0
        ask = ask_c / 100.0
        if ask < self.p.get("min_entry_price", 0.05):
            return 0.0                          # penny-longshot guard

        in_play = leg.get("in_play", False)
        if in_play and not self.p.get("inplay"):
            return 0.0
        if not in_play and not self.p.get("pregame"):
            return 0.0

        p_fair = leg["p_fair"]
        if in_play and self.p.get("scalp"):
            minute = leg.get("minute") or 0
            if minute < self.p.get("scalp_min_minute", 70):
                return 0.0
            if p_fair < self.p.get("scalp_min_prob", 0.85):
                return 0.0

        p_use = max(0.0, p_fair - self.p.get("edge_haircut", 0.0))
        return kelly.size_tvm(p_use, ask, balance, self._cfg(), tau_days)

    def exit_decision(self, position, live_p_fair, bid):
        """(action, fraction). Reuses the shared re-armed exit logic."""
        if live_p_fair is None:
            return ("HOLD", 0.0)
        bid_d = bid / 100.0 if bid else None
        return decide_exit(live_p_fair, bid_d, position.get("entry"), position, self.p)
