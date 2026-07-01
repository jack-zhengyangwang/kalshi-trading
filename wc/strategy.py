"""
strategy_v3.py — expanded strategist for Arena v3.

Fixes the v2 failure modes the YouTube/strategy research surfaced:
  • PRICE BAND (price_floor/ceiling)  → structurally bans 6c Haiti-type longshots
                                         AND bans no-edge near-certain favorites.
  • FIXED-UNIT CAP (unit_cap_frac)    → no bet exceeds X% of bankroll, regardless of
                                         what Kelly wants. This is THE fix for the
                                         Haiti x200 blowup (uncapped Kelly on longshots).
  • FAVORITE-TILT ALLOCATION (alloc_tilt) → stake scaled by p_fair**tilt, so the
                                         majority of money goes to HIGH-probability
                                         legs and only scraps to longshots.
  • EDGE-IN-SIGMA (edge_sigma_k)      → require edge >= k*sigma (uncertainty-aware;
                                         kills fake edge on noisy longshots).
  • MARKET FOCUS (market_focus)       → a team can specialize (e.g. a totals/Over-1.5
                                         specialist), per the proven WC totals edge.
  • multi-event is allowed; the no-YES+NO-same-event rule is enforced by the executor.

New seed archetypes alongside the classics: 'favorite' (high-prob/value), 'totals'
(Over-1.5 / filtered totals specialist), 'value_hunter', and 'flow' (whale-follow,
needs a flow signal — inert until that's wired).
"""
import random

from wc.lib import kelly
from wc.lib.exit_rules import decide_exit, disarm  # noqa: F401

# param -> (lo, hi). Flags rounded; optional exit rules may be None.
PARAM_SPACE = {
    "pregame":            (0.0, 1.0),
    "inplay":             (0.0, 1.0),
    "price_floor":        (0.12, 0.55),   # don't bet below this price (longshot guard;
                                          #   raised 0.03->0.12 so no team can spec in
                                          #   sub-12c longshots — the residual Haiti risk)
    "price_ceiling":      (0.55, 0.92),   # don't bet above this price. Capped at 92c:
                                          #   near-locks (96-98c) won't fill at size and
                                          #   carry no edge, so pre-game won't chase them.
    "min_edge":           (0.005, 0.12),
    "edge_sigma_k":       (0.0, 2.0),     # require edge >= k * sigma
    "alloc_tilt":         (0.5, 3.0),     # stake *= p_fair**tilt (favor high-prob; floored
                                          #   at 0.5 so the favorite-tilt can't be disabled)
    "kelly_fraction":     (0.03, 0.60),
    "tvm_rate":           (0.0, 0.6),     # time-value discount per week to resolution.
                                          #   Higher = more impatient: a slow-resolving
                                          #   market (knockout advance/outright) is sized
                                          #   down for the open slot it ties up. Evolvable
                                          #   so the GA learns each team's time-preference.
    "unit_cap_frac":      (0.01, 0.10),   # hard cap: max fraction of bankroll per bet
                                          #   (ceiling 0.15->0.10: tighter per-bet risk)
    "scalp":              (0.0, 1.0),
    "scalp_min_minute":   (40.0, 85.0),
    "scalp_min_prob":     (0.60, 0.95),
    "exit_take_profit_pct": (0.10, 1.50),
    "exit_sell_margin":   (0.02, 0.20),
    "exit_stop_fair":     (0.02, 0.30),
    "exit_fraction":      (0.30, 1.0),
}
_FLAGS = {"pregame", "inplay", "scalp"}
_OPTIONAL = {"exit_take_profit_pct", "exit_sell_margin"}
_FIXED = {"tvm_rate": 0.08, "max_bet_dollars": 1e9, "min_bet_dollars": 1.0}
# market_focus is a non-numeric attribute carried alongside params (None = bet all
# leg types; else a list like ["total"] or ["winner"]).


