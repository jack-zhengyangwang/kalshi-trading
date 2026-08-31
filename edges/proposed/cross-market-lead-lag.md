# Cross-Market Information Transfer (Lead-Lag)

**Status:** proposed (2026-08-04)
**Related:** [[excitement-spike-fading]], [[regime-detection]]

## The edge

When related markets share the same underlying information but reprice at different speeds, the faster market predicts the slower one. Information flows from the most liquid venue to the least. An automated system watching all related markets simultaneously can act on the laggard before it catches up.

## Mechanism

1. **Liquidity hierarchy:** Within a single game, the winner market is the most liquid. It reprices fastest when news arrives (goal, injury, red card). The totals market, corners market, and spread market lag — same game, same information, different repricing speed.
2. **Attention gradient:** Participants focus on the most salient market (who wins?) and update the derivative markets (how many goals?) only later or not at all.
3. **Cross-game spillover:** A result in one group game changes the advance probabilities for other teams in the same group. The group's winner market reprices immediately; the advance market for the other teams lags.

## Counterparty

- **Single-market watchers:** Most participants monitor one market at a time. Even sophisticated bettors can't watch every related market simultaneously.
- **Manual updaters:** Those who do understand the cross-market relationship may not act fast enough to capture the lag before it closes.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer Kalshi | Winner market moves → totals/spread/corners lag. Group result → advance probabilities for other teams lag. |
| Prediction markets | "Fed cuts rates" contract moves → "mortgage rates" / "housing starts" contracts lag. Election winner moves → policy-specific contracts lag. |
| Sports betting | Game line moves → player props lag (same team, same game, different attention). |
| Equities | Currency futures move before export-heavy equities. Commodities move before commodity-currency pairs (AUD ↔ iron ore, CAD ↔ oil). Index futures → individual stocks. |

## Quantification

For each pair of related markets (A = fast/leader, B = slow/laggard):
```
lag_correlation = corr(Δp_A at t, Δp_B at t+k) for k > 0
```

If B reprices fully within seconds, the edge is only harvestable with a fast automated system. If B takes minutes to reprice (common on thin Kalshi books), the edge is harvestable even with moderate polling frequency.

## Test plan

1. **Data:** Simultaneous price paths for related Kalshi markets. Winner market + totals + spread for the same game at matching timestamps.
2. **Method:** Cross-correlation analysis. Identify lead-lag pairs where one market consistently moves before another. Measure the mean lag duration and the magnitude of the delayed move.
3. **Simulation:** For each identified pair, simulate a strategy that trades the laggard when the leader moves. Account for execution latency and spreads.
4. **Success metric:** Identified lead-lag pairs where the laggard's subsequent move magnitude > transaction costs, with p < 0.05 on the cross-correlation.
