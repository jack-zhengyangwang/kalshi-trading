# Recency Overreaction

**Status:** proposed (2026-08-04)
**Related:** [[regime-detection]], [[excitement-spike-fading]]

## The edge

After a dramatic result (blowout win, heavy loss), the market over-extrapolates that single outcome into the next game's pricing. The winning team's next-game implied probability is inflated; the losing team's is deflated. Fading the overreaction — betting the underdog after a blowout, or fading the favorite after a rout — captures the mean reversion.

## Mechanism

**Availability heuristic:** A 5-0 scoreline is vivid and memorable. When pricing the next game, the recent blowout dominates mental models, crowding out the base rate (the team's long-run strength). One game's result, however dramatic, carries less signal about the next game than the market believes.

**Compounding factors:**
- Media amplifies blowouts (headlines, highlights, social media). The casual bettor sees the 5-0 and nothing else.
- Algorithmic models with short lookback windows overfit recent data.
- Tournament context: a team that just "looked unstoppable" attracts more recreational money for their next game.

## Counterparty

- **Recreational bettors:** Saw the highlights, bet on the team that looked good.
- **Short-window models:** Systematic strategies with insufficient shrinkage on recent observations.
- **Media-amplified flow:** Headlines drive attention, attention drives volume, volume moves prices.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer group stage | Post-blowout games: favorite's implied win% is inflated |
| Soccer knockouts | Less applicable — knockout context is different (single elimination changes incentives) |
| Prediction markets | After a surprise event (candidate drops out, Fed surprises), the next related contract over-adjusts |
| Equities | Post-earnings-announcement drift: stocks overcorrect on earnings surprises and drift back over weeks |

## Quantification

Construct a recency-shock variable:
```
recency_shock = previous_game_goal_diff × decay(tournament_weight)
```
where `decay()` accounts for the tournament round (group stage games weigh more than friendlies).

Regression:
```
(p_market - p_outcome) ~ recency_shock + elo_diff + home_away + round
```
Null: coefficient on `recency_shock` is zero. Alternative: positive coefficient → market systematically overreacts.

Simple version: group games by previous result (big win, narrow win, draw, narrow loss, big loss). Measure mean pricing error in each bucket. Expect monotonic relationship: bigger result → bigger error in the opposite direction next game.

## Test plan

1. **Data:** All Kalshi winner markets with at least one prior game result available.
2. **Method:** Regression as above + bucket analysis.
3. **Control:** ELO difference must be included — the recency effect must be measured *above and beyond* the team strength signal.
4. **Success metric:** Recency-shock coefficient positive and significant (p < 0.05). Post-blowout bucket shows mean error > 3pp in the expected direction, monotonic with shock magnitude.