def seed_params():
    """Archetype seeds. 'favorite' and 'totals' are the high-prob anchors the
    research says are the real edge; classics kept but now band-constrained."""
    base = dict(price_floor=0.05, price_ceiling=0.95, min_edge=0.03, edge_sigma_k=0.0,
                alloc_tilt=1.0, kelly_fraction=0.25, unit_cap_frac=0.05, tvm_rate=0.08,
                scalp=0, scalp_min_minute=70, scalp_min_prob=0.85,
                exit_stop_fair=0.06, exit_fraction=0.65)

    def mk(**kw):
        d = dict(base); d.update(kw); return d

    return {
        # HIGH-PROB / value favorite backer — small edge on likely outcomes
        "favorite": (mk(pregame=1, inplay=0, price_floor=0.45, price_ceiling=0.95,
                        alloc_tilt=2.0, min_edge=0.02, unit_cap_frac=0.06), None),
        # TOTALS specialist — the proven WC Over-1.5 / filtered-totals edge
        "totals":   (mk(pregame=1, inplay=0, price_floor=0.45, price_ceiling=0.92,
                        alloc_tilt=1.5, min_edge=0.02, unit_cap_frac=0.06), ["total"]),
        # CORNERS specialist — the real corner edge is IN-PLAY on TEAM corners
        # (dominating side, via live ESPN corners + dominance). Totals = no edge,
        # so focus team_corners only; edges are small (~2-4%) → low min_edge, tight cap.
        # EXIT FIX: a team-corner OVER on a dominating side is a MONOTONE accumulator —
        # its prob only ratchets up as corners arrive, so a fair-price STOP just dumps it
        # on noise/lumpy timing before the corners come (that was the replay "0% win").
        # So the STOP is pinned to its floor (~off → hold-to-settle); the ONLY exit is
        # OVERPRICED scale-out (harvest market overshoot) + a one-shot take-profit.
        "corners":  (mk(pregame=0, inplay=1, scalp=0, price_floor=0.12, price_ceiling=0.90,
                        alloc_tilt=1.0, min_edge=0.015, kelly_fraction=0.20,
                        unit_cap_frac=0.04, exit_take_profit_pct=0.4,
                        exit_sell_margin=0.05, exit_stop_fair=0.02), ["team_corners"]),
        # balanced value hunter — bets value across a wide band, tilt to high-prob
        "value_hunter": (mk(pregame=1, inplay=0, price_floor=0.12, price_ceiling=0.9,
                            alloc_tilt=1.2, min_edge=0.03, unit_cap_frac=0.04), None),
        # whale/flow follower — needs the flow signal; inert until wired
        "flow":     (mk(pregame=1, inplay=1, price_floor=0.1, price_ceiling=0.95,
                        alloc_tilt=1.0, unit_cap_frac=0.04), None),
        # classics (now band-constrained so they can't go full-Haiti)
        "aggressive_hold": (mk(pregame=1, inplay=0, price_floor=0.1, price_ceiling=0.92,
                               alloc_tilt=1.0, min_edge=0.02, kelly_fraction=0.45,
                               unit_cap_frac=0.08), None),
        "conservative_active": (mk(pregame=1, inplay=0, price_floor=0.2, price_ceiling=0.9,
                                   alloc_tilt=1.5, min_edge=0.05, kelly_fraction=0.15,
                                   unit_cap_frac=0.04, edge_sigma_k=0.5,
                                   exit_take_profit_pct=0.5, exit_sell_margin=0.05,
                                   exit_stop_fair=0.1), None),
        "late_scalp": (mk(pregame=0, inplay=1, scalp=1, scalp_min_minute=60,
                          scalp_min_prob=0.75, price_floor=0.1, price_ceiling=0.95,
                          kelly_fraction=0.2, unit_cap_frac=0.05), None),
        "momentum": (mk(pregame=0, inplay=1, price_floor=0.1, price_ceiling=0.92,
                        alloc_tilt=1.0, kelly_fraction=0.3, unit_cap_frac=0.05,
                        exit_take_profit_pct=0.4, exit_sell_margin=0.05,
                        exit_stop_fair=0.1), None),
    }


