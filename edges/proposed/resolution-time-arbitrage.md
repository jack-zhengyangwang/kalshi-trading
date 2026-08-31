# Resolution-Time Arbitrage (Horizon Mismatch)

**Status:** proposed (2026-08-04)
**Related:** [[liquidity-provision]], [[structural-model-pricing]]

## The edge

Markets that resolve far in the future are less efficiently priced than same-day or short-horizon markets. Capital tied up for weeks carries an opportunity cost that most participants over-discount. A system with no quarterly reporting, no emotional impatience, and no daily-P&L pressure can systematically bet these slow-resolving markets at favorable prices.

The edge compounds when you treat the position as a **tradeable instrument** over its life: enter on the initial mispricing, add when the market panic-sells, trim when it euphoria-buys, and exit when the gap closes. The total return = static mispricing at entry + accumulated P&L from trading around the position.

## Mechanism

1. **Short-horizon preference:** Most participants (retail bettors, even professional traders) have utility functions that penalize capital lockup. A 30-day resolution ties up capital that could be deployed elsewhere. They demand a discount.
2. **Attention scarcity:** Between event dates, attention vanishes. The book goes thin, prices drift. Few participants update their fair-value estimates when no games are happening.
3. **Game-day overspill:** When a game *does* happen, the emotional reaction spills into *all* contracts for that team, including the long-horizon ones. Brazil conceding a goal in a group game panics "Brazil advance" contracts whose true probability barely moved.
4. **Resolution certainty drift:** As the tournament progresses, the advance/elimination probability drifts toward 0% or 100%. The range of possible outcomes narrows, allowing more aggressive position sizing as uncertainty resolves.

## Counterparty

- **Short-horizon bettors:** Want same-day resolution. They might agree the contract is mispriced but won't tie up capital for 3 weeks to collect.
- **Inattentive participants:** Ignore the contract between event dates.
- **In-game panickers:** Sell everything with the team's name on it when they concede, regardless of which contract.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer Kalshi | "Advance from group," "Reach semifinal," "Tournament winner" — resolve weeks out |
| Prediction markets | "Who wins the 2028 election?" — years out, thinly traded |
| Sports betting | Futures bets (season champion, division winner) |
| Equities | Value factor, merger arb, closed-end fund discounts — all require patience; the premium is larger over longer holding periods |

## Quantification

```
edge_horizon = |p_fair - p_market| ~ resolution_days + controls(volume, event_uncertainty, market_type)
```

Hypothesis: `resolution_days` is a significant positive predictor of pricing error, controlling for volume and event uncertainty.

For the dynamic trading version: simulate two strategies against the same historical price path — static (hold to resolution) vs. dynamic (re-evaluate daily, add/trim/exit on gap changes). Decompose total P&L into static-mispricing component and trading-around-position component.

## Test plan

1. **Data:** All Kalshi contracts with resolution dates > 7 days out. Historical price paths (mid prices at regular intervals from trade tape or polled data).
2. **Method:** Regression of pricing error against resolution horizon, with volume and event-type controls.
3. **Dynamic test:** Simulate static vs. dynamic strategy on the same price paths. Compare total return, Sharpe, and max drawdown.
4. **Success metric:** Resolution horizon is significant predictor (p < 0.05). Far-horizon (>30 day) markets show 1.5–2× the mean edge of same-day markets. Dynamic strategy outperforms static on both winning and neutral positions.
