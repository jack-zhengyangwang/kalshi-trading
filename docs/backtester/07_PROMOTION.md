# Promotion criteria

*Written before the first strategy was tested, deliberately. Criteria invented
after seeing results are not criteria, they are rationalisations — and the one
thing everybody does after a good backtest is find a reason it counts.*

---

## The path

```
BACKTEST          historical replay, wc/backtest        → gate 1
   ↓
FORWARD PAPER     the arena, real book, no money        → gate 2
   ↓
REAL MONEY        smallest size that is not a rounding error
```

Each gate is a **veto**, not a score. Failing any one line stops the promotion;
passing all of them earns the next stage, never the one after it. Nothing skips
a stage, including a strategy that looks obviously good.

---

## Gate 1 — backtest → paper

| # | Requirement | Why |
|---|---|---|
| 1.1 | `quality.py` reports **no blocking issues** on the window | A confident number from bad data is worse than no number |
| 1.2 | **≥100 settled trades** — settled, not force-liquidated | Below that, P&L is noise. `n_liquidated_at_end / n_trades < 0.2` |
| 1.3 | **Out-of-sample net P&L > 0** | The walk-forward number, not the in-sample one |
| 1.4 | **Held-out period net P&L > 0** | Touched once, at the end. The only genuinely clean read |
| 1.5 | **`latency_sensitivity < 2.0`** | Above that the "edge" is mostly the strategy filling at its own trigger price |
| 1.6 | **Brier beats the market-implied baseline** | A strategy can be profitable through sizing luck while being badly calibrated. That does not survive a new regime |
| 1.7 | Positive in **≥2 of the walk-forward windows** | One good window and three flat ones is one lucky window |

**A `--brains` backtest cannot satisfy gate 1 on its own.** Those runs carry
model-fitted lookahead (today's Elo knows who won — see 02_ENGINE). They are
useful for *ranking* ideas and for sanity-checking that the live logic is
expressible, never as promotion evidence. A `model_prob`-dependent strategy is
promoted on **gate 2 alone**, with gate 1 downgraded to "did not obviously fail".

---

## Gate 2 — paper → real money

The arena is the real check. A backtest says a strategy would have worked; the
arena says it works against the book that actually exists, with the spreads and
fills that actually happen.

| # | Requirement | Why |
|---|---|---|
| 2.1 | **≥30 calendar days** in the arena | Shorter than that and you have tested one week of one league's form |
| 2.2 | **≥100 settled bets** | Same reason as 1.2 |
| 2.3 | **Net positive after fees** | Gross-positive/net-negative is the most common failure mode at these prices |
| 2.4 | **Calibration slope within [0.8, 1.2]** | The probabilities have to mean something, or sizing is guesswork |
| 2.5 | **Beats the do-nothing baseline** — taking the market price at face value | If the market is already right, there is nothing to trade |
| 2.6 | **Survives the selection test** (below) | The one that catches the most false positives |
| 2.7 | Max drawdown **< 25%** of its paper bankroll | Something you could actually hold through |

### 2.6, the selection test — read this one

"Promote the best performing portfolio" is the single most dangerous sentence in
this document, because **the best of N agents always looks good, even when none
of them has any edge.** Run 16 strategies on pure noise and one will finish up
20%; promoting it promotes randomness.

The fix is cheap and non-negotiable:

1. **Pick** the candidate on window A — the first two-thirds of the arena period.
2. **Validate** it on window B — the final third — with **no re-selection**.
   Whatever was chosen in step 1 is what gets scored. No swapping to whoever
   won window B.
3. The candidate must be **net positive in window B on its own**.
4. Record **how many agents were in contention** in the promotion note. Picking
   1 of 16 is a much weaker claim than picking 1 of 2, and the number has to be
   visible to whoever reads the decision later.

If the window-A winner loses in window B, **promote nothing this round**. That
outcome is information: it says the ranking was noise, which is exactly what you
wanted to find out before wiring money to it.

---

## Gate 3 — running with real money

Promotion is not a finish line, it is a smaller experiment.

| # | Requirement |
|---|---|
| 3.1 | Start at **20% of intended size**. Scale only after 3.4 |
| 3.2 | `guard.py` running, with `max_loss_dollars` set to a number you would shrug at |
| 3.3 | All three caps present and positive — `wc/lib/caps.py` fails closed, so a missing cap blocks trading rather than removing the limit |
| 3.4 | **Review at 30 days or 100 settled bets**, whichever comes later |
| 3.5 | **Automatic demotion:** back to paper if real-money net P&L is negative at review, or if drawdown passes 25%, or if calibration slope leaves [0.7, 1.3] |
| 3.6 | Paper keeps running for the promoted strategy, so live and paper can be compared |

**3.6 is the cheapest diagnostic you will ever have.** When live and paper
diverge on the same signals, the difference is execution — fills, latency,
spread — not the strategy. That tells you which of the two to fix.

---

## Per category, not overall

The arena runs strategists per market category (`winner`, `game_lines`,
`game_props`, `knockout`). Promote **per category**: a strategy that works on
match winners has demonstrated nothing about corners.

Each category's promotion is its own selection test, with its own N recorded.

---

## Writing the decision down

Every promotion appends one row to `data/promotions.jsonl`:

```json
{"ts": 1789000000, "strategy": "model-edge", "category": "winner",
 "from": "paper", "to": "real", "agents_in_contention": 16,
 "window_a": [...], "window_b": [...],
 "window_b_net": 42.10, "settled_bets": 137, "brier": 0.198,
 "latency_sensitivity": 1.3, "size_fraction": 0.2, "note": "..."}
```

Not ceremony. When a promoted strategy loses money three months later, the only
question that matters is *what did we know when we promoted it* — and memory
will supply a flattering answer if the file does not supply the real one.
