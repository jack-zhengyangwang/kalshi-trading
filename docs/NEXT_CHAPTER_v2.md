# Next chapter (v2): predict better, hold to settlement

**Goal:** make money by being **right more often than the price**, not by how we trade.
Buy only when our model's probability beats the price by enough, **hold to settlement**, and
sell early only in rare cases where the market overpays.

Why this, after v1.0 ([report](REPORT_v1.0.md)):
- Prices are accurate, so **the only edge left is information the price doesn't have yet**.
- Holding pays the fee **once**; trading pays the spread and fees twice.
- Posting orders cuts costs by about 3.5¢, so v2 posts orders first.

## The decision flow

```mermaid
flowchart LR
    A[Point-in-time features<br/>form, Elo, table, schedule,<br/>league, odds*] --> B[Model<br/>p̂ = our probability]
    M[Kalshi price] --> C{p̂ − price − cost<br/>≥ margin?}
    B --> C
    C -- no --> X[Do nothing]
    C -- yes --> D[Post a maker bid<br/>take only if the edge is large]
    D --> E[Hold to settlement]
    E --> F{Bid − fee ≥ p̂ + 15¢?<br/>rare take-profit}
    F -- yes --> G[Sell]
    F -- no --> H[Settle: $1 or $0]
```
*odds = bookmaker odds, if we start collecting them (currently paused).

## The bar a model must clear (in this order, before any money)

| Gate | Test | Pass |
|---|---|---|
| 1. **Beats the market's accuracy?** | On held-out games: Brier score and log loss of p̂ vs the market price | p̂ better, or at least… |
| 2. **Adds information?** | Regress the outcome on logit(market) + logit(p̂) | Model coefficient > 0 at 99%. **This is the key test**: a model can be worse than the market overall and still be useful blended in |
| 3. **Profitable when it disagrees?** | Pre-registered bets where p̂ − price − cost ≥ margin; hold to settlement | 99% interval of net ¢ per contract above 0, **≥ 50 bets**, beats random entry |
| 4. **Holds up forward?** | Paper trading, rules frozen | ≥ 30 days, ≥ 100 bets, same bar |

Gate 1 is hard: the market's own Brier score is what we measured in Finding 01. **Gate 2 is
where a real edge would first show up.**

## Roadmap

| Phase | What | Output |
|---|---|---|
| **0. Dataset** | One table: game × leg × entry time → point-in-time features, price, result. Leakage guard: no feature newer than the entry time. | `wc/research/dataset.py` + tests |
| **1. Baselines** | Market price (the benchmark to beat); Elo **recalibrated** (it was overconfident); Dixon-Coles Poisson; "market + small model nudge" | Gate 1–2 scorecard, one chart |
| **2. Where the market might be weak** | Split gates 1–2 by league tier (thin leagues), time to kickoff, early season, draws, schedule congestion | Where, if anywhere, the model adds information |
| **3. Betting rule** | Margin, fractional-Kelly size, maker-first order, the rare take-profit rule (bid − fee ≥ p̂ + 15¢) | A pre-registration doc like Batch 1 |
| **4. Test, then forward** | One test run, then paper trading | Pass or fail, reported like v1.0 |

## Decisions for Jack before Phase 0

