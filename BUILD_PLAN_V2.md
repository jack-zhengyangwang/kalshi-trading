# Arena v2 — Build Plan

> **This is the design/roadmap document.** For the *current* architecture and how to
> run v2, start with the [README](README.md#v2--the-current-system); for the shared
> design principles see [ARCHITECTURE.md](ARCHITECTURE.md). In this repo, v2 lives at
> the root and the original 16-team arena referenced below now lives in [`v1/`](v1/).

A full-surface Kalshi soccer betting arena. Tests one hypothesis: **there is
genuine, harvestable edge across the entire Kalshi soccer market surface**, not
just the match winner. The current 16-team arena (`arena.py`) stays running as
the **original track**; v2 is built **in parallel** and the original is
**disabled only when v2 is activated to the cloud** (to save droplet space).

> Status: design approved 2026-06-16. Phase 0 in progress.
> The live wallet stays OFF. Everything here is paper until a category's winning
> team is explicitly promoted through the patch-panel.

---

## 0. What changes vs. the original arena

| | Original arena (`arena.py`) | Arena v2 |
|---|---|---|
| Markets | winner + spread + team_props + game_events | **4 super-categories** (below), full leg coverage |
| Brain | per-leg blend (LLM+model+market), priced one leg at a time | **one Brain per super-category**, emits `p_fair` for every leg from a shared per-game prior |
| LLM calls | per leg / per poll | **one call per game per category** (priors only) → all legs derived analytically |
| Data model | multinomial logistic on ELO | **GBT + bivariate-Poisson**, trained on a multi-league corpus |
| Combiner | static-ish brain_weights, Trainer reweights | **Brain Assistant** — online stacker re-tuned each resolved round |
| Teams | fixed A/B/C/D presets | **evolving population** over a continuous strategy-parameter space (A/B/C/D are seeds) |
| Agent shape | one strategy object | **strategist + executor** pair (executor is zero-LLM) |
| Promotion | winner-only real path | per-category **logical sockets** over one shared Kalshi balance |

---

## 1. The four super-categories

| Super-category | Legs | Build status |
|---|---|---|
| **Game Lines** | spread, total, team total, BTTS, 1st-half winner, first-to-score | **BUILD FIRST** — mostly reuses the ELO→Poisson goals model. spread/total/team-total/BTTS already in `group/markets.py`; 1H-winner = minute-scaled, first-to-score = Poisson race (new, analytic) |
| **Game Props** | correct score, total corners, team corners, first team to score | **BUILD FIRST** — correct score already built (`scoreline`, paused → un-pause); corners need a new model + corner data; first-team-to-score = Poisson race |
| **Events** | "will the announcer say word X" | **EXPERIMENT** — its own brain (LLM + historical base rate, no Poisson). Isolated, flagged experimental |
| **Player Props** | player to score / assist / first to score | **STUBBED EMPTY** — scaffold the category, disabled until a player data source + per-player model exists |

---

## 2. Brain v2

One Brain per super-category. Signature: take a feature vector at a point in
time → emit `p_fair` per leg.

```
features (minute, score, corners_so_far, rankings, team/player history, …)
   │
   ├── LLM        ── priors (1 call / game / category): goal split, corner rate, …
   ├── data model ── GBT + bivariate-Poisson ── p_data per leg
   │
   └── linear stacker (logit space) ── p_fair per leg
```

- **LLM produces priors, not prices.** One call per game per category yields the
  per-game parameters (expected total goals split, expected corners, etc.); the
  data model + `markets.py`-style analytics derive *every* leg from those. This
  is the single change that makes full-surface coverage affordable under the
  Anthropic rate/token limit.
- **Data model = GBT + bivariate-Poisson** (NOT a neural net yet — data volume,
  not model capacity, is the binding constraint; NN overfits a single
  tournament). Trained on a large multi-league club corpus; WC inputs supply
  team strength (club-ELO fuzzy match, same as today).
- **Combiner = linear in logit space** to start (a logistic-regression stacker).
  Upgrade to non-linear only when it beats linear out-of-sample.

### Brain Assistant (the meta-learner)
After each **resolved round** (a batch of bets across the Brain's teams that has
settled), compare the realized outcome against LLM-`p_fair`, data-`p_fair`, and
combined-`p_fair`; re-tune the stacker weights and per-source calibration.
Guardrails: no re-weight below `MIN_PRICE_SAMPLE` resolved bets; shrink toward
the prior on thin samples. Generalizes the existing inverse-Brier Trainer.

---

## 3. Payoff note (drives bet selection + sizing)

Payoff is set by the **price you pay**, not your probability. A YES contract
bought at price `p` settles $1/$0, so payoff odds `b = (1−p)/p` and Kelly
fraction `f* = (p_fair − p)/(1 − p)`. Two legs with the same `p_fair` but
different ask → different edge, payoff, and stake. Consequences:
- Rank candidates by **Kelly growth** (edge adjusted for variance), not `p_fair`.
- Cheap legs are simultaneously highest-payoff and highest-ruin-risk from
  miscalibration → keep the `min_entry_price` floor (penny-longshot guard).

---

## 4. Agents: strategist + executor, evolving population

- **Strategist** — policy: given `p_fair`, price/payoff, time, bankroll → how
  much + when (aggressive / conservative / scalp / momentum live here).
- **Executor** — mechanism: scan markets/positions, fire signals, place/cancel
  after the strategist confirms. **Zero LLM tokens.**
- **Evolving population** — replace hard-coded A/B/C/D with a continuous
  strategy-parameter space (entry basis, min_edge, kelly_fraction, entry-time
  window, scalp thresholds, exit thresholds). A/B/C/D become seeds. New teams
  generate themselves via **evolutionary / population-based training** (winners
  breed + mutate; losers die). LLM-proposed strategies are a later creative layer.

---

## 5. Data sources

| Need | Source |
|---|---|
| Historical corners (training) | **football-data.co.uk** — `HC`/`AC` + shots, big-5 + more leagues, from 2000/01. Mirror: jokecamp/FootballData |
| Half-split corners (optional, in-play timing) | FootyStats CSV |
| Live corners (in-play scalpers) | **ESPN** `live_feed.get_game_stats()` → per-team `corners` (already wired, `wonCorners`) |
| Team strength | existing club-ELO (`models/club_elo.json`) + ratings, fuzzy-matched |

Gap: no large *international* corner set — train the corner-*generating process*
on the club corpus, feed WC team strength as inputs.

---

## 6. API discipline (Phase 0 prerequisite)

- **Kalshi:** migrate market-data reads toward the **WebSocket** feed; add 429
  retry/backoff + intra-cycle throttle (<5 req/s); cache market lists. Root cause
  of past 429s is the per-cycle burst, not cadence.
- **Anthropic:** one LLM call per game per category; prompt-cache static context;
  Batch API for non-latency-sensitive pre-game pricing; request a higher tier.
- **No multiple Kalshi accounts** — one KYC'd account per person (ToS; multiple
  risks suspension/forfeiture). Multiple API keys share one account's limit +
  balance, so they don't help. Architectural call-reduction + WebSocket is the fix.

