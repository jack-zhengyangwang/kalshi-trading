# Edge Library

A catalog of structural market inefficiencies — durable biases where the counterparty is identifiable, the mechanism is understood, and the edge can be quantified. Each edge is a hypothesis about *why the market price systematically differs from fair value*, not just an observation that it does.

## Classification

| Folder | Meaning |
|--------|---------|
| **`proposed/`** | Theorized, not yet tested. Has a clear mechanism, an identified counterparty, and a falsifiable test plan. Awaits data or implementation. |
| **`working/`** | Evidence-confirmed. The edge has been isolated (not just correlated P&L), tested out-of-sample or forward, and survives after accounting for transaction costs. |
| **`failed/`** | Tested and rejected. Either the mechanism didn't hold, the costs exceeded the edge, or the implementation was infeasible. **Not deleted** — the writeup preserves what was learned and why it didn't work, so the reasoning isn't lost. |

## How edges move

```
proposed/  ──(tested, confirmed)──▶  working/
    │                                      │
    └──(tested, rejected)──▶  failed/      │
                                           │
    working/  ──(regression, edge closed)──▶  failed/
```

## Meta layer

| File | Purpose |
|------|---------|
| [DISCOVERY.md](DISCOVERY.md) | Automated discovery mechanism — periodic web sweeps of academic sources (arXiv, Google Scholar, SSRN) for new biases, game theory theses, and published papers. Feeds the queue. |
| [discovery_queue.md](discovery_queue.md) | Candidate queue. Each entry is a one-paragraph card: source, claim, mechanism, testability, assessment. Human reviews periodically; promising ones promote to `proposed/`, rest are discarded with cause. |

The discovery mechanism is **infrastructure**, not an edge — it's the pipeline that keeps the library fed. It belongs at the root of the `edges/` folder, not inside `proposed/`.

## Required sections for every edge file

1. **The edge** — one-paragraph statement of the market inefficiency
2. **Mechanism** — *why* the mispricing exists (cognitive bias, structural constraint, information asymmetry)
3. **Counterparty** — who is on the other side and why they're systematically wrong
4. **Domain mapping** — how it manifests in prediction markets, sports betting, and equities
5. **Quantification** — how to measure the edge magnitude from data
6. **Test plan** — falsifiable test, data required, success metric
7. **Status** — proposed / working / failed, with date and evidence
8. **Related edges** — links to other edge files in this library

## Edge taxonomy

| Edge | Class | Domain | Status |
|------|-------|--------|--------|
| [Draw underpricing](proposed/draw-underpricing.md) | Overconfidence | Soccer → any multi-outcome | proposed |
| [Recency overreaction](proposed/recency-overreaction.md) | Availability bias | All sequential-event markets | proposed |
| [Resolution-time arbitrage](proposed/resolution-time-arbitrage.md) | Horizon mismatch | Long-dated contracts | proposed |
| [Volatility premium](proposed/volatility-premium.md) | Tail-risk overpricing | All binary markets | proposed |
| [Liquidity provision](proposed/liquidity-provision.md) | Thin-book spread capture | Thin prediction markets | proposed |
| [Excitement-spike fading](proposed/excitement-spike-fading.md) | Emotional overreaction | In-play sports | proposed |
| [Cross-market lead-lag](proposed/cross-market-lead-lag.md) | Information diffusion | All related markets | proposed |
| [Boring equilibrium](proposed/boring-equilibrium.md) | Narrative neglect | All multi-outcome markets | proposed |
| [Regime detection](proposed/regime-detection.md) | Stale-anchor | All markets with structural breaks | proposed |
| [Structural model pricing](working/structural-model-pricing.md) | Model-vs-crowd | Soccer Kalshi | working |
| [LLM-everything replay](failed/llm-everything-replay.md) | Rate-limit infeasibility | Prediction markets | failed |
| [Slow-poll in-play scalping](failed/slow-poll-in-play-scalping.md) | Feed latency | In-play sports | failed |
