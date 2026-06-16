# Adding your own agent team

An agent team is a **Strategy**: it shares the Brain's fair value and only decides
*when to enter, how big, and when to exit.* Adding one is four small steps.

A complete, commented template lives in [`strategies/example_team.py`](strategies/example_team.py).

---

## Step 1 — write the Strategy subclass

Add a class to `group/strategy.py` (or import one). Implement `entry_dollars(ctx)`;
inherit `exit_decision` (param-driven) unless you want custom exit logic.

```python
class ValueContrarian(Strategy):
    """Bet only when the model strongly disagrees with the market, then hold."""
    def entry_dollars(self, ctx):
        r, ask = ctx.get("pregame"), ctx["ask"]
        if r is None or ask is None:
            return 0.0
        if abs(r["p_fair"] - r.get("p_fair_data", ask)) < self.p.get("disagree", 0.10):
            return 0.0                              # not enough disagreement → skip
        prob = _prob(r, self.p.get("entry_prob", "lo"))
        return self._kelly_dollars(prob, ask, ctx["balance"], ctx["tau_days"])
```

**The `ctx` your team receives each cycle:**

| key | meaning |
|---|---|
| `market` | the Kalshi market dict (`ticker`, `yes_sub_title`, bid/ask cents) |
| `pregame` | Brain fair value before kickoff (`p_fair`, `p_fair_lo`, `p_fair_data`, `sigma`) or `None` |
| `live` | Brain fair value during the game (`live_p_fair`, `sigma`) or `None` |
| `ask` / `bid` | current YES ask / bid in dollars |
| `balance` | cash to size against |
| `tau_days` | days to resolution (time-value discount) |
| `game_state` | `{status: pre\|in, minute, home_score, away_score, ...}` |

**Helpers from the base class:**
- `self._kelly_dollars(prob, ask, balance, tau_days, min_edge=None)` → $ to bet (0 = skip). Handles the edge check, Kelly fraction, time-value discount, and per-bet cap.
- `_prob(result, basis)` → pick the probability to bet on: `"mean"` (no haircut), `"lo_half"` (half haircut), `"lo"` (full conservative lower bound).

Return **dollars to bet** (0 = pass). The arena converts to contracts at the ask.

---

## Step 2 — register it

In `group/strategy.py`, add your class to the registry:

```python
_REGISTRY = {
    "aggressive_hold": AggressiveHold,
    ...
    "value_contrarian": ValueContrarian,   # ← your kind name
}
```

---

## Step 3 — give it params in `tournament_config.json`

```json
{
  "name": "E_value_contrarian",
  "kind": "value_contrarian",
  "params": {
    "kelly_fraction": 0.20, "min_edge": 0.03, "entry_prob": "lo",
    "disagree": 0.10, "max_bet_dollars": 10.0, "min_bet_dollars": 1.0,
    "exit_stop_fair": 0.10
  }
}
```

**Common params:** `kelly_fraction` (sizing aggressiveness), `min_edge` (min edge over the ask to bet), `entry_prob` (`mean`/`lo_half`/`lo`), `min_entry_price` (don't buy below this price), `max_bet_dollars`/`min_bet_dollars` (caps), and exit thresholds `exit_take_profit_pct`, `exit_sell_margin`, `exit_stop_fair`, `exit_fraction`. Plus any custom params your class reads from `self.p`.

---

## Step 4 — put it in the field

In `categories.json`, either let your team run in the default sweep, or give a category its own roster via a `strategies` override (this is how the scoreline specialist runs a single bespoke team):

```json
"scoreline": {
  "series": ["KXWCSCORE"], "pricing": "scoreline", "llm": false,
  "strategies": [{"kind": "scoreline_combo", "suffix": "S"}]
}
```

Then run it:
```bash
python3 arena.py --reset
```
Your team now competes in the standings. Tweak its params, re-run, and watch how it does — and remember **Agent 3 will keep tuning its `kelly_fraction` and `min_edge`** from its own realized results.

---

## Want a whole new *market* (not just a strategy)?
Add a category to `categories.json` with its Kalshi `series` and a `pricing` mode
(`winner`, `adjacent`, or `scoreline`), and — if it's a new kind of market — teach
the Brain to price it (see how `evaluate_market` / `evaluate_scoreline` work in
`group/brain.py` and the Poisson helpers in `group/markets.py`). The scoreline
specialist in [ARCHITECTURE.md](ARCHITECTURE.md#a-worked-example-the-scoreline-specialist)
is a full worked example of doing exactly this.
