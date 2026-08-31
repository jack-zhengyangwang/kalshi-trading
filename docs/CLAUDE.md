# ⚠️ LEGACY — ebk-personal 4-Agent System (RETIRED)

> **This doc describes the v1/v2 4-agent system (Trader/Brain/Keeper/Trainer). It has been replaced by the v3 unified-pool arena.**
> See the root [CLAUDE.md](../CLAUDE.md) for the current system architecture and runbook.

# ebk-personal — World Cup Kalshi Betting System (LEGACY)

A personal, autonomous **4-agent** system that bets on FIFA World Cup games on
Kalshi and manages the in-play exit. Separate from the fund (`ebk-intel`) — these
are personal positions, no Rule Zero, no fund capital.

## Important

- Personal Kalshi account only. Auth = `KALSHI_KEY_ID` + RSA key in `MyPersonalAgent.txt`.
- **Kalshi needs no SOCKS5 proxy** (US-regulated). The proxy logic in
  `scripts/inplay_exit.py` only applies to the legacy Polymarket path.
- The LLM components bill to `ANTHROPIC_API_KEY` — separate from the Claude Code
  login. Make sure that key is the account you intend to pay from.
- Default is **dry-run**. Real orders only happen with `--execute`.

## The four agents

```
Trader  (agents/trader.py)   scan markets → ask Brain for p_fair → size with
                             Kelly+TVM → place YES order → launch the Keeper
Brain   (group/brain.py)     fair value: pre-game blend AND a live in-play blend
Keeper  (agents/keeper.py)   in-play exit manager — re-prices the position live
                             and sells when the market overpays vs. live fair
Trainer (agents/trainer.py)  reads resolved outcomes + exit counterfactuals →
                             retunes weights, Kelly, min_edge, exit thresholds,
                             and retrains the model
```

### Brain — pre-game and live fair value
Pre-game `evaluate()` blends three sources with learnable weights
(`config["brain_weights"]`, renormalized if one is unavailable):
1. **LLM** (`claude-haiku-4-5`) — prompted with the specific leg it's pricing.
2. **Trained model** (`models/model.pkl`) — multinomial logistic regression on
   team ELO → `[P(away win), P(draw), P(home win)]`; the right class is selected
   by the market's `yes_sub_title`.
3. **Market data** — orderbook mid + volume skew.

Live `evaluate_live(market, game_state)` (used by the Keeper) blends:
1. **Analytic in-play WP** — Skellam/Poisson model of remaining goals from the
   current score margin + minutes left (`_inplay_wp`); reflects live state the
   pre-game model is blind to (a favorite losing late is correctly priced low).
2. **Live market mid** — refreshed each poll.
3. **LLM-live** — throttled: only re-queried when the score changes.

### Trader — sizing & execution
- `kelly.size_detail()` — `f = edge/(1-p)` × `kelly_fraction` × TVM discount
  `e^(-tvm_rate·weeks)`. Bets on the conservative `p_fair_lo`.
- Per-bet cap `max_bet_dollars` ($20). If uncapped Kelly wants **more**, it
  **pings** you (macOS notification + console + `logs/alerts.jsonl`).
- Places **YES** orders only. Each game is three YES markets (home/draw/away),
  so YES-only covers every view.
- After a fill, launches the **Keeper** as a detached background process for that
  position (legacy `ebk-exit` only for non-parseable / Polymarket markets).

### Keeper — intelligent in-play exit
Polls every `live_poll_secs` while the game is live. Each poll it gets the live
game state (`group/live_feed.py`, ESPN free API), asks the Brain for live fair
value of our leg, reads the current sellable bid, and decides:

| Action | Condition | Result |
|--------|-----------|--------|
| OVERPRICED | `bid > live_fair + exit_sell_margin` | scale out (sell more if very overpriced) — strongest +EV signal |
| TAKE_PROFIT | `(bid-entry)/entry ≥ exit_take_profit_pct` | scale out `exit_fraction` |
| STOP | `live_fair < exit_stop_fair` | exit fully — our outcome is dead per the LIVE model (replaces the dumb price-floor panic) |
| HOLD | otherwise | residual rides to resolution |

Every action is logged to `logs/exits.jsonl` with a full snapshot for the
Trainer's counterfactual analysis.

### Trainer — the feedback loop
Run after games resolve (`python3 group/run.py --train`). It:
- Reweights the 3 Brain sources by **inverse Brier score** (with momentum).
- Nudges `kelly_fraction` by recent ROI; `min_edge` by calibration gap.
- **Exit counterfactual:** for each Keeper exit, compares the bid we sold at vs.
  the eventual $1/$0 outcome. If selling beat holding on average → tighten exit
  thresholds (sell sooner); if holding won → loosen them.
- Appends one 3-class row per resolved game to the training CSV and **retrains**
  the model.

All learned params persist in `trainer_state.json`, merged over `config.json`
at startup by `run.py` (and by the Keeper at launch).

