# Follow-up scope: faster live-feed / in-play goal latency

**Status:** PARKED (deferred 2026-06-15). Lower priority than prediction accuracy.
**Why deferred (user's call):** the binding constraint here is *getting fast data sources* — an external, expensive/licensed/legally-gray barrier we don't control. Prediction accuracy is ours to improve and a higher-leverage, lower-barrier lever. Do that first; revisit this only if the in-play P&L justifies it.

---

## The edge being chased
When a goal is scored, the true win/score probabilities jump instantly, but a **thin Kalshi WC book reprices over seconds-to-a-minute** as slow manual participants react. A machine that detects the goal and fires in **1–5s** picks off stale resting orders / slow repricing. Hand-trading can't compete with that latency.

## The actual bottleneck = DETECTION, not us
Order-send (~tens of ms) and signal-compute (`brain.evaluate_live` recomputes fair instantly on a score change) are already fast. The slow link is the **data feed**:
- **Current:** ESPN free scoreboard, polled every `live_poll_secs`=60s, and ESPN itself lags real play 30s–2min → we capture **0%** of this edge today.

## Don't chase the impossible version
Beating the goal by milliseconds = competing with HFT firms on **pro feeds** (Sportradar / Genius / Opta, <1–3s, $thousands/mo, betting-licensed) + co-location. We lose that race. **In-venue "courtsiding"** is fastest but banned/legally gray — out.

## The winnable version
Don't race pro HFT to the goal — **beat the slow manual traders on a sleepy thin book.** Being ~5–15s late is still enough when the other side is human. Fits what we already have (brain computes fair instantly); just feed it the score faster and act faster.

## Phased plan (cheap → less cheap) — when we pick this up
1. **Faster free detection.** Swap ESPN scoreboard → ESPN `summary`/play-by-play endpoint (more granular, updates sooner); poll **1–3s** during live games only. Free. Gets us from "minutes late" to "~5–15s late."
2. **Warm order path.** Pre-authenticated Kalshi session + pre-staged order intents per leg, so a detected goal fires instantly (no cold-start auth/round-trip).
3. **Confirming source.** A second fast feed (free API / social firehose) to filter phantom-goal false positives before dumping size.
4. **Only if justified:** a paid sub-second feed — gate on whether thin-market in-play P&L actually clears the $thousands/mo.

## Where it plugs in
- `group/live_feed.py` (the ESPN source + `state_from_events`) — the swap point.
- `group/config.json` `live_poll_secs` — drop during live games.
- Keeper / arena `live_step` already react to score changes via `evaluate_live` — they just need the faster trigger + warm order path.

## Honest bar
This is latency-arb / in-play scalping — a known, hard, contested game. On thin Kalshi WC books there's a real but small and contested opportunity. Worth a cheap Phase-1 experiment someday; not worth pro-feed money unless the data says so.
