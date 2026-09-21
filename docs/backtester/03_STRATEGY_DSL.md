# Phase 3 — Strategy DSL

*DSL = Domain-Specific Language: a small restricted vocabulary for describing
one kind of thing. Here, trading strategies. The contrast is a general-purpose
language like Python, which can express anything.*

---

## 1. Why strategies are data, not code

An LLM never emits Python. It fills in a schema.

```
"sell contracts under 10 cents"
        |  LLM, or written by hand
   { "entry": [{"signal": "price", "op": "lt", "value": 0.10}], ... }
        |  schema validation — malformed specs are rejected here
   interpret.run(spec, bars)      <- the ONLY code, written once, tested once
        |
   PnL, Brier, calibration, drawdown
```

Four consequences, and they are the whole argument:

**Bounded blast radius.** The worst a malformed spec can do is fail validation.
The worst malformed Python can do is anything — an infinite loop, a wallet-
draining call, a subtle sizing bug found only after it trades.

**Strategies become comparable.** Same interpreter, same fee model, same
slippage. When A beats B it is the strategy differing, not two subtly different
backtest implementations.

**Diffable and versionable.** A strategy is a JSON file in git. `git diff` shows
exactly what changed between v1 and v2 of an idea. Generated-Python diffs are
noise.

**No AST analysis needed.** `homerun`, the most mature project in this space,
runs AST checks before activating a strategy — because it accepts arbitrary
Python. We sidestep the problem rather than police it.

It also matches the repo's standing principle: *experiments are config, not new
files.* There is no `strategy_v2.py`.

**The cost, stated plainly:** the schema is a ceiling. A strategy it cannot
express cannot be traded until the schema is deliberately extended. That is the
price of the safety, and extensions are a conscious design step — never a
fallback to generated code.

---

## 2. Schema — draft for review

```json
{
  "name": "sell-cheap-longshots",
  "version": 1,
  "description": "Favorite-longshot bias: sub-10c contracts are systematically overpriced.",
  "universe": {
    "series": ["*"],
    "max_price_cents": 10,
    "min_volume": 100,
    "min_open_interest": 50,
    "min_days_to_resolution": 30
  },
  "side": "no",
  "entry": {
    "all": [
      {"signal": "price",              "op": "lt", "value": 0.10},
      {"signal": "days_to_resolution", "op": "gt", "value": 30},
      {"signal": "volume_24h",         "op": "gt", "value": 100}
    ]
  },
  "sizing": {
    "method": "kelly",
    "fraction": 0.25,
    "max_bet_dollars": 5.0,
    "max_concurrent_positions": 20
  },
  "exit": {
    "any": [
      {"signal": "price",              "op": "gt",  "value": 0.25},
      {"signal": "days_to_resolution", "op": "lt",  "value": 1},
      {"signal": "unrealized_pnl_pct", "op": "lt",  "value": -0.50},
      {"signal": "hold_to_settlement", "op": "eq",  "value": true}
    ]
  },
  "caps": {
    "daily_spend_dollars": 50.0,
    "per_market_dollars": 5.0,
    "total_exposure_dollars": 200.0
  }
}
```

### Signal vocabulary — v1

Deliberately small. Each signal is computed by the engine from the trailing bar
view only; none can read the future.

| Signal | Type | Notes |
|---|---|---|
| `price` | 0–1 | Mid, or side-appropriate bid/ask |
| `yes_bid`, `yes_ask` | cents | Raw book |
| `spread` | cents | `ask - bid` |
| `days_to_resolution` | float | From `close_time`. **First-class — the research shows edge varies sharply along this axis** |
| `volume_24h`, `open_interest` | int | Liquidity |
| `oi_change_pct` | float | Positioning shift |
| `price_change_pct` | float | Over a stated lookback |
| `volatility` | float | Trailing window |
| `model_prob` | 0–1 | Our own `p_fair` from the brains, when available. **Side-relative, like `price`:** a `"side": "yes"` agent sees P(yes), a `"side": "no"` agent sees P(no) = 1 − P(yes) |
| `edge` | float | `model_prob - price`, so always "our probability minus what we pay" on either side. Bridges the DSL to the existing v4 brains |
| `unrealized_pnl_pct` | float | Position-level, exits only |
| `hold_to_settlement` | bool | Terminal exit |

Operators: `lt`, `lte`, `gt`, `gte`, `eq`, `between`.
Combinators: `all` (AND), `any` (OR), nestable one level.

`sizing.method`: `kelly` | `fixed` | `fraction_of_bankroll`.
Kelly reuses `wc/lib/kelly.py` — not a second implementation.

### Validation rules

Rejected at load, never at trade time:

- Unknown signal, operator, or field
- `side` not in `{yes, no}`
- Missing `caps` — **caps are mandatory**, consistent with `wc/lib/caps.py`
  failing closed. A strategy with no cap is not a strategy
- `kelly.fraction` outside `(0, 1]`
- Empty `entry` (would trade everything)
- Any numeric out of its documented range

---

## 3. The interpreter

`wc/backtest/interpret.py`. The only code that executes a strategy, and
therefore the code that gets reviewed hardest.

```python
def evaluate(conditions, ctx) -> bool     # walk the condition tree
def should_enter(spec, ctx) -> bool
def should_exit(spec, ctx, position) -> bool
def size(spec, ctx, bankroll) -> float    # delegates to wc/lib/kelly.py
```

`ctx` is the bar view from the engine — trailing data only, no dataframe, no
outcome. Pure functions, no I/O, no network, no clock. Fully unit-testable
without touching Kalshi.

The same interpreter serves backtest, paper, and live. A strategy that
backtested cannot behave differently in production, because there is no second
implementation for it to diverge into.

---

## 4. Baseline strategy — written by hand, before any LLM

`strategies/sell-cheap-longshots.json`, exactly the spec in §2.

It exists to prove the schema can express something real, and to give the
engine a known target. It is also the best-evidenced edge in the literature:
sub-10¢ contracts lose 60%+ on average, and NO longshots beat YES longshots by
up to 64 percentage points across 300,000+ contracts. See
[06_RESEARCH.md](06_RESEARCH.md).

A second baseline, `strategies/model-edge.json`, gates on `edge > threshold` —
this is the existing v4 system expressed as a spec, and it lets us backtest what
we already run.

---

## 5. Extending the schema

When a strategy cannot be expressed:

1. Write down the idea in English in the PR
2. Add the signal or operator to the vocabulary, with its range and how it is
   computed from the trailing view
3. Add a validation rule and a unit test
4. Only then write the strategy that uses it

Never bypass with an `eval`, a `custom_python` field, or a callback. The moment
the schema has an escape hatch, every argument in §1 collapses.

---

## 6. Definition of done

- [x] Schema documented, with every signal's range and computation
- [x] Validator rejects each malformed case in §2, with a useful message
- [x] Interpreter unit-tested against hand-computed conditions
- [x] `sell-cheap-longshots.json` validates and backtests end-to-end
- [x] `model-edge.json` reproduces the current live logic — priced by the same
      brains, through `wc/backtest/pricing.py`, with `--brains`
- [x] Caps proven mandatory — a spec without them fails to load
- [x] No-lookahead test passes through the interpreter path
- [x] Kelly delegates to `wc/lib/kelly.py`; a test stubs the library out to
      prove there is no second implementation
- [x] Every declared signal is actually computed — `oi_change_pct`,
      `volume_24h`, and `hold_to_settlement` were declared-but-inert, meaning a
      spec using them validated, ran, placed nothing, and gave no reason why
