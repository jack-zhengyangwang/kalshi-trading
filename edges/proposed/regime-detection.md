# Regime Detection (Structural-Break Arbitrage)

**Status:** proposed (2026-08-04)
**Related:** [[recency-overreaction]], [[cross-market-lead-lag]]

## The edge

Markets are slow to recognize that the underlying regime has changed. They price using the old distribution while the world has shifted to a new one. A system that can detect the structural break before the market fully absorbs it can trade against the stale consensus.

In soccer terms: a red card in the 30th minute changes the game state fundamentally. The pre-game win probabilities are now wrong. The market reprices — but does it reprice *enough*? If the market under-adjusts (prices the 10-man team as if they still have a 35% chance when the true post-red-card probability is 20%), you can fade the stale consensus.

## Mechanism

1. **Anchoring:** Pre-game expectations anchor the market's probability estimate. When a regime-changing event occurs, the adjustment is a Bayesian update — but the prior (pre-game price) exerts a strong pull, and the update is often incomplete.
2. **Slow diffusion:** Information about the regime change (red card, key injury, tactical shift, weather) takes time to propagate through all related markets.
3. **Structural under-updating:** Even when the event is fully known, the magnitude of the probability shift may be underestimated. Market participants anchor to the pre-game number and adjust insufficiently.

## Counterparty

- **Anchored participants:** Using pre-game models that don't fully incorporate the regime change.
- **Slow updaters:** Manual bettors who haven't refreshed their information.
- **Under-reactors:** Those who know about the event but don't realize how much it changes things.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer in-play | Red card, key injury, weather change (torrential rain), tactical shift |
| Soccer pre-game | Star player ruled out day-of-game, manager sacked mid-tournament |
| Prediction markets | Fed rate path shift (one FOMC meeting changes the entire trajectory), election poll surprise |
| Equities | Sector rotation (rates regime change, AI disruption), M&A announcement |

## Quantification

For each regime-change event:
```
pre_event_p_fair → post_event_p_fair (our updated model)
pre_event_market_price → post_event_market_price (market's update)
underreaction = post_event_p_fair - post_event_market_price
```

The key is distinguishing between "our update is better calibrated than the market's update" vs. "our update happened to be right in this instance." This requires testing across many events where the regime change was unambiguous and the new-state probability can be estimated from historical data (e.g., how often does a 10-man team win given the scoreline at the time of the red card?).

## Test plan

1. **Data:** Historical soccer matches with identifiable regime-change events (red cards, injuries to key players). Pre-event and post-event Kalshi prices at high frequency.
2. **Method:** For each event, compare the market's post-event repricing against a structural post-event model (trained on historical red-card/injury scenarios). Measure the mean under-reaction.
3. **Control:** Also measure over-reaction cases (does the market sometimes over-adjust?). The edge exists only if the mean error is directional (systematic under-reaction), not just noisy.
4. **Success metric:** Mean `p_fair_post_event - p_market_post_event` > 0 and significant (p < 0.05). The edge should be larger for more dramatic regime changes (red card worse than yellow; star player injury worse than squad player).
