"""The interpreter: the ONLY code that executes a strategy.

Pure functions. No I/O, no network, no clock, no filesystem. Everything it can
see arrives in `ctx`, which the engine builds from the trailing bar view — so a
strategy structurally cannot read the future.

The same interpreter serves backtest, paper, and live. There is no second
implementation for a strategy to diverge into between backtest and production.

See docs/backtester/03_STRATEGY_DSL.md section 3.
"""
from __future__ import annotations

from wc.backtest.spec import COMBINATORS
from wc.lib import kelly


class MissingSignal(KeyError):
    """A condition referenced a signal the context cannot supply."""


def _compare(value, op, target):
    if op == "lt":
        return value < target
    if op == "lte":
        return value <= target
    if op == "gt":
        return value > target
    if op == "gte":
        return value >= target
    if op == "eq":
        return value == target
    if op == "between":
        return target[0] <= value <= target[1]
    raise ValueError(f"unknown operator {op!r}")      # unreachable: validated at load


def evaluate(cond, ctx):
    """Evaluate a condition tree against a context dict. Returns bool.

    A condition whose signal is absent from ctx evaluates FALSE rather than
    raising: a strategy must not trade on a signal we could not compute. This
    is the fail-closed direction — the same reasoning as wc/lib/caps.py.
    """
    for combi in COMBINATORS:
        if combi in cond:
            inner = cond[combi]
            if combi == "all":
                return all(evaluate(c, ctx) for c in inner)
            return any(evaluate(c, ctx) for c in inner)

    sig = cond["signal"]
    if sig not in ctx or ctx[sig] is None:
        return False                                   # fail closed
    return _compare(ctx[sig], cond["op"], cond["value"])


def passes_universe(spec, ctx):
    """Cheap pre-filter before the full entry tree.

    Universe describes the MARKET, so its price bounds are on `yes_price` — the
    market's own price, independent of which side we take. A "cheap longshot"
    is a market whose YES trades at 7c, whether we buy the YES at 7c or the NO
    at 93c. `price` (what we pay) belongs in `entry`, not here.
    """
    uni = spec.get("universe") or {}

    yes_price = ctx.get("yes_price")
    if yes_price is not None:
        cents = yes_price * 100.0
        if "max_yes_price_cents" in uni and cents > uni["max_yes_price_cents"]:
            return False
        if "min_yes_price_cents" in uni and cents < uni["min_yes_price_cents"]:
            return False

    for key, sig in (("min_volume", "volume_24h"),
                     ("min_open_interest", "open_interest")):
        if key in uni:
            v = ctx.get(sig)
            if v is None or v < uni[key]:
                return False

    if "min_days_to_resolution" in uni:
        d = ctx.get("days_to_resolution")
        if d is None or d < uni["min_days_to_resolution"]:
            return False
    if "max_days_to_resolution" in uni:
        d = ctx.get("days_to_resolution")
        if d is None or d > uni["max_days_to_resolution"]:
            return False

    series = uni.get("series")
    if series and series != ["*"]:
        if ctx.get("series") not in series:
            return False

    return True


def should_enter(spec, ctx):
    """True when this bar is an entry for this strategy."""
    if not passes_universe(spec, ctx):
        return False
    return evaluate(spec["entry"], ctx)


def should_exit(spec, ctx, position=None):
    """True when an open position should be closed.

    `position` supplies the exit-only signals (unrealized_pnl_pct, days_held).
    They are merged into a copy of ctx — ctx itself is never mutated, so the
    engine can reuse it across strategies in the same bar.
    """
    if position:
        ctx = {**ctx, **position}
    return evaluate(spec["exit"], ctx)


def _kelly_config(sizing):
    """Translate the spec's sizing block into wc/lib/kelly.py's config dict.

    Two defaults differ deliberately from the live path's:

    `min_edge` defaults to 0, because in the DSL an edge threshold is an ENTRY
    condition ({"signal": "edge", "op": "gt", ...}), not a sizing parameter. A
    hidden second threshold inside sizing would silently veto entries the spec
    said to take, and the spec is meant to be the whole strategy.

    `min_bet_dollars` defaults to 0 for the same reason: the live path floors
    tiny bets because a 30c order is not worth placing, but in a backtest that
    floor would round a stake UP, inventing exposure the strategy never asked
    for. Sizing up is the wrong direction to be wrong in.

    `tvm_rate` defaults to 0 (no time-value discount) so a spec means the same
    thing whether or not it opts in. Set it explicitly to get the live
    behaviour.
    """
    return {
        "kelly_fraction":  float(sizing["fraction"]),
        "max_bet_dollars": float(sizing.get("max_bet_dollars", float("inf"))),
        "min_edge":        float(sizing.get("min_edge", 0.0)),
        "min_bet_dollars": float(sizing.get("min_bet_dollars", 0.0)),
        "tvm_rate":        float(sizing.get("tvm_rate", 0.0)),
    }


def size(spec, ctx, bankroll):
    """Dollar stake for an entry. Returns 0.0 when the strategy should not size.

    Kelly delegates to wc/lib/kelly.py. There is deliberately no second Kelly
    implementation in this codebase, and no injection hook to become one: a
    backtest that sized differently from the live path would be measuring a
    strategy we do not actually run.
    """
    sizing = spec["sizing"]
    method = sizing["method"]
    cap = sizing.get("max_bet_dollars")

    if method == "fixed":
        stake = float(sizing.get("dollars", cap or 0.0))

    elif method == "fraction_of_bankroll":
        stake = bankroll * float(sizing.get("fraction", 0.0))

    elif method == "kelly":
        p = ctx.get("model_prob")
        price = ctx.get("price")
        if p is None or price is None or not (0 < price < 1):
            return 0.0                                 # cannot size without both
        tau = ctx.get("days_to_resolution") or 0.0
        stake = kelly.size_tvm(p, price, bankroll, _kelly_config(sizing), tau_days=tau)

    else:
        return 0.0                                     # unreachable: validated

    if cap is not None:
        stake = min(stake, float(cap))
    return max(0.0, min(stake, bankroll))
