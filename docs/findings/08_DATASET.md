# Finding 08: v2 Phase 0 dataset (laptop data)

Branch `feat/v2-dataset`. Build: `python -m wc.research.dataset` (≈3 s), then
`python scripts/phase0_charts.py`. Plan: [NEXT_CHAPTER_v2.md](../NEXT_CHAPTER_v2.md).

![Phase 0 dataset](img/17_phase0_dataset.png)

## Table A: learn + practice exam ✅

- **54,254 finished matches, 2021 → Sep 2026,** with **35,693 carrying bookmaker odds** (until 2026-05-13).
- Splits: train 38,099 · tune 10,958 · test 4,756 · Kalshi period 441.
- **23 point-in-time features per match,** rebuilt in one forward pass. 15 tests cover the no-peek rule, the ordering of results and cutoffs, venue-specific records, head-to-head orientation and the 365-day league window.
- **The first bar to beat (Jan–May 2026, 1,954 matches with odds), leg Brier score:**
  - base rates: **0.2156**
  - bookmaker, margin removed: **0.1981**
  - A model has to get below about **0.198** to beat the bookmaker, or show it **adds information** alongside them (Gate 2).

## Table B: final exam vs Kalshi ❌, a data gap

- **967** Kalshi games (Jul–Sep, ESPN kickoff), and **only 12** join a `soccer.db` match.
- **Cause:** `soccer.db` has almost no results after May 2026 (the data lake ends 2026-07-04; openfootball covers mainly the top European leagues). Kalshi's games are mostly lower leagues (Argentina Nacional B, Brasileiro B, …).
- So the model **can** learn these teams from 2021 to May, but it **can't see their Jun–Sep results**, and has nothing to be tested against.

## Ways to fill it using data already on the laptop (no new collection)

1. **The ESPN scoreboard cache** (`data/espn_cache/`, downloaded to build the kickoff table) holds **final scores for every ESPN soccer game** on about 90 days of Jul–Sep. That's results with goals, already on disk.
2. **Kalshi's own settled markets:** every settled game tells us home win, draw or away win, without goals. `derived.py` already uses this.
3. The droplet store (deferred): more Kalshi games, so more results and a bigger Table B.

**Recommendation:** add 1 as the main source and 2 as a fallback, mapping team names to `soccer.db` keys with the tested name matcher. Then re-join.

## Open check

- The bookmaker odds are "the first bookmaker per fixture" from the data lake. Which bookmaker, and opening or closing prices, is unknown. If they're opening odds, 0.198 is an **easy** bar and closing odds would be harder.
