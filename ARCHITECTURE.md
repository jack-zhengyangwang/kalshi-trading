# Architecture — the thinking behind the design

This document explains *why* the system is shaped the way it is. The one-sentence
version: **separate "what is it worth?" from "what do I do about it?", price every
market off one honest model, size by edge, and let realized outcomes tune everything.**

> **These are the shared principles behind both generations.** The current system,
> **v2**, generalizes every one of them: the single fair value becomes a per-category
> **stacker** the Brain re-tunes by accuracy; the fixed strategies become an **evolving
> population**; the few markets become the **full match surface**. For v2's concrete
> architecture see the [README](README.md#v2--the-current-system) and the full
> [BUILD_PLAN_V2.md](BUILD_PLAN_V2.md). The reasoning below is what v2 is built on.

---

## 1. Two layers: the Brain vs. the agent teams

The most important design decision is the split:

- **The Brain** answers *what is this market worth?* — one shared fair-value estimate per market.
- **The agent teams** answer *what do I do about it?* — entry timing, sizing, exits.

Why split them? Because most "strategies" conflate a *view* with an *action*, and you can't tell which one was right or wrong when you lose. By forcing every team to consume the **same** fair value, the only variable left is the *betting behavior* — so the tournament cleanly measures which behavior makes money, not which got lucky on a price.

---

## 2. The Brain — fair value (`group/brain.py`, `group/markets.py`)

### Pre-game: a blend of three independent sources
```
p_fair = weighted_blend( p_llm , p_model , p_market )
```
1. **LLM** — Claude reads the matchup + price and estimates the probability. Good at context the model can't see (injuries, narrative).
2. **Stats model** — a multinomial logistic regression on team **ELO** → P(away / draw / home). Calibrated, fast, unbiased on average.
3. **Market** — the orderbook mid. The crowd's view; usually efficient.

The weights are **learned** (Agent 3 shifts them toward whichever source has been most accurate). Disagreement between the sources becomes the **uncertainty `sigma`**, which gives a **conservative lower bound** `p_fair_lo = p_fair − sigma`. Teams that want to be careful bet the lower bound; aggressive teams bet the mean.

### One goal model → every derived market (`markets.py`)
The winner market and all the "adjacent" markets (spread, total goals, team totals, both-teams-to-score, exact score) are priced from the **same** Poisson scoring model:
```
home goals ~ Poisson(mu_home),  away goals ~ Poisson(mu_away)
```
where `mu` is split by ELO supremacy and (crucially) the **expected total is anchored to the market's implied total** for that game. So a spread fair value and a winner fair value can never silently disagree — they come from one joint distribution. This consistency is a feature: edge in one market is a real, model-coherent edge, not a pricing artifact.

### In-play: a live model that the pre-game model is blind to
Once the game starts, fair value is recomputed from the **current score + minutes left** using a Skellam (difference-of-Poisson) model, optionally scaled by **live dominance** (possession/shots/shots-on-target from ESPN box scores). A favorite losing late is correctly priced *low* — which is exactly where slow, manual markets misprice and the Momentum team strikes.

---

## 3. Edge and sizing (`group/kelly.py`)

A bet is made only when **edge = p_fair − ask > min_edge** (you pay the ask, so edge is measured against it — the honest, conservative comparison). Size is **fractional Kelly with a time-value discount**:
```
f   = edge / (1 − ask)              # Kelly fraction for a binary contract
bet = f × kelly_fraction × e^(−tvm_rate · weeks) × balance     # capped per bet
```
- `kelly_fraction` (< 1) keeps sizing well below full Kelly — full Kelly is too aggressive for a noisy edge estimate.
- The time-value discount shrinks bets on far-off resolutions (capital is tied up).
- A per-bet dollar cap bounds the worst case even when the model is overconfident.

---

## 4. Exits — one shared decision (`group/exit_rules.py`)

The in-play exit logic is written **once** and used by both the paper tournament and the live position manager (`agents/keeper.py`), so they can never drift apart. Each poll it compares **live fair value** vs the current **bid** and decides:

- **STOP** — live fair below a floor → our outcome is dead, sell 100%.
- **OVERPRICED** — bid is above live fair by a margin → the market is overpaying us, scale out (the strongest +EV signal).
- **TAKE-PROFIT** — gain vs entry exceeds a threshold → bank some.
- **HOLD** — otherwise.

It's **re-armed**: each one-shot trigger fires once and only re-arms after the market cools, so a position scales out instead of dumping the whole thing on one poll.

---

## 5. Learning — "Agent 3" (`agents/tournament_trainer.py`)

After every resolved game, the system diagnoses *which* part erred and fixes that part:

- **Pricing (shared, per category):** score each Brain source by **Brier** on the pooled bets; shift weight from the worst source to the best, and correct the culprit (nudge the LLM prompt, or calibrate the model). Gated by a minimum sample so a few lucky games can't overfit the blend.
- **Sizing (per team):** nudge `kelly_fraction` by recent **ROI** — winners size up, losers size down (faster).
- **Participation (per team):** if a team sat idle while real edge was on the table, lower its `min_edge` and raise its Kelly — the "regret of missed profit."

All nudges are **small and bounded** — it adapts gradually from clean, realized outcomes, never fits to history.

---

## 6. Categories and pricing modes (`categories.json`)

| Category | Kalshi series | Pricing | Notes |
|---|---|---|---|
| `winner` | `KXWCGAME` | LLM + model + market blend | the most-validated edge |
| `spread` | `KXWCSPREAD` | Poisson model vs market | margin view |
| `team_props` | `KXWCTEAMTOTAL` | Poisson model vs market | per-team goal totals |
| `game_events` | `KXWCTOTAL`, `KXWCBTTS` | Poisson model vs market | **market-anchored → near-zero edge by design** |
| `scoreline` | `KXWCSCORE` | exact-score grid | the specialist (below) |

A useful honesty check: `game_events` totals are anchored *to the market*, so the model ≈ the market and there's little edge to capture. A team "winning big" there is a sign of **variance, not skill** — exactly the kind of thing the tournament is built to expose rather than reward.

---

## A worked example: the scoreline specialist

This is a concrete walk-through of **how a new agent design is reasoned into existence** — the actual point of this repo.

**The tempting idea:** "bet several specific scorelines (1-0, 2-1, 2-0…); if any one hits, the payout dwarfs the stake — easy money."

**Why it's wrong (the math):** exact scores are mutually exclusive — at most one pays. By linearity of expectation, spreading a stake across scores has the *same* expected value as putting it all on one; it only lowers variance. And exact-score markets carry the **fattest overround** in sports (the grid's implied probabilities sum to 130–150%), so a coverage basket pays the vig on every leg → structurally **−EV**. The basket is profitable **iff** your selected lines are *underpriced on average* — i.e. only with a calibrated model that beats the posted price, line by line.

**So the design that *could* work:** price the exact-score grid with the **same** independent-Poisson model (`markets.exact_score_prob` — one cell of the joint goal grid), compare each line to its ask, and bet only where `model_prob > ask + edge`. Express it as one bespoke team that **combines** all four archetypes (pre-game edge entry + in-play momentum + late scalp + active exits), since exact-score opportunities are sparse and need every angle.

**The honest result:** on Kalshi the correct-score market turned out to be barely quoted (near-zero volume, enormous spreads), so the model rarely clears the bar — which is itself the answer: the edge there is thin and contested, and the *paper* arena let us learn that for free.

That arc — tempting idea → EV math → a model-gated redesign → a cheap paper test that tells the truth — is the process this whole system is built to support. Code: `group/markets.py::exact_score_prob`, `group/brain.py::evaluate_scoreline`, `group/strategy.py::ScorelineSpecialist`.
