# Kalshi Trading — Autonomous Prediction-Market Betting System

An autonomous system that scans every open Soccer market on Kalshi (discovered live by tag, no hardcoded series list), prices each leg with a blend of structural Poisson/NegBin models + optional LLM + market data, sizes bets via Kelly with hard per-bet caps, and evolves a population of strategy agents through a genetic algorithm.

**Status:** v4 (per-league brains, all-soccer). Droplet wiped 2026-09-07 for a clean rebuild — nothing is deployed or trading right now. See [CLAUDE.md](CLAUDE.md) for full agent instructions, [RECAP.md](RECAP.md) for latest results.

## Latest finding — the market is close to efficient (2026-09-22)

A 53-tick walk-forward (Jun 3 → Sep 14) ran 5 LLM-managed desks against 5 fixed benchmarks,
$500 each. Across 3,582 settled bets the average price paid was 48.8¢ and the win rate
47.7%: Kalshi's price already is the probability. Every desk that traded lost money after
spread and fees; doing nothing beat all of them.

![Kalshi's prices are the probabilities](docs/findings/img/01_calibration.png)

Full write-up with four charts: [docs/findings/01_MARKET_EFFICIENCY.md](docs/findings/01_MARKET_EFFICIENCY.md).

## Quick Start

```bash
cd ~/dev/kalshi-trading
source venv/bin/activate

# Paper arena status
python3 -m wc.arena --status

# Real-money pilot status
python3 -m wc.pilot_status

# Dry-run promoter (safe — no real orders)
python3 -m wc.promote
```

See [CLAUDE.md](CLAUDE.md) for the full runbook, architecture, and safety rules.

## Docs

| File | Purpose |
|------|---------|
| [CLAUDE.md](CLAUDE.md) | Agent instructions: architecture, runbook, ground rules |
| [docs/findings/01_MARKET_EFFICIENCY.md](docs/findings/01_MARKET_EFFICIENCY.md) | Walk-forward finding: Kalshi soccer prices ≈ probabilities (2026-09-22) |
| [docs/backtester/08_WALK_FORWARD.md](docs/backtester/08_WALK_FORWARD.md) | Walk-forward runner + AI desk managers |
| [RECAP.md](RECAP.md) | Older arena results + findings (2026-06-30) |
| [docs/v3_plan.md](docs/v3_plan.md) | v3 design: what exists, what's duplicated, what's still to build |
| [docs/BUILD_PLAN_V2.md](docs/BUILD_PLAN_V2.md) | Arena v2 original build plan (historical) |
| [docs/PROMOTION_BUILD_PLAN.md](docs/PROMOTION_BUILD_PLAN.md) | Promotion patch-panel design |
| [docs/DEPLOY_CLOUD.md](docs/DEPLOY_CLOUD.md) | Cloud deployment guide |
| [docs/CLAUDE.md](docs/CLAUDE.md) | Legacy 4-agent system docs (retired — v1/v2 reference) |
