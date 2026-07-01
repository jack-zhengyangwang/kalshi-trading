"""
strategy.py — pluggable entry/exit behaviors for the paper tournament.

Each team runs one Strategy with its own params. Strategies share the Brain's
fair-value output and only differ in HOW they enter and exit. The tournament
runner calls:
    entry_dollars(ctx) -> float            # 0 = skip
    exit_decision(position, ctx) -> (action, fraction)

`ctx` is a dict the runner fills per market each cycle:
    market, pregame (Brain.evaluate result | None), live (evaluate_live | None),
    ask (dollars|None), bid (dollars|None), balance, tau_days, game_state(|None)

Re-arm state for active exits lives on the position dict (`_op_armed`, `_tp_done`),
flipped by the runner only on a successful paper sell — same fix as the Keeper.
"""
from group import kelly
from group.exit_rules import decide_exit


def _prob(result, basis="lo"):
    """
    Pick the probability a strategy bets on, from a Brain result.
      "mean"    → the blended p_fair (aggressive — no uncertainty haircut)
      "lo_half" → p_fair minus HALF the sigma haircut (moderate)
      "lo"      → the full conservative lower bound (p_fair_lo / live_p_fair - sigma)
    """
    if result is None:
        return None
    mean = result.get("p_fair", result.get("live_p_fair", 0.5))
    sigma = result.get("sigma", 0.15)
    if basis == "mean":
        return mean
    if basis == "lo_half":
        return max(0.01, mean - 0.5 * sigma)
    if "p_fair_lo" in result:
        return result["p_fair_lo"]
    return max(0.01, mean - sigma)


class Strategy:
    def __init__(self, name, params):
        self.name = name
        self.p = params

    def _kelly_dollars(self, prob, ask, balance, tau_days, min_edge=None):
        # Price floor: never buy ultra-cheap longshots. At a 1¢ ask a fixed dollar
        # stake buys ~1000 contracts, so a miscalibrated in-play estimate dumps a
        # huge position into a near-zero market (the Momentum penny-longshot bug).
        if ask is not None and ask < self.p.get("min_entry_price", 0.05):
            return 0.0
        cfg = {
            "min_edge": min_edge if min_edge is not None else self.p.get("min_edge", 0.03),
            "kelly_fraction": self.p.get("kelly_fraction", 0.25),
            "tvm_rate": self.p.get("tvm_rate", 0.08),
            "max_bet_dollars": self.p.get("max_bet_dollars", 10.0),
            "min_bet_dollars": self.p.get("min_bet_dollars", 1.0),
        }
        return kelly.size_tvm(prob, ask, balance, cfg, tau_days)

    def entry_dollars(self, ctx):
        raise NotImplementedError

    def exit_decision(self, position, ctx):
        """Shared re-armed exit (group/exit_rules). Each strategy's enabled rules
        come from its own params, so behavior differs by config, not by code."""
        live, bid = ctx.get("live"), ctx.get("bid")
        if live is None:
            return ("HOLD", 0.0)
        return decide_exit(live["live_p_fair"], bid, position.get("entry"),
                           position, self.p)


class AggressiveHold(Strategy):
    """High Kelly, low min_edge, bets on the MEAN fair value (no uncertainty
    haircut), enter at ask, hold to resolution (stop only)."""

    def entry_dollars(self, ctx):
        prob = _prob(ctx["pregame"], self.p.get("entry_prob", "mean"))
        ask = ctx["ask"]
        if prob is None or ask is None:
            return 0.0
        return self._kelly_dollars(prob, ask, ctx["balance"], ctx["tau_days"])


class ConservativeActive(Strategy):
    """Low Kelly, high min_edge, active exit (overpriced / take-profit / stop)."""

    def entry_dollars(self, ctx):
        prob = _prob(ctx["pregame"], self.p.get("entry_prob", "lo"))
        ask = ctx["ask"]
        if prob is None or ask is None:
            return 0.0
        return self._kelly_dollars(prob, ask, ctx["balance"], ctx["tau_days"])


class LateScalp(Strategy):
    """Enter only late when an outcome is near-certain and priced at/below fair —
    small, high-probability income (the user's $60-Tie→$3 play). Hold to resolution."""

    def entry_dollars(self, ctx):
        gs, live, ask = ctx.get("game_state"), ctx.get("live"), ctx["ask"]
        if not gs or gs.get("status") != "in" or live is None or ask is None:
            return 0.0
        if gs["minute"] < self.p.get("scalp_min_minute", 75):
            return 0.0
        fair = live["live_p_fair"]
        # Buy a near-certain outcome late as long as there's ANY positive edge
        # (the small, reliable income play). Uses a tiny scalp-specific min_edge,
        # not the strategy's general one.
        if fair >= self.p.get("scalp_min_prob", 0.85):
            return self._kelly_dollars(
                fair, ask, ctx["balance"], 0,
                min_edge=self.p.get("scalp_min_edge", 0.005))
        return 0.0


class Momentum(Strategy):
    """Enter IN-PLAY when the live model sees edge the market hasn't priced.
    (Live dominance signal arrives in Phase 3; until then live = score+time.)"""

    def entry_dollars(self, ctx):
        gs, live, ask = ctx.get("game_state"), ctx.get("live"), ctx["ask"]
        if not gs or gs.get("status") != "in" or live is None or ask is None:
            return 0.0
        lo = _prob(live, self.p.get("entry_prob", "lo"))
        return self._kelly_dollars(lo, ask, ctx["balance"], 0) if lo else 0.0


class ScorelineSpecialist(Strategy):
    """Bespoke exact-score specialist — ONE team that combines all four archetypes
    on the KXWCSCORE grid:
      • pre-game  → moderate edge entry (half-sigma basis: between Aggressive's
                    mean and Conservative's full haircut), Kelly-sized.
      • in-play   → Late-Scalp a near-certain exact score late in the game, else
                    Momentum-enter when the live model sees edge the market missed.
      • exits     → active (overpriced / take-profit / stop) via its own params.
    Every lever, one account. Sizing is still edge-vs-ask Kelly, so the wide
    scoreline spreads gate out anything that isn't genuinely underpriced."""

    def entry_dollars(self, ctx):
        ask = ctx["ask"]
        if ask is None:
            return 0.0
        # pre-game leg
        if ctx.get("pregame") is not None:
            prob = _prob(ctx["pregame"], self.p.get("entry_prob", "lo_half"))
            if prob is None:
                return 0.0
            return self._kelly_dollars(prob, ask, ctx["balance"], ctx["tau_days"])
        # in-play leg
        gs, live = ctx.get("game_state"), ctx.get("live")
        if gs and gs.get("status") == "in" and live is not None:
            fair = live["live_p_fair"]
            if (gs.get("minute", 0) >= self.p.get("scalp_min_minute", 80)
                    and fair >= self.p.get("scalp_min_prob", 0.80)):
                return self._kelly_dollars(fair, ask, ctx["balance"], 0,
                                           min_edge=self.p.get("scalp_min_edge", 0.005))
            lo = _prob(live, self.p.get("live_entry_prob", "lo"))
            return self._kelly_dollars(lo, ask, ctx["balance"], 0) if lo else 0.0
        return 0.0


_REGISTRY = {
    "aggressive_hold": AggressiveHold,
    "conservative_active": ConservativeActive,
    "late_scalp": LateScalp,
    "momentum": Momentum,
    "scoreline_combo": ScorelineSpecialist,
}


def build_strategy(kind, name, params):
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unknown strategy kind: {kind}")
    return cls(name, params)
