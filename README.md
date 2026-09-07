# Kalshi Trading — Autonomous Prediction-Market Betting System

An autonomous system that scans every open World Cup market on Kalshi, prices each leg with a blend of structural Poisson/NegBin models + optional LLM + market data, sizes bets via Kelly with hard per-bet caps, and evolves a population of strategy agents through a genetic algorithm.

**Status:** v3 paper arena running. Real-money pilot armed with tiny caps on game_lines pre-game. See [CLAUDE.md](CLAUDE.md) for full agent instructions, [RECAP.md](RECAP.md) for latest results.

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
| [RECAP.md](RECAP.md) | Latest results + findings (2026-06-30) |
| [docs/v3_plan.md](docs/v3_plan.md) | v3 design: what exists, what's duplicated, what's still to build |
| [docs/BUILD_PLAN_V2.md](docs/BUILD_PLAN_V2.md) | Arena v2 original build plan (historical) |
| [docs/PROMOTION_BUILD_PLAN.md](docs/PROMOTION_BUILD_PLAN.md) | Promotion patch-panel design |
| [docs/DEPLOY_CLOUD.md](docs/DEPLOY_CLOUD.md) | Cloud deployment guide |
| [docs/CLAUDE.md](docs/CLAUDE.md) | Legacy 4-agent system docs (retired — v1/v2 reference) |
