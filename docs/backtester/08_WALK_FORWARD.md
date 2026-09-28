# 08 — Walk-forward with managers

`wc/firm/walk.py` replays the same engine as `wc/firm/run.py`, in ticks, with a
manager deciding between ticks. Same fees, same fills, same scorecard — a
one-tick walk reproduces the single-pass number exactly (tested), so gate-1
and gate-2 numbers are comparable by construction.

```
python3 -m wc.firm.walk --tick-days 2 --start 2026-07-10          # backtest
python3 -m wc.firm.walk --tick-days 7 --source collector           # arena
python3 -m wc.firm.walk --no-manager --pm draw-desk elo-desk       # fixed desks, no LLM
```

One tick of bars is in memory at a time, which is what lets the whole archive
run on the 1 GB droplet.

## What a desk is here

A **view** + **agents** + **bankroll** + **caps** + a **manager**. The manager is
set in the PM's JSON:

```json
"manager": {"type": "llm", "provider": "anthropic", "model": "claude-opus-5"}
"manager": {"type": "llm", "provider": "openrouter", "model": "qwen/qwen3-235b-a22b"}
"manager": {"type": "llm", "provider": "groq", "model": "llama-3.3-70b-versatile"}
"manager": {"type": "none"}                       // a fixed desk — every benchmark
```

Keys come from the environment (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`,
`GROQ_API_KEY`), never from config. Two desks that differ only in `manager`
are a fair test of which manager is better.

## The tick

1. Load the tick's bars. Every desk prices them with its view and runs its
   session; positions, bankroll and journal carry across ticks.
2. Benchmarks (manager `none`) are scored first so their tick P&L is in every
   brief.
3. Each managed desk gets a **brief**: this tick (P&L, fees, Brier, win rate,
   per-agent), the previous tick, cumulative, the benchmarks over the same
   tick, open positions, the journal, a **noise band** (per-trade std × √n —
   a tick inside it is what luck alone produces), and its own decision log.
4. The manager returns `hold` or `change` with reasoning and an assessment
   (vs benchmark, vs last period, variance or structure). A `change` carries
   the complete new desk definition.
5. The runner validates it — a known view, agents in the DSL, caps present,
   **same name, same bankroll** — and applies it: same view → parameters
   change and learned state (Elo ratings, trailing windows) is kept; a new
   view starts cold; agents that stay keep their positions, retired agents
   wind down by their own exit rules, new agents start with a share of free
   cash. Rejected configs are logged with the reason and the desk holds.
6. `version` bumps on every applied change. Versions live in this number and
   in git — never in filenames.

## Output

`data/walks/<stamp>/pm-<name>.json` — final report, every tick's summary,
every decision (brief, action, reasoning, assessment, applied/rejected, what
changed, version before/after), the final config, all trades.
`leaderboard.json` — every desk ranked, benchmarks flagged, `vs_baseline`.

## What the manager is not allowed to do

Change the desk's name or bankroll. That is all. Everything else — view,
parameters, sizing, rules, agents, universe, caps — is the manager's call,
and the log is where it answers for it.