def clip(params):
    out = dict(params)
    for k, (lo, hi) in PARAM_SPACE.items():
        if k in out and out[k] is not None:
            v = min(hi, max(lo, out[k]))
            out[k] = round(v) if k in _FLAGS else round(v, 4)
    # keep floor < ceiling
    if out.get("price_floor", 0) >= out.get("price_ceiling", 1):
        out["price_floor"] = max(0.03, out["price_ceiling"] - 0.1)
    return out


def random_params(rng):
    p = {k: (round(rng.uniform(*b)) if k in _FLAGS else round(rng.uniform(*b), 4))
         for k, b in PARAM_SPACE.items()}
    for k in _OPTIONAL:
        if rng.random() < 0.4:
            p[k] = None
    return clip(p)


def mutate(params, rng, rate=0.25, scale=0.20):
    out = dict(params)
    for k, (lo, hi) in PARAM_SPACE.items():
        if rng.random() >= rate:
            continue
        base = out.get(k)
        if base is None:
            base = rng.uniform(lo, hi)
        out[k] = base + rng.gauss(0, scale * (hi - lo))
    return clip(out)


def perturb(params, rng):
    return mutate(params, rng, rate=0.4, scale=0.12)


def crossover(p1, p2, rng):
    return clip({k: (p1.get(k) if rng.random() < 0.5 else p2.get(k)) for k in PARAM_SPACE})


class Strategist:
    def __init__(self, params, market_focus=None):
        self.p = clip(params)
        self.market_focus = market_focus      # None = all leg types

    def _cfg(self):
        def val(k, d):                       # None-safe (crossover can yield None);
            v = self.p.get(k)                # preserves a legit 0.0 (e.g. tvm_rate=0)
            return d if v is None else v
        c = dict(_FIXED)
        c.update({"min_edge": val("min_edge", 0.03),
                  "kelly_fraction": val("kelly_fraction", 0.25),
                  "tvm_rate": val("tvm_rate", _FIXED["tvm_rate"])})
        return c

    def entry_size(self, leg, balance, tau_days=0):
        """Dollars to bet (0 = skip). leg carries: p_fair, ask(cents), sigma,
        in_play(bool), minute, type(market type)."""
        ask_c = leg.get("ask")
        if not ask_c:
            return 0.0
        ask = ask_c / 100.0
        # PRICE BAND — the structural longshot/no-edge guard
        if ask < self.p.get("price_floor", 0.05) or ask > self.p.get("price_ceiling", 0.95):
            return 0.0
        # market specialization
        if self.market_focus and leg.get("type") not in self.market_focus:
            return 0.0
        # entry window
        in_play = leg.get("in_play", False)
        if in_play and not self.p.get("inplay"):
            return 0.0
        if not in_play and not self.p.get("pregame"):
            return 0.0
        p_fair = leg["p_fair"]
        if in_play and self.p.get("scalp"):
            if (leg.get("minute") or 0) < self.p.get("scalp_min_minute", 70):
                return 0.0
            if p_fair < self.p.get("scalp_min_prob", 0.85):
                return 0.0
        edge = p_fair - ask
        if edge < self.p.get("min_edge", 0.03):
            return 0.0
        # uncertainty-aware: require edge >= k * sigma
        sigma = leg.get("sigma", 0.12)
        if edge < self.p.get("edge_sigma_k", 0.0) * sigma:
            return 0.0
        # Kelly, then FAVORITE-TILT, then HARD UNIT CAP
        stake = kelly.size_tvm(p_fair, ask, balance, self._cfg(), tau_days)
        if stake <= 0:
            return 0.0
        stake *= p_fair ** self.p.get("alloc_tilt", 1.0)        # favor high-prob legs
        cap = self.p.get("unit_cap_frac", 0.05) * balance       # hard per-bet cap
        return min(stake, cap)

    def exit_decision(self, position, live_p_fair, bid):
        if live_p_fair is None:
            return ("HOLD", 0.0)
        bid_d = bid / 100.0 if bid else None
        return decide_exit(live_p_fair, bid_d, position.get("entry"), position, self.p)
