"""
kelly.py — Conservative fractional Kelly sizing with TVM discount.
"""
import math


def size(p_fair_lo, p_market, balance, config):
    """
    Returns dollars to bet. Returns 0 if edge is insufficient.

    Uses the conservative lower bound (p_fair_lo) as the probability estimate.
    """
    return size_tvm(p_fair_lo, p_market, balance, config, tau_days=0)


def size_tvm(p_fair_lo, p_market, balance, config, tau_days=0):
    """Convenience wrapper returning only the final (capped) dollar bet."""
    return size_detail(p_fair_lo, p_market, balance, config, tau_days)["bet"]


def size_detail(p_fair_lo, p_market, balance, config, tau_days=0):
    """
    Kelly sizing with a time-value-of-money discount for longer-duration markets.

    edge = p_fair_lo - p_market        (conservative: uses lower bound)
    f    = edge / (1 - p_market)       (fractional Kelly for a binary contract)
    discount = exp(-tvm_rate * weeks_to_resolution)

    Returns a dict:
        bet     — final dollars to bet after the max cap (0 = PASS)
        raw     — dollars Kelly wanted before the max cap was applied
        capped  — True if raw exceeded max_bet_dollars (i.e. the cap bound the bet)
        edge    — p_fair_lo - p_market
    Same-day markets (tau<=1) get ~no TVM discount.
    """
    result = {"bet": 0.0, "raw": 0.0, "capped": False, "edge": 0.0}

    if p_market <= 0 or p_market >= 1:
        return result

    edge = p_fair_lo - p_market
    result["edge"] = edge
    if edge < config.get("min_edge", 0.03):
        return result

    f = edge / (1.0 - p_market)
    tau_weeks = max(0.0, tau_days) / 7.0
    tvm_discount = math.exp(-config.get("tvm_rate", 0.08) * tau_weeks)

    raw = f * config.get("kelly_fraction", 0.25) * tvm_discount * balance
    result["raw"] = raw

    max_bet = config.get("max_bet_dollars", 20.0)
    bet = min(raw, max_bet)
    result["capped"] = raw > max_bet

    if bet <= 0:
        return result
    result["bet"] = max(bet, config.get("min_bet_dollars", 1.0))
    return result


def to_contracts(bet_dollars, p_market):
    """Convert a dollar bet to an integer Kalshi contract count (min 1)."""
    if p_market <= 0:
        return 0
    return max(1, int(bet_dollars / p_market))