## Kalshi World Cup market structure

Each game = three separate binary YES/NO markets. Example "Czechia vs Mexico
Winner?": `…CZEMEX-CZE` (Czechia wins), `…CZEMEX-MEX` (Mexico wins),
`…CZEMEX-TIE` (draw). `yes_sub_title` is the authoritative label for what YES
means; home = the first team in the title. The three legs' fair probabilities
sum to ~1.0.

## Folder structure

```
ebk-personal/
├── CLAUDE.md
├── kalshi_client.py          REST wrapper (RSA/Bearer auth, buy/sell, orderbook)
├── ebk-exit                  legacy one-command exit-monitor launcher
├── scripts/inplay_exit.py    legacy price-trigger exit monitor (+ KalshiAdapter, reused by Keeper)
├── MyPersonalAgent.txt       RSA private key (auth) — do not delete
├── trainer_state.json        learned params (weights, kelly, min_edge, exit_*)
│
├── group/
│   ├── config.json           tunable parameters
│   ├── scanner.py            find candidate open markets
│   ├── brain.py              Brain — pre-game blend + evaluate_live (in-play)
│   ├── live_feed.py          live game state from ESPN (score/minute/status)
│   ├── kelly.py              Kelly + TVM sizing (size_detail returns raw/capped)
│   ├── notify.py             "ping" helper (macOS notification + alerts log)
│   └── run.py                orchestrator / entry point
│
├── agents/
│   ├── trader.py             Trader agent (entry)
│   ├── keeper.py             Keeper agent (in-play exit) — also a CLI process
│   └── trainer.py            Trainer agent (feedback)
│
├── models/
│   ├── fetch_stats.py        download team ELO → team_elo.json (clubelo, w/ fallback)
│   ├── train.py              train multinomial WP model → model.pkl
│   ├── wc_matches_1990_2022.csv  training data (3-class: 0 away / 1 draw / 2 home)
│   ├── team_elo.json         current team ELO ratings
│   └── model.pkl             trained pipeline (StandardScaler + LogisticRegression)
│
└── logs/
    ├── group_YYYYMMDD.jsonl  one line per entry decision (BET/PASS)
    ├── exits.jsonl           one line per Keeper exit (for counterfactuals)
    ├── alerts.jsonl          cap-exceeded / exit pings
    └── keeper_<ticker>.log   per-position Keeper stdout
```

## Config (`group/config.json`)

| Key | Meaning |
|-----|---------|
| `min_volume`, `min_edge` | candidate filters |
| `kelly_fraction` | fractional-Kelly multiplier (Trainer-tuned, 0.10–0.35) |
| `tvm_rate` | time-value discount rate per week |
| `max_bet_dollars` | per-bet cap ($20); exceeding it pings you |
| `min_bet_dollars` | floor for a non-zero bet |
| `brain_weights` | `{w_llm, w_model, w_data}` (Trainer-tuned) |
| `exit_take_profit_pct` | scale out when profit ≥ this % of entry (Trainer-tuned) |
| `exit_sell_margin` | sell when bid exceeds live fair by this (Trainer-tuned) |
| `exit_stop_fair` | exit when live fair drops below this |
| `exit_fraction` | fraction sold on a scale-out |
| `live_poll_secs` | Keeper poll interval |
| `sell_high / sell_all_hi / sell_panic` | legacy `inplay_exit.py` triggers only |
| `claude_model` | LLM for the Brain |

## Running it

```bash
cd ~/Desktop/EBK/ebk-personal

# one-time setup
pip3 install anthropic scikit-learn pandas requests cryptography
python3 group/run.py --fetch-stats     # refresh team ELO (needs network)
python3 models/train.py                # train the model

# dry run — prints BET/PASS, places no orders, Keeper runs in dry mode
KALSHI_KEY_ID=... ANTHROPIC_API_KEY=... python3 group/run.py --once

# LIVE — real orders; each fill spawns a Keeper that auto-manages the exit
KALSHI_KEY_ID=... ANTHROPIC_API_KEY=... python3 group/run.py --execute

# after games resolve — update params (incl. exit thresholds) + retrain
python3 group/run.py --train
```

Flags: `--once` (single scan), `--execute` (live), `--train` (Trainer only),
`--fetch-stats` (refresh ELO only).

## Notes / open items

- `team_elo.json` falls back to hardcoded ratings if clubelo.com is unreachable;
  re-run `--fetch-stats` on a live network for current values.
- The training CSV is hand-built approximate prior data; the Trainer refines it
  with real outcomes over time. The in-play WP is analytic (Poisson) — it can be
  replaced later with a model trained on logged live games.
- No portfolio-level cap yet (each bet is capped, but one scan can place multiple
  ≤$20 bets). Add `max_open_positions` / `max_cycle_dollars` if desired.
- The Keeper relies on ESPN's free `fifa.world` scoreboard feed for live state.
