# Slow-Poll In-Play Scalping

**Status:** failed (2026-06-30)
**Related:** [[excitement-spike-fading]], [[cross-market-lead-lag]]

## What was attempted

Capturing the edge from in-play events (goals, momentum shifts) using the Keeper's exit-management poll loop at 60-second intervals, driven by ESPN's free scoreboard API.

## Why it failed

- **60-second poll is too slow.** The Kalshi book reprices within seconds of a goal. At 60 seconds, the edge window has already closed.
- **ESPN lag compounds the problem.** The ESPN free scoreboard itself lags real play by 30 seconds to 2 minutes. Combined with the 60s poll interval, we were 90-180 seconds behind the event.
- **Result:** The "in-play P&L" in the RECAP ($460 on 167 bets, 76% win) is not genuine edge — it's betting on nearly-decided games where the outcome is already obvious. Not scalping; just picking up pennies in front of a steamroller with a long lag.

## What was learned

1. **In-play edge requires sub-10-second detection.** The VAR/fast-feed document (`docs/FASTER_FEED_SCOPE.md`) correctly identifies the bottleneck: the data feed, not the model or the execution path.
2. **ESPN free API is insufficient for in-play scalping.** The free feed is fine for pre-game pricing and game-state tracking. It cannot support latency-sensitive in-play strategies.
3. **The RECAP in-play numbers are misleading.** The 76% win rate on in-play bets looks good but is an artifact of betting on games whose outcomes are broadly decided. This is not a repeatable edge — it's a selection effect.
4. **In-play remains theoretical until feed upgrade.** The excitement-spike fading, regime-detection, and cross-market lead-lag edges all require faster data than we currently have.

## Disposition

In-play scalping with the current infrastructure is abandoned as an edge source. The in-play path should be:
- **Disabled for promotion** (pre-game only for real money — already the policy)
- **Paper-only** for forward-testing and model calibration
- **Revisited only if/when** a faster feed is available (see `docs/FASTER_FEED_SCOPE.md` phased plan)

The excitement-spike fading edge is parked in `proposed/` — it requires a feed upgrade to test or trade.
