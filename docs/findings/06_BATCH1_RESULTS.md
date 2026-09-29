# Batch 1 results

Rules: [05_PREREG_BATCH1.md](05_PREREG_BATCH1.md) (signed off 2026-09-28).
Code: `wc/research/batch1.py`. Raw results: `results/*.json`.

| # | Test | Status | Verdict |
|---|---|---|---|
| 4 | Three-way sum | **done** | **FAIL:** no gap in 860 games |
| 2 vs 7 | Longshot direction | **done** | **FAIL, both:** #2 never fired in test; #7 −7.8¢ |
| 1 | Maker orders | **next** | — |
| 3 | Bookmaker gap | **paused** (Jack, 2026-09-28: no droplet deploy for now) | — |

## #4 Three-way sum: FAIL

![Closest to arbitrage](img/13_threeway_closest.png)

- **0 of 860 games** had a gap after fees. Develop: 636 games (51,362 hourly checks). Test: 224 games (21,591 checks).
- **Before fees, 207 games (24%) dipped under $1**, by up to 4¢.
- **Taker fees add about 6¢** per set of three, so the cheapest any set ever cost was **$1.01**.
- **What it means:** no free money lasting an hour or more for a taker. Gaps that close within seconds can't be seen in hourly data.
- **Link to #1:** maker fees are about ¼ of taker fees. A maker who got filled on all three legs could capture some of those pre-fee gaps. The risk is getting filled on one or two legs but not all three.
- Rebuild: `python -m wc.research.batch1 three-way`, then `python scripts/batch1_charts.py`.

## #2 vs #7 Longshots: FAIL, both

![Longshot results](img/14_longshot_results.png)

- **Develop (636 games):** the pre-registered rule (highest mean) picked **#2 = NO when YES ≤5¢, 6h before kickoff**. That was only **10 bets**; the 5¢ settings are tiny because most sub-5¢ legs have no bid, so they don't count as quoted.
- **Test (331 games), run once:**
  - **#2 never fired: 0 trades.** A fail, and really "not testable at this threshold".
  - **#7 (buy longshots): −7.8¢ per contract** on 40 bets, 99% interval −16.1 to +5.3¢. It did worse than random entry (−3.2¢).
- **The broader #2 settings (≤15¢, about 120 bets) sit right at 0¢**, meaning longshots look fairly priced after costs.
- **What it means:**
  - Buying longshots (#7, the Betfair result) does not carry over to Kalshi. Its sign is negative in both periods.
  - Betting against them (#2) shows no edge at usable sizes.
  - Our best estimate is **no longshot bias big enough to beat the fee**.
- **Lesson for the pre-registration:** the selection rule should have required a minimum number of bets (say, n ≥ 50). Recorded here; any future batch must fix it **before** develop runs.
- Rebuild: `python -m wc.research.batch1 longshot develop`, then `... longshot test` (it refuses to run unless `results/batch1_frozen.json` is committed).