1. **Data:** ✅ **laptop data only for now; no new collection** (Jack, 2026-09-28). The droplet's Kalshi store (~5× more Kalshi games) can be added to Table B later, if the model looks promising on Table A. Work happens on branch `feat/v2-dataset`.
2. **Bookmaker odds:** probably the strongest single predictor, and also the strongest benchmark. **Restart forward collection (#3), or find a historical source?**
3. **LLM as a predictor:** the desk-manager setup could produce a p̂ per game as one more input. Worth testing in Phase 1?

## Phase 0 in detail: the dataset (laptop data)

**One model, two tables:**

```
 TABLE A: learn + practice exam              TABLE B: final exam
 soccer.db, 2021 → 2026, ~54k matches        Kalshi legs, Jul → Sep 2026 (~2,200 games)
 features known BEFORE kickoff − 1h          same features, as of each entry time (24h / 6h / 1h)
 target: home / draw / away                  target: did this leg win
 benchmark: bookmaker odds (margin removed)  benchmark: Kalshi bid / ask / mid
```

- **Features are rebuilt in one forward pass over `matches`**, not read from `facts`:
  - `facts` stamps each value at the *end* of the match it came from (kickoff + 2h), holding the state *before* that match.
  - So a strict "known before the cutoff" rule would always be **one match stale**. Safe, but it loses the latest result.
  - Rebuilding is point-in-time by construction for **any** cutoff, and fully tested.
- **Features:**
  - Elo (both teams, difference, implied home probability)
  - matches seen, form (points per game, last 5)
  - goals and shots for/against (last 5)
  - home win rate at home / away win rate away
  - rest days
  - head-to-head (games, home side's win rate)
  - league draw rate and home win rate over the previous 365 days
- **Not features:** bookmaker odds and Kalshi prices. They are the benchmarks.
- **No-peek rule:** a match updates the state only once its result is known (`known_at` < cutoff). Ties go to the query, so a result known exactly at the cutoff is excluded. Tested.
- **Kalshi → soccer.db join:** same team-name matcher as the kickoff table, kickoff within ±36h. Unmatched and ambiguous games are counted and reported, never silently dropped.
- **Splits (fixed now):**
  - Table A: train 2021–2024 · tune 2025 · test Jan–May 2026 (bookmaker odds end 2026-05-13)
  - Table B: final exam, Jul–Sep 2026
- **Output:** `data/research/v2_dataset.db` (tables `ds_matches`, `ds_kalshi`), regenerable and gitignored.
- **First bars to beat:** leg Brier score of the base rates, the bookmaker (Table A) and Kalshi (Table B).
- **Checked:** the odds are "96%+ Pinnacle closing" (data lake data dictionary), a hard benchmark.

## Phase 0 status ([Finding 08](findings/08_DATASET.md))

- ✅ **Phase 0 done.** Table A: 55,667 matches; the bar is **Pinnacle closing** leg Brier 0.1981 (Jan–May 2026).
- ✅ Table B: 457 of 967 Kalshi games (3,855 rows); Kalshi mid at 24h scores 0.1975. The Jun–Sep gap was filled from the ESPN cache and Kalshi settlements, both already on disk.
- **Next: Phase 1 baselines.**

## Phase 1 protocol (fixed 2026-09-29, before any model is trained)

**Models** (each predicts home / draw / away):

| | Model | Inputs | Chosen on the tune set (2025) |
|---|---|---|---|
| M0 | League base rates | lg_home, lg_draw | nothing |
| M1 | Elo, recalibrated | multinomial logistic regression on elo_diff, lg_home, lg_draw | nothing |
| M2 | All features, linear | multinomial logistic regression; missing values filled with the train median plus a missing-value flag; standardised | C ∈ {0.01, 0.1, 1} |
| M3 | All features, trees | HistGradientBoosting (handles missing values natively) | learning rate ∈ {0.03, 0.1}, max leaf nodes ∈ {15, 31} |

- **Fit on train (2021–2024), pick settings by 3-way log loss on tune (2025), refit on train + tune.**
- **Scored once on:**
  - Table A test: Jan–May 2026 rows with Pinnacle odds
  - Table B: Kalshi legs at 24h (6h and 1h reported too)
- **Gate 1 (more accurate?):** leg Brier of model minus market, with a 99% interval (bootstrap by game). Passes if the whole interval is **below 0**.
- **Gate 2 (adds information?):** per leg, a logistic regression of won on 1 + logit(market) + logit(model). Passes if the model's coefficient has a **99% interval above 0**.
- 3 models × 2 tables, so everything uses 99% intervals.
- Phase 1 involves **no betting**. It's about accuracy and information only.

## Phase 1 result ([Finding 09](findings/09_PHASE1_BASELINES.md))

- ❌ **0 of 4 models pass either gate.** Best: M2 (all features, linear), Brier 0.2027 vs Pinnacle 0.1981 and 0.2014 vs Kalshi 0.1975.
- Gate 2 against Pinnacle is a confident no (narrow intervals). Against Kalshi it's undecided (wide intervals, small Table B).
- **Options:** Phase 2 (split by league tier and entry time), a bigger Table B (droplet), or new information sources (paused).

## Phase 2 protocol (fixed 2026-09-29, before any Phase 2 run)

**Question:** is the market weaker somewhere: in some league tiers, or earlier before kickoff?

- **Model:** M2 (all features, logistic regression), **C = 0.01 frozen** from Phase 1, refit on train + tune. No new model search.
- **League tiers** (from `soccer.db` league names):
  - **T1 top:** EPL, LaLiga, SerieA, Bundesliga, Ligue1, UCL
  - **T3 lower divisions:** Championship, Bundesliga2, BrasileiroB, BrasileiroC, ArgNacionalB, SerieB, SerieC, LaLiga2, Ligue2, and any league whose name marks a second or third tier
  - **T2 everything else** (other first divisions)
- **Groups tested:**
  - Table A test (vs Pinnacle closing): T1, T2, T3
  - Table B (vs Kalshi): T1, T2, T3 × entry 24h / 6h / 1h
- **Gate 2 in each group:** the model's coefficient must have a 99% interval above 0 (bootstrap by game). Groups with fewer than 150 legs are reported but not judged.
- **Counts as a signal only if** a Table B tier passes at **≥ 2 entry times**. One pass among 12 groups is expected by chance.
- **Where it runs:** on the droplet, with its full Kalshi store, which gives a bigger Table B. `soccer.db` is copied up from the laptop. The kickoff table and caches are rebuilt there.
- Code is smoke-tested on the laptop using the **tune split only**, so the test sets are untouched before the real run.

## Phase 2 result ([Finding 10](findings/10_PHASE2_TIERS.md), full droplet data)

- ❌ **No signal:** 0 of 12 groups (3 tiers × Pinnacle + 3 entry times on Kalshi) pass Gate 2, and the market is more accurate in all 12.
- Table B grew to 1,363 games (~3,400 legs per entry time). Lower divisions aren't easier; entry time doesn't matter.
- T1 on Kalshi shows +0.4 at every entry time, but it's the same ~188 games, every interval includes 0, and it's −0.29 against Pinnacle.
- **Public-data models don't beat or add to this market.** What's left: new information sources (paused), or close v2.

## Settled: price-blind betting doesn't work ([Finding 07](findings/07_PRICE_VS_MODEL.md))

- Idea checked: "the model picks the winner and the stake; enter at any price".
- Profit per $ = model probability ÷ price − 1, so **the price is always in the answer**.
- Price and outcome are strongly related (market coefficient +1.30), and the Elo model added **nothing** once the price was known.
- **So the stake is set by the edge (p̂ − price), not by p̂ alone.**

## Rules carried over from v1.0

- Pre-register before testing; the git commit is the timestamp.
- Develop / test / forward split; 99% bar when several tests run at once; minimum sample sizes.
- Every chart regenerated from data; results pushed with short bullets.
