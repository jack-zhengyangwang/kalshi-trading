# WorldCupTrading

A multi-agent system that trades **FIFA World Cup** markets on [Kalshi](https://kalshi.com). It prices the **entire derivable match surface**, runs an **evolving population** of competing agent teams on it, learns from every resolved game, and surfaces the best team per market as a candidate to promote to real money.

This repo is about the **design and the process**, not a frozen result. You run it yourself: it scans live and resolved games, prices ~30 legs per match, paper-trades them with a population that **breeds and culls itself**, and re-tunes its own models — so you can reproduce it, watch it evolve, and **drop in your own agents**.

> **Paper by default.** Nothing places a real order. The arena fills at real market bid/ask and settles at $1/$0.

---

## The core idea

A bet is worth making only when **your estimate of fair value beats the price you pay.** The system is built to make that judgment well and repeatedly, then let *realized outcomes* — not opinions — decide which approach wins. It separates two things usually tangled together:

1. **The Brain** — *what is this worth?* A fair-value estimate per market.
2. **The agent teams** — *what do I do about it?* When to enter, how big, when to exit.

Every team consumes the **same** fair value, so the tournament cleanly measures *betting behavior*, not luck on a price.

There are **two generations** of this system in the repo:
- **v2 (current, at the repo root)** — the full-surface, evolving-population system. **Start here.**
- **v1 (legacy, in [`v1/`](v1/))** — the original fixed 16-team arena. Kept for reference, condensed at the bottom.

---

# v2 — the current system

## What's new vs v1
- **Coverage:** 3 legs/game → **~30 legs/game** (the full derivable surface).
- **Teams:** 16 fixed strategies → an **evolving population** that breeds winners and culls losers.
- **Brain:** a 3-source blend → a per-category **stacker** that a meta-learner re-tunes by accuracy.
- **LLM:** per-leg, per-poll → **one call per game per category** (cached), to survive rate limits.

## The market surface (4 super-categories)

Each **super-category** runs **its own Brain** and an independent population. Config in `categories_v2.json`; pricing in `markets_v2.py`.

| Super-category | Status | Markets (Kalshi series) |
|---|---|---|
| **game_lines** | active | winner, spread, total, team-total, BTTS — full match **+ 1st/2nd-half** variants (`KXWCGAME/SPREAD/TOTAL/TEAMTOTAL/BTTS`, `KXWC1H*`, `KXWC2H*`) |
| **game_props** | active | correct score, total & team **corners**, first-to-score (`KXWCSCORE`, `KXWCCORNERS`, `KXWCTCORNERS`, `KXWCFTTS`) |
| **events** | experimental (off) | announcer-word / in-broadcast markets — own LLM + base-rate brain, not Poisson |
| **player_props** | stubbed (off) | player-to-score / assists / shots — needs a player-data source |

Everything is priced from **one coherent goal/corner model per game**, so a spread, a total, and a winner can never silently disagree.

## How a price is made — the Brain (`brain_v2.py`)

```
game_prior (ELO supremacy split of a market-anchored goal & corner total)
   ├─ data   → markets_v2: Poisson / negative-binomial / exact-score grid prices every leg
   ├─ llm    → one cached call/game/category: per-game priors + optional per-leg overrides
   └─ market → the orderbook mid
        └─►  logit-space STACKER blend  →  p_fair   (+ per-source record)
```

- **`data_pfair`** prices any leg analytically: totals/team-totals via Poisson tails, spreads via the goal-difference grid, BTTS, 1H/2H via a 0.46 half-goal split, **corners** via a negative-binomial (over-dispersed, from a club corpus), **team corners** via a home/away share, **first-to-score** via a Poisson race, **correct score** via the exact-score grid.
- **The stacker** (`_stack`) blends the available sources in logit space.
- **The Brain Assistant** (`update_stacker`) re-tunes the stacker weights after each round by **inverse Brier** — sources that predicted better get more weight (guarded by a minimum sample so a few lucky games can't overfit).
- **LLM** is gated by a `use_llm` flag (off by default); when on it's **one call per game per category**, cached, returning `{total_goals, total_corners, p_home/draw/away, overrides}`.

## The agents — an evolving population (`strategy_v2.py` + `arena_v2.py`)

Instead of hand-coded strategies, each team is a point in a **continuous parameter space** (`PARAM_SPACE`): entry gates (`pregame`/`inplay`/`scalp`), `min_edge`, `edge_haircut`, `kelly_fraction`, `min_entry_price`, and exit thresholds (`take_profit`/`sell_margin`/`stop`/`fraction`). The four classic archetypes (aggressive-hold, conservative-active, late-scalp, momentum) are just **seeds**.

A team = a **Strategist** (sizes entries via Kelly, decides exits via the shared exit rules) + a zero-LLM **executor**. Each cycle (`cycle_once`):

```
SETTLE   resolved bets → realized P&L; feed outcomes to the Brain Assistant
SCAN+BET price each live game's legs; each team sizes & places paper bets on its edges
EXITS    re-price live; take-profit / scale-out / stop on open positions
EVOLVE   fitness = realized_pnl / √staked  →  cull the worst, breed replacements
         (crossover two winners + mutate); population size held constant
```

Fitness is **risk-adjusted P&L** (a Sharpe-like ratio), and only teams with enough resolved bets are eligible to be culled — so the population gradually concentrates on whatever *actually* makes money on each surface.

## In-play
Live games are detected from ESPN; the Brain re-prices full-period legs on **remaining minutes + current score/corners** (`live_prior`/`live_pfair`), and teams enter/exit intra-match using the **same exit logic** the live position-manager uses (so paper and live never drift).

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # add your Kalshi + Anthropic keys (see SETUP.md)
python3 models/fetch_stats.py # one-time: team ELO ratings
python3 models/train.py       # one-time: the win-probability model

python3 arena_v2.py --replay 30   # seed the population from resolved games
python3 arena_v2.py --once        # one live cycle (settle → scan → bet → exit → evolve)
python3 arena_v2.py --status      # leaderboard: fitness, P&L, params per team
```
Run `arena_v2.py --once` on a schedule to keep it trading live and evolving — see [DEPLOY.md](DEPLOY.md).

> ⚠️ **Replay seed numbers are unreliable** (they price off historical candlestick quotes, an imperfect proxy). Trust the *forward live* cycles, not the replay seed.

- Keys / wallet → [SETUP.md](SETUP.md) · Cloud 24/7 → [DEPLOY.md](DEPLOY.md) · Add your own agent → [ADDING_AN_AGENT.md](ADDING_AN_AGENT.md) · Design reasoning → [ARCHITECTURE.md](ARCHITECTURE.md) · Full plan → [BUILD_PLAN_V2.md](BUILD_PLAN_V2.md)

---

## Repository layout

A **shared core + version folders** structure — the standard way to keep two generations in one repo. Both versions reuse the same engine, so it stays at the root:

```
WorldCupTrading/
├── arena_v2.py  brain_v2.py  scanner_v2.py  strategy_v2.py        ← v2 (current system)
│   markets_v2.py  kalshi_client_v2.py  categories_v2.json
│   test_arena_v2.py  BUILD_PLAN_V2.md
│
├── group/             ← SHARED core library (used by both v2 and v1)
│   brain.py (ELO)  markets.py (Poisson)  kelly.py  exit_rules.py
│   paper.py  live_feed.py  dominance.py  env_portable.py
│   strategy.py / scanner.py / config.json   ← used only by v1
├── models/            ← SHARED data + models (ELO ratings, corners model + corpus)
├── kalshi_client.py   ← SHARED Kalshi REST client (v2 extends it)
│
├── README.md  ARCHITECTURE.md  SETUP.md  DEPLOY.md  ADDING_AN_AGENT.md
├── requirements.txt   .gitignore   .env.example
│
└── v1/                ← LEGACY: the original fixed 16-team arena (frozen)
    arena.py  backtest.py  run.py  categories.json  tournament_config.json
    agents/  scripts/  strategies/
```

**Why not "all v1 in `v1/`, all v2 at root"?** Both versions import the same `group/` + `models/` core, so that *can't* live under `v1/` without breaking v2. Keeping the shared engine at the root and putting only version-specific code in `v1/` (and at the root for v2) is the clean, conventional pattern. `v1/` reaches the shared core via a small path shim, so it still runs standalone.

---

## v1 — the legacy arena (in [`v1/`](v1/))

The original system: **16 fixed teams** (4 market categories × 4 strategy archetypes A/B/C/D), one shared Brain per category that blends an LLM + an ELO model + the market, with per-team learning ("Agent 3"). It pioneered the Brain-vs-teams split, the shared exit logic, Kelly+time-value sizing, and the catch-up-replay tournament — all of which v2 inherits and generalizes. Run it with `cd v1 && python3 arena.py --reset`. It's kept frozen for reference; **active development is on v2.**

---

## Roadmap
A goal-expectancy/xG model (the biggest edge unlock) · promotion to gated live execution · turning the LLM on (Design 2: priors + per-leg overrides) · activating events & player-props · faster in-play reactions. These are directions, not promises — the arena exists to let outcomes decide which pay.

## Disclaimer
For research and education. Prediction-market trading carries risk; markets can be illiquid and mispriced against you. Nothing here is financial advice. Run paper-only until you deeply understand the behavior, and never commit money you can't lose.
