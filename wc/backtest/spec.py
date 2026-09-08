"""Strategy specification: the DSL schema and its validator.

A strategy is DATA, not code. An LLM (or a human) fills in this schema; the
interpreter in interpret.py executes it. Nothing here ever evals, imports, or
executes a string — that property is the whole point of the DSL, and it is why
there is no escape hatch (no `custom_python`, no callback, no expression field).

See docs/backtester/03_STRATEGY_DSL.md.
"""
from __future__ import annotations

import json


class SpecError(ValueError):
    """A strategy spec is malformed. Raised at load time, never at trade time."""


# ── vocabulary ────────────────────────────────────────────────────────────────
# Every signal is computed by the engine from the TRAILING bar view only. None
# can read the future; see engine.BarView.

SIGNALS = {
    # name:              (low, high, "description")
    "price":             (0.0, 1.0, "price of the side WE are buying, as a probability"),
    "yes_price":         (0.0, 1.0, "the market's YES price, independent of our side. "
                                    "Use this to describe the market (e.g. 'a cheap "
                                    "longshot'); use `price` for what we pay"),
    "yes_bid":           (0, 100, "best yes bid, cents"),
    "yes_ask":           (0, 100, "best yes ask, cents"),
    "spread":            (0, 100, "ask - bid, cents"),
    "days_to_resolution": (0.0, 3650.0, "from close_time; edge varies sharply on this"),
    "volume_24h":        (0, None, "contracts traded, trailing 24h"),
    "open_interest":     (0, None, "open contracts"),
    "oi_change_pct":     (-1.0, None, "open-interest change over lookback"),
    "price_change_pct":  (-1.0, None, "price change over lookback"),
    "volatility":        (0.0, None, "stdev of price over trailing window"),
    "model_prob":        (0.0, 1.0, "our own p_fair from the brains, when available"),
    "edge":              (-1.0, 1.0, "model_prob - price"),
}

# Exit-only signals: meaningless without an open position.
POSITION_SIGNALS = {
    "unrealized_pnl_pct": (-1.0, None, "position P&L as a fraction of cost"),
    "hold_to_settlement": (None, None, "bool; terminal exit at settlement"),
    "days_held":          (0.0, None, "days since entry"),
}

OPS = {"lt", "lte", "gt", "gte", "eq", "between"}
COMBINATORS = {"all", "any"}
SIDES = {"yes", "no"}
SIZING_METHODS = {"kelly", "fixed", "fraction_of_bankroll"}

REQUIRED_CAPS = ("daily_spend_dollars", "per_market_dollars", "total_exposure_dollars")


def _fail(msg):
    raise SpecError(msg)


def _check_condition(cond, where, allow_position_signals):
    if not isinstance(cond, dict):
        _fail(f"{where}: condition must be an object, got {type(cond).__name__}")

    # nested combinator
    combi = [k for k in cond if k in COMBINATORS]
    if combi:
        if len(cond) != 1:
            _fail(f"{where}: a combinator object may hold only '{combi[0]}'")
        inner = cond[combi[0]]
        if not isinstance(inner, list) or not inner:
            _fail(f"{where}.{combi[0]}: must be a non-empty list")
        for i, c in enumerate(inner):
            _check_condition(c, f"{where}.{combi[0]}[{i}]", allow_position_signals)
        return

    for key in ("signal", "op", "value"):
        if key not in cond:
            _fail(f"{where}: missing '{key}'")

    sig, op, val = cond["signal"], cond["op"], cond["value"]

    known = dict(SIGNALS)
    if allow_position_signals:
        known.update(POSITION_SIGNALS)
    if sig not in known:
        extra = " (position signals are exit-only)" if sig in POSITION_SIGNALS else ""
        _fail(f"{where}: unknown signal '{sig}'{extra}. "
              f"Extending the vocabulary is a deliberate schema change — "
              f"see docs/backtester/03_STRATEGY_DSL.md section 5")
    if op not in OPS:
        _fail(f"{where}: unknown operator '{op}'. Known: {sorted(OPS)}")

    if op == "between":
        if not isinstance(val, (list, tuple)) or len(val) != 2:
            _fail(f"{where}: 'between' needs a [low, high] pair")
        if val[0] >= val[1]:
            _fail(f"{where}: 'between' low must be < high, got {val}")
        vals = list(val)
    elif isinstance(val, bool):
        vals = []                                   # bool signals are unbounded
    elif isinstance(val, (int, float)):
        vals = [val]
    else:
        _fail(f"{where}: value must be a number, bool, or [low, high]; got {val!r}")

    lo, hi = known[sig][0], known[sig][1]
    for v in vals:
        if lo is not None and v < lo:
            _fail(f"{where}: {sig}={v} below its minimum {lo}")
        if hi is not None and v > hi:
            _fail(f"{where}: {sig}={v} above its maximum {hi}")


