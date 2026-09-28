# Finding 04: Rule-based strategy ideas (literature review, 2026-09-28)

Candidates only; **nothing here has been tested on our data yet.** The grades are judgements
from a web review of papers and public analyses. The citations were collected by a research
agent and have **not been independently checked**, so read the source before relying on a number.
**Chart:** `venv/bin/python scripts/strategy_map.py`.

![Strategy ideas ranked](img/11_strategy_map.png)

## The big theme

- On Kalshi, the best evidence says the edge is in **how** you trade, not **what** you buy.
- **Makers** (resting orders) beat **takers** (paying the ask). That matches our own results: prices are calibrated, and a taker pays about 5.5¢ to trade a 1¢ move.

## The 12 ideas

| # | Rule | Evidence | Our data? |
|---|---|---|---|
| 1 | Post resting bids (maker), never take; e.g. favourites ≥50¢, 1–24h before kickoff | **A** Kalshi: makers +1.1% vs takers −1.1% (Becker 2026, 43.6M sports trades); makers buying ≥50¢ +2.6% after fees (Bürgi, Deng & Whelan 2026) | Partly: fills can only be estimated from hourly bars |
| 2 | Sell longshots: buy NO when YES ≤10¢ | **B** Kalshi-wide longshots lose heavily, **but** Polymarket sports and Betfair soccer show no bias, or the reverse | Yes |
| 3 | Buy when the Kalshi ask < the de-vigged bookmaker probability − costs | **B** Kaunitz et al. 2017: +3.5% vs bookmakers; one analysis claims Kalshi is sharper than Pinnacle | Yes* (ESPN odds may be stale) |
| 4 | Three-way sum: buy all 3 YES if the asks + fees add up to < $1 | **B** Polymarket arbitrage studies (2026): it exists, but at small size | Yes (hourly bars may miss brief gaps) |
| 5 | Kalshi vs Polymarket, same match | **C** practitioner posts, non-sports | No: needs Polymarket data |
| 6 | Draw when home ≈ away (within 10¢) and draw ≤28¢ | **B–C** Betfair "splitting" bias; draws positive only in data-mined subgroups | Yes (overfitting risk) |
| 7 | Buy home ≤24¢ / away ≤14¢ | **C** Betfair EPL, +52% on only 175 bets | Yes: test it against #2 |
| 8 | After a surprise goal, buy the scorer's side | **B** Betfair (Choi & Hui 2014); the effect fades within minutes | No: needs in-play goal times |
| 9 | Last 15 min: buy NO on the trailing team | **C** | No |
| 10 | Fade popular clubs | **C**: studies disagree on the direction | Partly |
| 11 | At neutral venues, fade the listed "home" team | **C**: only after a structural change, now gone | Yes |
| 12 | Kalshi Liquidity Incentive Program: rewards for resting two-sided quotes | **A** (Kalshi's own rules, until Jan 2027); a rebate that adds to #1, not an edge by itself | Check whether soccer markets qualify |

## Skeptic's notes

- Most published biases are measured **before costs**, and bookmaker studies include a 3–5% margin.
- **The direction of the longshot bias in soccer is unresolved.** Kalshi overall says sell longshots; Betfair soccer says the opposite. Test #2 against #7, split by league.
- The lineup announcement (~1h before kickoff) was **dropped**: no academic evidence was found, and it would need a lineup feed and fast execution.
- Our data is **hourly bars**, so anything that needs exact fills (#1, #4) can only be estimated. Trade-level data would make those tests credible.

## Suggested first batch (to be written down before testing)

1. **#4 Three-way sum:** a quick scan. Is there any free money at all?
2. **#2 vs #7 longshots:** one test settles which way the bias runs on Kalshi soccer.
3. **#3 Bookmaker gap:** if ESPN's odds are usable.
4. **#1 Maker:** estimate fill rates from the hourly bid/ask path. A proper test needs trade-level data.

## Sources

- Bürgi, Deng & Whelan 2026 — https://www.karlwhelan.com/Papers/Kalshi.pdf
- Becker 2026 — https://www.jbecker.dev/research/prediction-market-microstructure
- Cardozo & Rivero 2026 — https://arxiv.org/pdf/2609.12878
- Angelini, De Angelis & Singleton 2022 — https://www.sciencedirect.com/science/article/abs/pii/S0169207021000996
- Choi & Hui 2014 — https://www.sciencedirect.com/science/article/abs/pii/S0167268114000481
- Croxson & Reade 2014 — https://onlinelibrary.wiley.com/doi/abs/10.1111/ecoj.12033
- Kaunitz, Zhong & Kreiner 2017 — https://arxiv.org/abs/1710.02824
- ImpliedScore 2026 — https://impliedscore.com/market-calibration/
- Fischer & Haucap 2020 — https://arxiv.org/html/2008.05417
- Forrest & Simmons 2008 — https://eprints.lancs.ac.uk/id/eprint/44748/1/10.pdf
- Polymarket NBA arbitrage 2026 — https://arxiv.org/html/2605.00864v1
- Executable arbitrage 2026 — https://arxiv.org/pdf/2608.00666
- Kalshi LIP — https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program
- SportsBookISH 2026 — https://sportsbookish.com/research/why-mid-game-kalshi-lines-lag
