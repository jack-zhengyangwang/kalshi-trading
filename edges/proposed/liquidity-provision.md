# Liquidity Provision (Thin-Book Spread Capture)

**Status:** proposed (2026-08-04)
**Related:** [[resolution-time-arbitrage]], [[structural-model-pricing]]

## The edge

On thin order books (Kalshi WC markets typically have $50–200 resting on each side), the bid-ask spread is wide not because of risk but because there is no dedicated market maker. An automated system that places resting limit orders at favorable prices can capture the spread — earning the gap between what impatient takers pay and what fair value suggests.

This is not a directional edge. It does not require being right about the outcome. It requires being willing to provide liquidity when others demand immediacy.

## Mechanism

1. **No natural market maker:** Kalshi WC markets have no designated market maker. The spread is whatever the most patient buyer and seller are willing to quote. Between games, these quotes can be very wide.
2. **Impatient flow:** When a bettor wants to enter a position *now* (game starting, emotion running high, news breaking), they cross the spread and pay the ask. A resting limit order at the bid captures this.
3. **Gap mean-reversion:** Prices that gap away from fair value on a thin book tend to drift back as patient participants fill the vacuum. Placing orders at the displaced price and waiting for reversion is a form of statistical arbitrage.

## Counterparty

- **Impatient takers:** Bettors who demand immediate execution and pay the spread to get it.
- **The absence of anyone:** Between games, the book is empty. There is no counterparty — just a vacuum that you fill.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer Kalshi | Resting orders at the bid on thinly-traded legs; selling at the ask when the spread is wide relative to fair-value uncertainty |
| Prediction markets | Every thin Kalshi market. This applies to non-WC markets on Kalshi too — the mechanism is venue-wide. |
| Sports betting exchanges | Betfair, Smarkets — same structure, different venue |
| Equities | Small-cap stocks with wide spreads. Harder: you're competing with Citadel et al. But the principle is identical — you're the market maker. |

## Quantification

For each leg:
```
spread_pct = (ask - bid) / mid
edge_spread = spread_pct - (2 × execution_uncertainty)
```

The edge is the spread less an estimate of adverse selection cost (the probability that the price moved because someone knows something you don't). On Kalshi WC, adverse selection is low — there's no insider trading on soccer results — so the raw spread is mostly edge.

Implement as: place resting limit orders at `p_fair - half_spread` (buy) or `p_fair + half_spread` (sell). Collect the spread when filled. Manage inventory risk by capping total exposure.

## Test plan

1. **Data:** Kalshi order book snapshots (bid/ask/size) at regular intervals. Compare against structural fair value estimates.
2. **Method:** Simulate a market-making strategy: place resting bids at various offsets from mid, track fill rates and subsequent price moves. Measure spread capture net of adverse selection.
3. **Success metric:** Positive net spread capture after accounting for adverse selection and inventory carry costs. Sharpe > 0.5 on dedicated liquidity-provision capital.