---

## 7. Promotion (the extension cord)

Per-category **logical sockets** (the existing `switchboard.json` +
`group/promotion.py` + `live_promote.py` patch-panel), NOT separate wallets —
Kalshi is one account = one balance = one netting space. So promotion needs a
**portfolio allocator** over the shared balance (per-category capital + master
cap), enforced by the switchboard fuses. Winning team per category plugs into
its socket; the rest stay paper.

---

## 8. Phases

- **Phase 0 — Data + API foundation** *(in progress)*
  - `models/fetch_corners.py` → `models/corners_dataset.csv` (football-data.co.uk)
  - `models/train_corners.py` → corner-rate model (bivariate-Poisson + GBT)
  - Kalshi WebSocket + 429 backoff/throttle; LLM-once-per-game refactor
- **Phase 1 — Brain v2 for Game Lines** (spread/total/team-total/BTTS reuse;
  add 1H-winner + first-to-score; LLM-priors + GBT/Poisson + linear stacker +
  Brain Assistant)
- **Phase 2 — Brain v2 for Game Props** (un-pause correct score; corners model;
  first-team-to-score)
- **Phase 3 — Agent restructure** (strategist/executor split; evolutionary
  population)
- **Phase 4 — Events** (own LLM+base-rate brain, experimental) + **Player Props**
  scaffold (disabled)
- **Phase 5 — Promotion** (logical sockets + portfolio allocator over one balance)

### Parallel-track placement
v2 lives in new modules alongside the originals (`brain_v2.py`,
`categories_v2.json`, `arena_v2.py`), sharing `kalshi_client` / `live_feed` /
`group/markets.py`, so `arena.py` is untouched until cutover. On cloud
activation: deploy v2, disable the original arena cron, free its state/space.
