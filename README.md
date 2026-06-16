# WorldCupTrading

A multi-agent system that trades **FIFA World Cup** markets on [Kalshi](https://kalshi.com). It runs a **paper tournament** of competing agent teams across several market types, learns from resolved games, and surfaces the best team per market as a candidate to promote to real money.

This repo is about the **design and the process**, not a frozen result. You can run it yourself: it scans every already-resolved game, prices it, paper-trades it with every team, and trains them — so you can reproduce the whole thing, watch the teams compete, and **add your own agent teams**.

> **Paper by default.** Nothing here places a real order unless you explicitly wire up live execution. The arena is a simulation that fills at real market bid/ask and settles at $1/$0.

---

## The core idea

A bet is only worth making when **your estimate of fair value beats the price you pay**. Everything here is built around making that judgment well and repeatedly, across many markets, with many different "personalities" competing to see which judgment + sizing style actually makes money.

So the system separates two things that are usually tangled together:

1. **The Brain** — *what is this worth?* One shared fair-value estimate per market (blends an LLM, a stats model, and the market itself).
2. **The agent teams** — *what do I do about it?* Each team sees the same fair value and differs only in **when it enters, how big it bets, and when it exits.**

That separation is the whole point: you can test many betting philosophies against the *same* honest price signal, and let realized outcomes decide which one wins.

---

## The workflow (every agent, every market)

```
   SCAN            EDGE                 SIZE              BET            EXIT            LEARN
 find open  →   Brain prices    →   Kelly + time-   →  place YES   →  manage in-   →  Agent 3 re-tunes
 markets        fair value vs       value discount      order          play: take      pricing weights
 (scanner)      the ask (brain,     decides $          (trader/         profit /        + each team's
                markets, edge)      (kelly)            arena)           stop / scale    kelly & min_edge
                                                                       out (keeper,     (tournament_
                                                                       exit_rules)      trainer)
```

| Stage | Question | Code |
|---|---|---|
| **Scan** | what's tradable? | `group/scanner.py` |
| **Edge** | what's it worth vs the price? | `group/brain.py` (+ `group/markets.py` for the Poisson model) |
| **Size** | how much to bet? | `group/kelly.py` (fractional Kelly + time-value discount) |
| **Bet** | place it | `agents/trader.py` / `arena.py` |
| **Exit** | manage it live | `group/exit_rules.py`, `agents/keeper.py` |
| **Learn** | get better from outcomes | `agents/tournament_trainer.py` (a.k.a. "Agent 3") |

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the reasoning behind each piece.

---

## The agent teams

Markets are grouped into **categories**, each with its own shared Brain, and every category fields the same four **strategy archetypes** plus an optional specialist:

**Strategy archetypes** (`group/strategy.py`):
- **A — Aggressive Hold:** bets the mean fair value with a low edge bar, sizes hard, holds to resolution.
- **B — Conservative Active:** bets a *discounted* fair value (uncertainty haircut), small size, exits actively.
- **C — Late Scalp:** only buys near-certain outcomes late in a game for small reliable gains.
- **D — Momentum:** enters *in-play* when the live model sees edge the market hasn't repriced yet.
- **Scoreline Specialist** (example of a *new* team): prices Kalshi's exact-score grid with an independent-Poisson model and combines all four behaviors. A worked example of how a new agent design is born — see [ARCHITECTURE.md](ARCHITECTURE.md#a-worked-example-the-scoreline-specialist).

**Market categories** (`categories.json`): `winner`, `spread`, `team_props`, `game_events`, `scoreline`.

Category × archetype = the field of competing teams. Best team per category is the promotion candidate.

---

## Quickstart

```bash
# 1. install
pip install -r requirements.txt

# 2. keys  (see SETUP.md for where to get them)
cp .env.example .env        # then edit .env with your Kalshi + Anthropic keys
#   put your Kalshi RSA private key at the path in KALSHI_PRIVATE_KEY_FILE

# 3. (one time) fetch team ratings + train the base model
python3 group/run.py --fetch-stats
python3 models/train.py

# 4. run the arena — scans every resolved game, paper-trades all teams, trains them
python3 arena.py --reset

# 5. run it again any time to pick up new games + keep learning (no --reset)
python3 arena.py
```

`arena.py --reset` replays all already-resolved World Cup games from 24h before kickoff through settlement, with every team trading on paper, then prints the standings (best team per category). Each later run catches up new games and keeps the teams learning.

- **Set up your keys / wallet →** [SETUP.md](SETUP.md)
- **Keep it running 24/7 on a cloud server →** [DEPLOY.md](DEPLOY.md)
- **Add your own agent team →** [ADDING_AN_AGENT.md](ADDING_AN_AGENT.md)

---

## Repo map

```
arena.py                 the multi-team paper tournament (scan + replay + learn)
kalshi_client.py         Kalshi REST client (RSA auth, orderbook, orders)
backtest.py              replay/discovery helpers (resolved games, price paths, ESPN match)
categories.json          which Kalshi series belong to which category + pricing mode
tournament_config.json   the agent teams and their starting params

group/
  brain.py        fair value = blend(LLM, stats model, market) + the in-play model
  markets.py      Poisson/Skellam fair values for spread/total/team-total/BTTS/exact-score
  kelly.py        fractional-Kelly sizing with a time-value discount
  strategy.py     the agent-team archetypes (entry/exit behaviors) + the registry
  exit_rules.py   the one shared in-play exit decision (used by paper + live)
  scanner.py      find candidate open markets
  paper.py        a simulated account (fills at real bid/ask, settles $1/$0)
  live_feed.py    live score/minute from ESPN
  dominance.py    in-play "who's dominating" multipliers from box-score stats
  run.py          single-agent live/dry-run entry point

agents/
  trader.py       sizes + places bets, launches the exit manager
  keeper.py       in-play exit manager (re-prices live, sells when overpaid)
  trainer.py      single-agent feedback loop
  tournament_trainer.py   "Agent 3" — per-team + per-category learning

models/           ELO ratings + the trained win-probability model
strategies/       templates + your own contributed agent teams
```

---

## Disclaimer
For research and education. Prediction-market trading carries risk; markets can be illiquid and mispriced against you. Nothing here is financial advice. Run paper-only until you deeply understand the behavior, and never commit money you can't lose.
