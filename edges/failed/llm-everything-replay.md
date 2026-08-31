# LLM-Everything Replay

**Status:** failed (2026-06-30)
**Related:** [[structural-model-pricing]]

## What was attempted

Running the full Brain v2 with `use_llm=True` on a batch replay of 75 historical games. The idea was to price every leg using the 3-source stacker (data model + LLM priors + market mid) and backtest the full system with LLM intelligence.

## Why it failed

- **Rate limits:** Anthropic API rate limits made thousands of LLM calls across 75 games × multiple categories × multiple legs infeasible. The replay would take hours or get throttled.
- **Cost:** Even if technically possible, the token cost of calling the LLM for every leg in a batch replay was disproportionate to the edge gained.
- **LLM is the bottleneck, not the model:** The structural model runs in milliseconds per leg. The LLM call adds seconds, and you need one per game per category. For a batch replay of 75 games, that's hundreds of calls.

## What was learned

1. **LLM is live-only by design.** One call per game per category when the game is actually happening is affordable. Batch-replaying history with the LLM is not.
2. **Structural model carries the load.** The no-LLM results (RECAP.md) show the structural model alone generates genuine edge. The LLM is an overlay for novel market types the Poisson kernel can't price, not the primary edge source.
3. **LLM caching is critical.** The current Brain's LLM cache (one call per game per category, cached for reuse across legs) is the right architecture. Without it, the LLM path is a non-starter.
4. **This is an infrastructure limitation, not an edge failure.** The LLM blend may still add value in live trading where the call count is bounded by the number of live games (handful, not hundreds).

## Disposition

The LLM-everything replay approach is abandoned. The LLM path should only be used:
- In **live** trading (not batch replay)
- For **novel market types** the structural model can't price
- With **caching** (one call per game per category)
- Capped at **30 calls per scan** (existing guardrail)
