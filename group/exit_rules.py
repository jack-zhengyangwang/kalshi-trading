"""
exit_rules.py — the single, shared in-play exit decision used by BOTH the live
Keeper and the paper-tournament strategies, so the two can never drift apart.

decide_exit(...) implements the re-armed scale-out logic:
  • STOP        — live fair below `exit_stop_fair`  → sell 100%
  • OVERPRICED  — bid above live fair + `exit_sell_margin` → scale out once per
                  episode (re-arms only after the market cools back below the
                  threshold); sells 100% if it's *very* overpriced (2× margin)
  • TAKE_PROFIT — gain vs entry ≥ `exit_take_profit_pct` → scale out once ever
  • HOLD        — otherwise

Any rule whose threshold key is absent/None is disabled (lets a strategy enable
only the rules it wants — e.g. hold-to-maturity uses STOP only).

`flags` is a mutable dict carrying per-position re-arm state (`_op_armed`,
`_tp_done`). The CALLER flips those to "fired" only after a *successful* sell, so
a failed/unfilled sell is retried next poll — never mutate them on a no-fill.
"""


def decide_exit(live_fair, bid, entry_price, flags, p):
    """Return (action, fraction_to_sell). See module docstring."""
    stop = p.get("exit_stop_fair")
    margin = p.get("exit_sell_margin")
    tp = p.get("exit_take_profit_pct")
    frac = p.get("exit_fraction", 0.65)

    if stop is not None and live_fair < stop:
        return ("STOP", 1.0)

    if margin is not None and bid is not None:
        overpriced = bid > live_fair + margin
        if not overpriced:
            flags["_op_armed"] = True            # re-arm once it cools off
        if overpriced and flags.get("_op_armed", True):
            return ("OVERPRICED", 1.0 if bid > live_fair + 2 * margin else frac)

    if (tp is not None and entry_price and bid is not None
            and not flags.get("_tp_done")
            and (bid - entry_price) / entry_price >= tp):
        return ("TAKE_PROFIT", frac)

    return ("HOLD", 0.0)


def disarm(action, flags):
    """Caller invokes this AFTER a confirmed sell to consume the one-shot triggers."""
    if action == "OVERPRICED":
        flags["_op_armed"] = False
    elif action == "TAKE_PROFIT":
        flags["_tp_done"] = True
