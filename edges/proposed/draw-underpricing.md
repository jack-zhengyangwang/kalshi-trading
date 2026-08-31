# Draw Underpricing (Overconfidence Bias)

**Status:** proposed (2026-08-04)
**Related:** [[boring-equilibrium]], [[volatility-premium]]

## The edge

In any multi-outcome market where one outcome carries emotional attachment (a team winning), the emotionally neutral outcome (the draw/tie) is systematically underbet. The market price implies a lower draw probability than the true underlying probability. Buying the draw leg at inflated odds yields positive expected value over a large sample.

## Mechanism

Two reinforcing forces:

1. **Tribal loyalty:** Fans bet on their team to *win*. Betting on a draw is emotionally unsatisfying — there's no celebration, no bragging rights. The flow of recreational money is overwhelmingly directional toward the win legs.

2. **Bookmaker pass-through:** On Kalshi and betting exchanges, the win legs attract the volume. The draw leg gets residual pricing — the market clears at whatever price balances the (smaller) draw interest, not at the true probability. Sportsbooks have a separate incentive: they set draw odds wide intentionally because their liability on the win/loss legs is what keeps them up at night.

## Counterparty

- **Primary:** Recreational fans betting with tribal loyalty. They are not profit-maximizing. They are buying entertainment and identity expression. This counterparty is durable — every World Cup, every league weekend, the same bias.
- **Secondary:** Sportsbooks hedging their book on the win/loss legs, leaving the draw as a residual price.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer winner markets (KXWC) | Draw-YES contract is underpriced vs. structural Poisson model |
| Soccer totals, BTTS | "Under" and "No" legs (the boring side) are underpriced |
| Prediction markets | Status-quo outcomes: "No recession," "No government shutdown" |
| Equities | Range-bound stocks are underpriced vs. the narrative-driven names |

## Quantification

For each game, compute:
```
edge_draw = p_fair_draw - p_market_draw
```
where `p_fair_draw` comes from the Brain v2 structural model (Poisson goal model → draw probability from scoreline distribution) and `p_market_draw` is the draw-YES mid price on Kalshi.

Hypothesis: `mean(edge_draw) > 0` across the sample, with statistical significance.

## Test plan

1. **Data:** Historical Kalshi winner markets (home/draw/away YES legs) from the group stage. Brain v2 already produces `p_fair` for these legs.
2. **Method:** Paired comparison of `p_fair_draw` vs. `p_market_draw` for each game. One-tailed t-test.
3. **Control:** Also test home-win and away-win edges. If the mechanism is correct, the edge should be concentrated on the draw, not evenly distributed.
4. **Out-of-sample:** Validate on Betfair exchange historical data (different venue, different participant pool). If the edge persists, it's the mechanism (overconfidence), not the venue (Kalshi specific).
5. **Success metric:** Mean edge on draw legs > 2 percentage points, p < 0.05, across >50 games.
