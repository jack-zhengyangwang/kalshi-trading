# Excitement-Spike Fading (VAR / Event Overreaction)

**Status:** proposed (2026-08-04)
**Related:** [[recency-overreaction]], [[cross-market-lead-lag]]

## The edge

During a soccer game, dramatic events (goal scored, penalty awarded, red card shown) trigger an immediate and large market repricing — but the market prices the event as *certain* before it actually is. A goal under VAR review has a ~2-15% chance of being disallowed, but the market reprices to near-100% certainty within seconds. A penalty has a ~20% chance of being saved or missed, but the win-probability jump is priced as if the penalty = 1.0 goals. A red card might be overturned, or the 10-man team might still draw.

Fading the spike — selling into the excitement and buying back on confirmation or reversal — collects the gap between "market prices this as done" and "it's not done yet."

## Mechanism

1. **Emotional certainty:** The crowd sees ball in net = goal. The emotional bettor sees red card = game over. The probability of reversal is ignored because the *visual* is conclusive.
2. **Attention capture:** The dramatic event dominates attention. Nobody is thinking about the base rate of VAR disallowances or penalty saves — they're reacting to the moment.
3. **VAR window blindness:** During the 30–120 second VAR review window, the market has already repriced to the post-goal state. But the goal may not stand. The gap between "market-implied 100% goal" and "true ~95-98% goal" is edge.
4. **Pre-VAR signals:** Certain play-by-play signals (referee hand to ear, late flag, mention of "challenge") raise the disallowance probability to 30-50% — but the market doesn't process these text cues. A text-based feed watcher can act before the TV-watching crowd.

## Counterparty

- **Emotional in-game bettors:** Reacting to the visual, not the probability. They see a goal and buy the scoring team; they see a red card and sell the punished team.
- **TV-only participants:** Latency of video + human reaction time > latency of text-based feed parsing.
- **Certainty seekers:** Want to lock in a "sure thing" after the dramatic event.

## Domain mapping

| Domain | Manifestation |
|--------|---------------|
| Soccer in-play | VAR reviews, penalties, red cards, late goals (equalizers possible), star player injuries |
| Other sports | NFL: touchdown under review, turnover; NBA: buzzer-beater review, flagrant foul |
| Prediction markets | Breaking news events — market spikes on headline, drifts back as details emerge |
| Equities | Earnings headlines cause spike, full report causes drift-back; FDA announcements |

## Quantification

For each excitement-spike event type:
```
pre_event_p_fair → post_event_market_price → post_confirmation_true_probability
edge_temporary = post_event_market_price - post_confirmation_true_probability
```

The edge exists only during the window between the visual event and the official confirmation. After confirmation, the gap closes to near-zero.

Signal conditioning: the edge magnitude varies by event subtype. A goal from open play with no VAR signal = small edge (~2%). A goal with the referee's hand to the ear = large edge (~30-50%).

## Test plan

1. **Data:** In-play Kalshi price paths at high frequency (ideally 1-5 second intervals) during games with known VAR events, penalties, or red cards. Requires a faster feed than the current 60s ESPN poll — this edge is **untestable** with the existing slow-poll infrastructure.
2. **Method:** Event study. For each event, measure the pre-event price, the post-spike peak price, and the post-confirmation price. Compute the temporary overreaction.
3. **Prerequisite:** Upgrade live feed to 1-3s polling (see `docs/FASTER_FEED_SCOPE.md`) before this edge can be tested or traded.
4. **Success metric:** Mean overreaction magnitude > 2pp, with >60% of events showing a reversal toward fair value after the spike.
