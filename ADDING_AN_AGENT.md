# Adding your own agent

In **v2** an agent isn't a hand-coded class — it's a **point in a parameter space** that the population evolves. So "adding an agent" means one of three things, easiest first.

> Working with the legacy **v1** arena instead? There, an agent *is* a `Strategy` subclass — see [`v1/strategies/example_team.py`](v1/strategies/example_team.py) and the steps at the bottom of this file.

---

## Option A — seed a new archetype (easiest)

A team is just a `params` dict over `strategy_v2.PARAM_SPACE`. Add a named seed to `seed_params()` in `strategy_v2.py`, and it joins every category's starting population; evolution then refines (or culls) it.

```python
# strategy_v2.py → seed_params()
"value_contrarian": {            # only bet when the model strongly disagrees, then hold
    "pregame": 1, "inplay": 0,
    "min_edge": 0.05, "edge_haircut": 0.02,
    "kelly_fraction": 0.20, "min_entry_price": 0.05,
    "exit_stop_fair": 0.10,      # STOP only (hold-to-resolution)
},
```

**The evolvable params** (`PARAM_SPACE`): entry gates `pregame` / `inplay` / `scalp` (+`scalp_min_minute`/`scalp_min_prob`), `min_edge`, `edge_haircut`, `kelly_fraction`, `min_entry_price`, and exits `exit_take_profit_pct` / `exit_sell_margin` / `exit_stop_fair` / `exit_fraction`. All are mutated/crossed during evolution and clipped to bounds.

Run it and watch it compete:
```bash
python3 arena_v2.py --reset && python3 arena_v2.py --replay 30
python3 arena_v2.py --status
```

---

## Option B — add a new *behavior* (a new lever)

If your idea needs a knob that doesn't exist yet:

1. **Add the param** to `PARAM_SPACE` in `strategy_v2.py` (with `(low, high)` bounds).
2. **Use it** in `Strategist.entry_size()` (sizing/timing) or `Strategist.exit_decision()` (exits). Sizing reuses `group/kelly.py`; exits delegate to `group/exit_rules.py` (the same logic the live position-manager uses, so paper and live never drift).

The new lever is automatically part of mutation/crossover, so the population starts exploring it immediately.

---

## Option C — add a new *market* to price

To trade a Kalshi series the system doesn't cover yet:

1. **Register it** in `categories_v2.json` under a super-category's `series`.
2. **Parse it** — add the series prefix to `SERIES_SPEC` and the parser in `markets_v2.py::parse_market_v2`.
3. **Price it** — add the analytic in `markets_v2.py::fair_yes_v2` (the existing legs show the patterns: Poisson tails for totals, the goal-difference grid for spreads, negative-binomial for corners, a Poisson race for first-to-score, the exact-score grid for correct score).

The Brain (`brain_v2.py`) then prices it for free via `data_pfair`, and — if you turn the LLM on — `llm_pfair`. The **correct-score** and **corners** markets are full worked examples of exactly this.

---

## How the population uses your agent
Each cycle, every team sizes bets on its edges, exits live, and is scored by **fitness = realized P&L / √staked**. Teams with enough resolved bets that sit at the bottom get **culled**; the top get **bred** (crossover + mutate). So a good seed propagates its params into the next generation, and a bad one earns its way out — you don't have to tune it by hand.

---

## Legacy v1 (class-based)
In `v1/`, an agent is a `Strategy` subclass implementing `entry_dollars(ctx)` (and optionally `exit_decision`), registered in `v1/.../group/strategy.py`'s `_REGISTRY` and given params in `v1/tournament_config.json`. The commented template is [`v1/strategies/example_team.py`](v1/strategies/example_team.py). v2 supersedes this with the evolving population above.
