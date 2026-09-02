# Volatility Premium (Tail-Risk Overpricing)

**Status:** proposed (2026-08-04)
**Related:** [[boring-equilibrium]], [[draw-underpricing]]

## The edge

Tail-risk events — longshots, catastrophe outcomes, extreme results — are systematically overpriced. Participants overpay for lottery-ticket upside and disaster insurance. Selling the tail (betting NO on overpriced longshot YES contracts, or selling the overpriced side of binary event markets) collects this premium.

This is the most cross-domain-portable edge in the library: it has been documented in equities (variance risk premium, OTM put overpricing), sports betting (favorite-longshot bias), and prediction markets (catastrophe contract overpricing). The mechanism is the same everywhere: fear and hope are inelastic demands.

## Mechanism

1. **Lottery-ticket psychology:** A $1 bet that could pay $50 is more exciting than a $50 bet that could pay $1, even if the former has 3% EV and the latter has 5% EV. The skew is what sells.
2. **Fear-driven hedging:** Participants buy "No" on scary events (recession, market crash, political crisis) at any price. The demand for certainty is inelastic.
3. **Bookmaker structure:** Sportsbooks widen the spread on longshots because adverse selection is higher on the longshot side (informed bettors target them). The price drifts above fair value.

## Counterparty

- **Lottery-ticket buyers:** Recreational bettors chasing the dream of a big payout on a small stake.
- **Fearful hedgers:** Buying protection against outcomes they dread, regardless of cost.
- **Sportsbooks:** Quoting wide on longshots by structural necessity, passing the cost to takers.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer Kalshi | Longshot scorelines (4-0, 5-1), red cards, "Will there be a penalty?" — YES overpriced. Also: heavy underdog winner legs. |
| Sports betting | Longshot winners, exact-score bets, parlay bets (compounding the bias) |
| Prediction markets | Catastrophe contracts ("Will X country default?"), "black swan" events |
| Equities | Deep OTM puts, VIX futures contango, selling strangles on range-bound names |

## Quantification

For each leg, compute the market-implied probability and compare against the structural model:
```
edge = p_market - p_fair
```
Group legs by `p_fair` decile. Hypothesis: the lowest `p_fair` decile (longshots) has the largest positive `edge` (market overprices it). The relationship should be monotonic: as probability decreases, overpricing increases.

## Test plan

1. **Data:** All Kalshi legs across all market types. Group by p_fair from Brain v2.
2. **Method:** Decile analysis of `edge` by `p_fair`. Regression: `edge ~ p_fair` expecting negative coefficient (lower probability → more overpricing).
3. **Control:** Must exclude legs where Brain v2 is known to be weak (novel/LLM-only types). Use only legs with structural model coverage.
4. **Success metric:** Lowest p_fair decile shows mean edge > 3pp (market overpriced). Monotonic negative relationship across deciles. p < 0.05.
