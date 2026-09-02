# Structural Model Pricing (Model-vs-Crowd)

**Status:** working (2026-06-30)
**Related:** [[draw-underpricing]], [[resolution-time-arbitrage]]

## The edge

The core edge-generating mechanism in the current system: a structural Poisson/NegBin model that prices every leg from first principles (team ELO → goal distribution → leg probability) systematically outperforms the market mid price. The market price embeds emotional flow, recency bias, and lazy extrapolation; the structural model embeds the underlying goal-generating process.

This is the "engine room" edge — it's what all the other proposed edges overlay on top of.

## Mechanism

1. **Structural pricing:** Rather than betting on which outcome occurs, model the underlying goal-generating process (two Poisson processes, one per team, parameterized by ELO strength). Derive every leg's probability analytically from the scoreline distribution.
2. **Market mid as noisy signal:** The market mid price is incorporated into the logit stacker as one input among several — it provides a sanity check and a measure of market sentiment, but does not dominate.
3. **Logit stacking:** `p_fair = logit⁻¹(w_data × logit(p_data) + w_llm × logit(p_llm) + w_market × logit(p_mid))`. The data model weight (`w_data`) is the largest, and the market weight is moderate — meaning we're systematically betting against the market when our model disagrees.

## Evidence

From RECAP.md (2026-06-30), no-LLM full retrain across 75 games, 6 generations:
- **SPREAD: +$268 (69% win)** — the structural model's spread pricing is the strongest single edge
- **WINNER: +$226** — the winner market also shows structural edge
- **Pre-game overall: +$31 (54% win)** — the trustworthy baseline; everything pre-game is slightly positive
- **In-play: +$460 but suspect** — likely betting near-decided games, not model edge

The structural model consistently finds edge in markets where the goal distribution is the right abstraction. It does NOT work on:
- Novel/LLM-only market types (no Poisson kernel)
- Corners (requires the corner NB model, which is separate)
- Market types where the underlying process isn't goals

## Counterparty

- **Emotional bettors:** Fading their flow (draw underpricing, recency, etc.)
- **Lazy consensus:** The market mid is an average of participant opinions, many of which are under-informed
- **Short-horizon participants:** Not pricing the full resolution path

## Current limitations

- **LLM blend dormant:** `use_llm=False` in most paths. The stacker is currently 2-source (data model + market mid), not 3-source.
- **No resolution-time awareness:** `tau_days` always 0. The structural model doesn't adjust for horizon.
- **Limited market coverage:** Only prices legs where a Poisson/NegBin kernel applies. Novel types fall back to LLM-only (capped at 30 calls/scan).

## Next steps

- Turn the LLM blend on for live pricing (Brain v3)
- Add resolution-time adjustment to the structural model
- Wire the prediction DB to accumulate calibrated evidence by market type
