# Edge Discovery Queue

*Candidates surfaced by automated or manual research. Review periodically. Promote promising ones to full `proposed/` files. Discard the rest with a one-line reason.*

---

## Format

Each candidate: `### [short name]` | Source | Claim | Mechanism | Testability | Assessment

## Queue

<!-- APPEND NEW CANDIDATES BELOW -->

### Candidate: Hot-hand fallacy in penalty shootouts

**Source:** Manual — domain knowledge
**Claim:** After a successful penalty, the next penalty-taker from the same team is overbet (hot-hand fallacy). After a save, the next taker is underbet (gambler's fallacy). Both biases create edge in live penalty-shootout markets.
**Mechanism:** Cognitive — perceived streaks in independent events. Each penalty is effectively independent but bettors perceive momentum.
**Testability:** Historical penalty shootout data + Kalshi shootout markets (if they exist for WC). Compare implied probability shifts to actual conversion rate shifts.
**Assessment:** Plausible but narrow — penalty shootouts are rare. Test if Kalshi lists shootout markets.

---

### Candidate: Disposition effect in Kalshi positions

**Source:** Manual — behavioral finance literature (Shefrin & Statman 1985, Odean 1998)
**Claim:** Participants hold losing positions too long and sell winning positions too early. On Kalshi, this means contracts that have drifted against the holder are overheld (keeping prices above fair value), and contracts that have drifted in favor are undersupplied (prices below fair value).
**Mechanism:** Cognitive — loss aversion + mental accounting. Selling a loser realizes the loss; holding preserves the possibility of recovery. Selling a winner locks in the pleasure; holding risks giving it back.
**Testability:** Requires per-account position data (not available from Kalshi public API). Testable via aggregate order flow analysis: do winning contracts see more sell volume than losing contracts, controlling for probability change?
**Assessment:** Plausible but data-constrained. Parked until we have order-flow data.

---

### Candidate: Home-field advantage decay in empty stadiums

**Source:** Manual — COVID-era sports research
**Claim:** During tournaments or conditions where stadiums are empty or neutral-venue, the market overestimates home-field advantage. The historical home-win premium persists in pricing even when the mechanism (crowd noise influencing referees) is absent.
**Mechanism:** Structural — stale-anchor. Historical home-field advantage (~60% win rate) is embedded in models and participant priors. When the mechanism is removed (neutral venue), the adjustment is incomplete.
**Testability:** Compare Kalshi prices for games at neutral venues vs. true-neutral-venue outcomes. The 2026 World Cup has neutral venues for most games — directly testable.
**Assessment:** Plausible and testable with current WC data. Promote to full proposal.

---

### Candidate: Information cascade in low-liquidity markets

**Source:** Game theory — Bikhchandani, Hirshleifer, Welch (1992) "A Theory of Fads, Fashion, Custom, and Cultural Change as Informational Cascades"
**Claim:** In thin markets where participants can observe each other's trades (Kalshi shows order book and recent fills), early trades can trigger cascades where later participants ignore their own signal and follow the crowd. An agent that trades against the cascade direction when its own signal disagrees profits from the eventual correction.
**Mechanism:** Information cascade — rational herding. Each participant sees prior trades and updates their belief, eventually ignoring their private signal entirely. The cascade is fragile — a single large counter-trade can break it.
**Testability:** Observe Kalshi order book before/after large trades. Does a large buy trigger a cascade of smaller buys that pushes the price beyond fair value? Does it revert? Requires high-frequency order book data.
**Assessment:** Plausible mechanism, data constrained for now. Worth re-evaluating when we have WebSocket-level order book data from Kalshi.

---

### Candidate: Selection bias in survivorship (tournament advance markets)

**Source:** Manual — statistical reasoning
**Claim:** The market overprices "Team X advances" for teams that have already played their easiest group game, and underprices teams that have their hardest game first. The schedule asymmetry creates a selection bias in the advance probability that the market doesn't fully adjust for.
**Mechanism:** Cognitive — anchoring to current group standings. A team that won its first game (against the weakest opponent) looks more likely to advance than a team that lost its first game (against the strongest), even if their residual schedules imply the same advance probability.
**Testability:** Within-group advance probabilities after each round of group games. Compare market-implied advance probabilities against a schedule-aware structural model. Does the market underweight remaining schedule strength?
**Assessment:** Promising and directly testable with current WC group stage data. Promote.

---

*Last swept: 2026-08-04 (manual, initial population)*
