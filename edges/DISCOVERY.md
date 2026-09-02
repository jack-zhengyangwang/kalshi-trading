# Automated Edge Discovery

**Status:** proposed (2026-08-04)
**Related:** All edges in this library

## The meta-edge

The edge library grows through manual research — conversations, reading, observation. But there is a pipeline of *already-published* academic work on behavioral biases, game theory, prediction markets, and market microstructure that we haven't read yet. A periodic automated sweep of these sources surfaces candidate edges faster than waiting to encounter them organically.

This is not an edge itself — it's a **discovery mechanism** that feeds the `proposed/` pipeline.

## Mechanism

A cron-driven search process that queries academic and practitioner sources on a regular cadence, extracts candidate biases or inefficiencies, and writes structured proposals to the `proposed/` folder. Each candidate goes through human triage before being accepted into the library.

## Search sources

| Source | Query pattern | Frequency |
|--------|--------------|-----------|
| **arXiv** (q-fin, econ, stat) | "prediction market" + "bias" / "inefficiency" / "mispricing"; "sports betting" + "market efficiency"; "favorite-longshot bias"; "overconfidence" + "betting" | Weekly |
| **Google Scholar** | "behavioral bias prediction markets"; "game theory betting strategy"; "cognitive bias financial markets" | Bi-weekly |
| **SSRN** | Same as arXiv patterns, plus "market microstructure" + "prediction" | Weekly |
| **Kalshi blog / research** | Kalshi's own market data and research publications | Monthly |
| **Betfair / betting exchange research** | Academic studies using Betfair data (the most-studied prediction market) | Monthly |
| **Cognitive bias literature** | Newly documented biases from psychology / behavioral economics (Kahneman successor work, replication studies, meta-analyses) | Monthly |
| **Game theory theses** | Evolutionary game theory applied to markets, agent-based market models, zero-sum game optimal strategies | Bi-weekly |

## Extraction format

Each search hit that looks promising gets a one-paragraph candidate card:

```markdown
### Candidate: [short name]

**Source:** [paper title / URL / author / year]
**Claim:** [one-sentence statement of the claimed inefficiency]
**Mechanism:** [why it exists — cognitive, structural, informational]
**Testability:** [how we could measure it with Kalshi data]
**Initial assessment:** [plausible / dubious / needs more reading]
```

A batch of candidates accumulates in a `discovery_queue.md` file, reviewed by the human operator periodically. Promising candidates get promoted to full `proposed/` files with proper test plans.

## Implementation

Two parts:

1. **A shell script** (`edges/discover.sh`) that runs web searches against the configured sources, fetches paper abstracts, and appends candidates to the queue.

2. **A cron entry** that triggers the script on the defined cadence. The script is read-only — it never modifies the edge library, only appends to the discovery queue for human review.

The human remains the gate: the script surfaces candidates; the human evaluates, writes the full edge file, and moves it to `proposed/`.

## Test plan (for the discovery mechanism itself)

1. **Run a manual sweep** of the last 2 years of arXiv q-fin + Google Scholar against the query patterns above.
2. **Count:** How many candidate edges surfaced? How many survived initial triage (plausible + testable with our data)? How many were genuinely new (not already in the library)?
3. **Success metric:** The mechanism pays for itself if it surfaces ≥1 new testable edge per month that wasn't already in the library.