def validate(spec):
    """Validate a strategy spec. Raises SpecError with a useful message, or
    returns the spec unchanged."""
    if not isinstance(spec, dict):
        _fail(f"spec must be an object, got {type(spec).__name__}")

    for key in ("name", "side", "entry", "sizing", "exit", "caps"):
        if key not in spec:
            _fail(f"missing required field '{key}'")

    if not str(spec["name"]).strip():
        _fail("'name' must be a non-empty string")

    if spec["side"] not in SIDES:
        _fail(f"'side' must be one of {sorted(SIDES)}, got {spec['side']!r}")

    # ── entry / exit ─────────────────────────────────────────────────────────
    entry = spec["entry"]
    if not isinstance(entry, dict) or not entry:
        _fail("'entry' must be a non-empty condition object — "
              "a strategy with no entry conditions would trade everything")
    _check_condition(entry, "entry", allow_position_signals=False)

    exit_ = spec["exit"]
    if not isinstance(exit_, dict) or not exit_:
        _fail("'exit' must be a non-empty condition object")
    _check_condition(exit_, "exit", allow_position_signals=True)

    # ── sizing ───────────────────────────────────────────────────────────────
    sizing = spec["sizing"]
    if not isinstance(sizing, dict):
        _fail("'sizing' must be an object")
    method = sizing.get("method")
    if method not in SIZING_METHODS:
        _fail(f"sizing.method must be one of {sorted(SIZING_METHODS)}, got {method!r}")
    if method == "kelly":
        frac = sizing.get("fraction")
        if not isinstance(frac, (int, float)) or not (0 < frac <= 1):
            _fail(f"sizing.fraction must be in (0, 1], got {frac!r}")
    if "max_bet_dollars" in sizing:
        if not isinstance(sizing["max_bet_dollars"], (int, float)) \
                or sizing["max_bet_dollars"] <= 0:
            _fail("sizing.max_bet_dollars must be a positive number")

    # ── caps: mandatory, and must be usable ──────────────────────────────────
    # Consistent with wc/lib/caps.py: a cap that is missing or non-positive
    # must block, never silently disable the check.
    caps = spec["caps"]
    if not isinstance(caps, dict):
        _fail("'caps' must be an object")
    for key in REQUIRED_CAPS:
        if key not in caps:
            _fail(f"caps.{key} is required — a strategy without caps is not a "
                  f"strategy. Caps fail closed here as they do in wc/lib/caps.py")
        v = caps[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            _fail(f"caps.{key} must be a positive number, got {v!r}")

    # ── universe (optional) ──────────────────────────────────────────────────
    uni = spec.get("universe", {})
    if not isinstance(uni, dict):
        _fail("'universe' must be an object")

    known_uni = {"series", "max_yes_price_cents", "min_yes_price_cents",
                 "min_volume", "min_open_interest",
                 "min_days_to_resolution", "max_days_to_resolution"}
    for key in uni:
        if key not in known_uni:
            hint = ""
            if key in ("max_price_cents", "min_price_cents"):
                hint = (" — universe describes the MARKET, so use "
                        "max_yes_price_cents / min_yes_price_cents")
            _fail(f"unknown universe key '{key}'{hint}. Known: {sorted(known_uni)}")

    for key in ("max_yes_price_cents", "min_yes_price_cents"):
        if key in uni and not (0 <= uni[key] <= 100):
            _fail(f"universe.{key} must be within 0-100 cents, got {uni[key]}")

    return spec


def load(path):
    """Load and validate a strategy spec from a JSON file."""
    with open(path) as f:
        try:
            spec = json.load(f)
        except json.JSONDecodeError as e:
            raise SpecError(f"{path}: invalid JSON — {e}") from e
    try:
        return validate(spec)
    except SpecError as e:
        raise SpecError(f"{path}: {e}") from e
