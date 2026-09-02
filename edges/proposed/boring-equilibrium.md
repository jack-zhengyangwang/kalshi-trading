# Boring Equilibrium (Null-Outcome Underpricing)

**Status:** proposed (2026-08-04)
**Related:** [[draw-underpricing]], [[volatility-premium]]

## The edge

In any multi-outcome market, the outcome that lacks narrative appeal — the null, the status quo, the "nothing happens" result — is systematically underpriced. People bet on *stories*, not probabilities. The null hypothesis is the least-bet outcome in every domain.

This is the draw insight generalized to its purest form. The draw is underpriced not because it's a draw, but because it's boring.

## Mechanism

1. **Narrative utility:** Betting on an outcome provides not just expected monetary return but *narrative consumption value* — being able to say "I knew they'd win," participating in the excitement, having a stake in the story. The null outcome provides zero narrative utility.
2. **Salience asymmetry:** A recession is a story. "No recession" is not. A government shutdown is a headline. "Government continues functioning normally" is not. The salient outcome attracts bets; the non-salient outcome gets residual pricing.
3. **Attention economics:** Media, social media, and conversation all amplify the dramatic outcomes. The boring ones are ignored, and what is ignored is mispriced.

## Counterparty

- **Narrative-seekers:** Betting for entertainment value, not just expected return. They want a story to tell.
- **Salience-chasers:** Attention drives their betting decisions. The outcome that makes headlines is the one they bet on.
- **Media-amplified flow:** The press covers what could go wrong, not what could stay the same.

## Domain mapping

| Domain | Narrative outcome (overpriced) | Boring outcome (underpriced) |
|--------|-------------------------------|------------------------------|
| Soccer | Home team wins, BTTS yes, over 2.5 goals | Draw, under 2.5 goals, BTTS no |
| Prediction markets | Recession coming, government shutdown, crisis | No recession, status quo maintained |
| Event contracts | "Will X happen?" — the exciting event | "Will nothing happen?" — the null |
| Equities | Growth stocks (story), momentum (trend) | Value stocks (boring), range-bound (sideways) |

## Quantification

Identify pairs of complementary outcomes where one is narratively exciting and the other is boring:
```
edge_boring = p_fair_null - p_market_null
```

Compare the edge on the boring leg against the edge on the exciting leg. Hypothesis: the boring leg has systematically larger positive edge (market underprices it).

The meta-test: across *all* edge types, classify each leg as "narrative" or "null" based on its outcome description. The null-classified legs should show higher mean edge than the narrative-classified legs, controlling for market type and probability.

## Test plan

1. **Data:** All Kalshi legs across all market types. Classify each leg's YES outcome as narrative (a specific event happens) or null (status quo maintained, nothing happens, draw).
2. **Method:** Regression of `edge = p_fair - p_market` on `is_boring` dummy variable, controlling for market type, probability decile, and volume.
3. **Success metric:** `is_boring` coefficient positive and significant (p < 0.05). Mean edge on boring legs > mean edge on narrative legs by >1pp.
