# WorldCup Trading — Autonomous Kalshi Betting System (v4 Brain)

## Context

An autonomous system that scans **every open Soccer market on Kalshi** (tag-based discovery via `tags=['Soccer']`, no hardcoded menu), prices winner legs (home/draw/away) with a per-league blend of structural Poisson models + optional LLM + market data, sizes bets via Kelly with hard per-bet caps, and evolves a population of ~13 strategy agents through a genetic algorithm. A separate, heavily-guarded real-money execution bus routes proven agents to live Kalshi orders.

Originally built for World Cup only (regex `^KXWC`). **v4 pivots to all soccer leagues with per-league brains** — 6 brain instances (EPL, LaLiga, SerieA, Bundesliga, Ligue1, Other), each with league-specific calibration, stacker weights, and LLM prompts. Winner-only pricing for the deepest, most liquid market.

The system is at **v4** (per-league brains, winner-only, v3 archive). The old 4-agent system (Trader/Brain/Keeper/Trainer), v2 per-category arena, and v3 multi-type pricing are retired/legacy/archived.

## Current State (last reviewed 2026-08-25)

**Read this first.** Status snapshot from a full read-only audit of the repo. Update it when the facts below change.

### Money

**DISARMED.** `config/switchboard_v3.json` → `master.armed = false`, `master.kill = false`. No real orders can be placed. A real order requires `armed && --execute && !kill` (`wc/cycle.py:382`).

### Git / backup status

- Working branch: **`per-game-cap`**. Branches: `main`, `Dev`, `restructure`, `per-game-cap`, `structural-support` — all committed work is pushed and matches `origin/`.
- **Last commit: 2026-07-03** (`d294ab3`, "Add per-game (per-event) spend cap"). GitHub `pushedAt` confirms the same date.
- **The entire v4 all-soccer pivot (2026-08-10) is uncommitted** — 13 untracked, 13 modified, 37 deleted (35 of those are moves into `v3_archive/`, which git records as delete+add).
- Repo is **PRIVATE** on GitHub (`jack-zhengyangwang/WorldCupTrading`), so the droplet IP in this file is not publicly exposed.
- `.gitignore` now also excludes `.claude/` (machine-local paths, ssh probes) and `v3_archive/models/*.csv`. Note: this drops the two CSVs out of version control; they remain on disk and in older commits.

### Environment gotcha — iCloud eviction breaks git

`~/Desktop` is iCloud-synced. Files get evicted to `dataless` placeholders, and **git hangs** (`status`, `log`, even with pager and locks disabled) because it must hash every worktree file and blocks materializing them.

- Symptom: `git status` runs for minutes with no output; stale `.git/index.lock` accumulates; an editor integration respawns `git ls-files` constantly.
- Diagnose: `find . -type f -not -path './.git/*' -exec ls -lO {} + | awk '$5 ~ /dataless/ {print $NF}'`
- Fix: `cat` each dataless file to materialize it (~11s each), then `rm -f .git/index.lock`.
- **Files re-evict within minutes, so this recurs.** Durable fix is moving the repo off `~/Desktop`, or disabling "Optimize Mac Storage". Source `.py` files stay local and read in ~4ms; docs and dotfiles are what get evicted.

### Known bugs / open items

| # | Item | Detail |
|---|------|--------|
| 1 | **v4 brain not wired in** | `arena.py`/`cycle.py`/`promote.py` still use `BrainV2` — WC-worded LLM prompts and `WC_BASE_WEIGHT=0.25` still active. `brain_v4.py` is referenced only by `tests/test_brain_v4.py`. |
| 2 | **9 modules bypass `wc/paths.py`** | `BASE = os.path.dirname(__file__)` now resolves to `.../WorldCupTrading/wc`, not the repo root, so joins land one or two levels too deep. Files: `telegram_bot.py:20`, `scanner.py:26`, `promote.py:36`, `markets.py:33`, `pilot_status.py:15`, `prediction_db.py:23`, `investigate.py:14`, `core/promote_base.py:31`, `core/arena_base.py:36`, `lib/brain_model.py:25`. Several import `paths` *and* keep the stale constant. |
| 3 | **`deploy.sh` targets the wrong directory** | Deploys to `root@147.182.237.14:/root/ebk-personal`, but the live v3 pilot runs from `/root/WorldCupTrading`. Deploying today writes to the wrong folder and silently has no effect — this is the "my change didn't take" / "disarm didn't work" bug class. |
| 4 | **`per_game_cap_dollars` missing** | Read at `wc/cycle.py:235` but absent from the current `switchboard_v3.json` (the 2026-07-03 commit added it; an uncommitted edit removed it). |
| 5 | **Doc drift** | `README.md` still says "every open World Cup market". `RECAP.md` commands predate the `wc/` layout. `docs/DEPLOY_CLOUD.md` says `/opt/ebk-personal`. `ARCHITECTURE.md`, `DEPLOY.md`, `SETUP.md`, `ADDING_AN_AGENT.md` are 0-byte stubs. `edges/discover.sh` is referenced but does not exist. |
| 6 | **Two gates on the droplet, not three** | `scripts/run_promote.sh` hardcodes `--execute`, so in cron the only live gates are `master.armed` and `master.kill`. The "three independent switches" claim below holds only for manual invocation. |
| 7 | **Double-run risk** | `arena.py --once` and `promote.py` both delegate into `wc/cycle.py`. Running both cron scripts concurrently runs the paper arena twice. |
| 8 | **Droplet unexamined** | No one has SSH'd in since the pilot. Unknown whether it is running, what its live config says, or what the World Cup pilot's results were. The safety guard was set to stop on 2026-07-20 (World Cup end) or −$200. |

### Evidence quality reminder

Per `edges/failed/slow-poll-in-play-scalping.md`: the in-play **+$460** in `RECAP.md` is **not alpha** — 167 bets held to settlement at 76% win, 90–180s behind ESPN, i.e. betting on nearly-decided games. The trustworthy figure is **pre-game +$31 at 54%**. In-play is paper-only and barred from promotion.

## Pivot from World Cup → All Soccer (2026-08-10)

### The Problem

The system was hardcoded to World Cup in ~50 places across 8 files. The single chokepoint was `_KXWC_RE = re.compile(r"^KXWC")` in `scanner.py` — it fetched ALL sports from Kalshi, then threw away everything except `^KXWC`.

### The Insight (from Kalshi ticker conventions)

Kalshi tickers follow `Series → Event → Market`: e.g. `KXEPLGAME-25AUG16ARSMCI-ARS`. The Kalshi API returns `category` and `tags` fields per series. **Every soccer series has `tags=['Soccer']`** regardless of league. And all leagues use consistent suffixes: `*GAME` (winner), `*TOTAL` (goals), `*SPREAD` (margin), `*BTTS`, `*CORNERS`, `*1H` (1st half), `*ADVANCE`, etc.

This means the system can discover and price ANY soccer league without knowing its ticker prefix in advance — just filter by tag, classify by suffix.

### Changes Made (7 files, ~20 edits)

| File | Change | Why |
|------|--------|-----|
| `wc/scanner.py` | `^KXWC` regex → `tags=['Soccer']` filter. Renamed `discover_kxwc_series` → `discover_soccer_series`. Added `iter_game_series()`, `iter_total_series()`, `iter_corner_series()`, `get_settled_total_legs()`, `get_settled_corner_legs()`, `_find_game_series()`, `_context_series()`, `_find_and_cache_context_series()`. Updated `_cfg()` to accept client for dynamic suffix resolution. Updated `series_for()` to resolve suffix patterns against discovered series. Removed dead `_all_curated_series()`. Updated `implied_total()` and `implied_corner_mean()` to use suffix matching (`_is_full_total_series()`, `_is_full_corner_series()`) instead of hardcoded `KXWCTOTAL`/`KXWCCORNERS`. Updated `price_games()` to find home/away from ALL game-level series, not just `KXWCGAME`. Updated `game_series()` to dynamically find the first available GAME series. Updated CLI `--discover` output. | **The gate**: without this, only World Cup series are visible |
| `wc/markets.py` | `SERIES_SPEC` dict (25 hardcoded `KXWC*` prefixes) → `_SUFFIX_SPEC` ordered list + `_classify_series()` function with cache. Matches any ticker prefix by suffix: `KXEPLGAME` → `GAME` → `("winner", "full")`, `KXUCL1HTOTAL` → `1HTOTAL` → `("total", "1H")`, etc. Works for any soccer league automatically. | **The classifier**: without this, non-WC tickers are `"unknown"` and unpriced |
| `config/categories_v2.json` | Hardcoded KXWC series lists → suffix patterns. `"winner": {"series": ["KXWCGAME"]}` → `"suffixes": ["GAME"]`. Added `knockout` category. `series_for()` resolves suffixes against discovered series dynamically — new leagues appear automatically. | **The categories**: agents bet on types (winner, total, spread...) across ALL leagues |
| `wc/arena.py` | `client.list_markets_by_series("KXWCGAME")` → `scn.iter_game_series(client)`. `settled.get("KXWCTOTAL", ...)` → `scn.get_settled_total_legs(settled, ec)`. | Replay path works for all leagues |
| `wc/promote.py` | `client.list_markets_by_series("KXWCGAME")` → `scn.iter_game_series(client)`. | Real-money path works for all leagues |
| `wc/cycle.py` | Same as promote.py. | Paper forward path works for all leagues |
| `wc/match_sim.py` | `client.list_markets_by_series("KXWCGAME")` → `scn.iter_game_series(client)`. `_anchor(... "KXWCTOTAL" ...)` → `_anchor_multi(..., scn._is_full_total_series, ...)`. `CONTEXT_SERIES` → `scn._context_series()`. | Match simulation works for all leagues |
| `wc/investigate.py` | `p[0].replace("KXWC", "")` → strips any `KX*` prefix for display. | Display works for all leagues |

### TDD Process (v4 Brain Build)

All changes verified with tests before and after each edit:

```bash
# Baseline before any v4 changes
python3 -m pytest tests/ -v    # 32 passed (test_markets, test_strategy, test_tradetape)

# After each new module — verify no regressions
python3 -c "import wc.lib.elo_index"   && echo "OK"
python3 -c "import wc.brain_v4"        && echo "OK"
python3 -c "import wc.scanner"         && echo "OK"
python3 -c "import wc.brain"           && echo "OK"
python3 -c "import wc.arena"           && echo "OK"
python3 -c "import wc.cycle"           && echo "OK"
python3 -c "import wc.promote"         && echo "OK"

# After each test file — verify green
python3 -m pytest tests/test_elo_index.py -v    # 10 passed
python3 -m pytest tests/test_brain_v4.py -v     # 19 passed
python3 -m pytest tests/test_scanner_v4.py -v   # 3 passed

# Final check — all 77 tests green
python3 -m pytest tests/ -v    # 77 passed (all green)
```

### Architecture After the Pivot

```
DISCOVER  ── scanner.discover_soccer_series() — every series with tags=['Soccer']
                ↓
CLASSIFY  ── markets._classify_series() — suffix match: KXEPLGAME→winner, KXUCLTOTAL→total, ...
                ↓
CATEGORIZE ── series_for() — resolve category suffix patterns against discovered series
                ↓
BRAIN     ── BrainV2.pfair() — logit stack of {data model, LLM(optional), market mid}
                ↓
STRATEGIST ── strategy_v3.Strategist.entry_size() — Kelly + tilt + unit cap + price band
                ↓
EXECUTOR  ── Paper account (arena) or REAL Kalshi order (promoter)
                ↓
PREDICTION DB ── prediction_db.log_prediction() / grade() — rights/wrongs ledger
                ↓
RESOLVE   ── Settle against Kalshi result ($1 YES / $0 NO)
                ↓
LEARN     ── Brain Assistant retunes stacker weights + GA evolves strategist params
```

Key architectural properties:
- **New league appears on Kalshi** → discovered automatically (tag filter). Classified by suffix. Included in categories. Agents bet on it. Zero config changes.
- **Categories are type-based, not league-based**: `winner` = any `*GAME` series (EPL, UCL, La Liga, WC, etc.), not just KXWCGAME.
- **Suffix matching is ordered**: longer suffixes checked first (`1HTOTAL` before `TOTAL`, `TEAMTOTAL` before `TOTAL`).
- **Bootstrap fallback**: if no client/discovery available, `_cfg()` returns raw suffixes so the system can start.

### What Still Has World Cup / v3 Legacy Assumptions

These are addressed in v4 brain but still present in arena/cycle/promote (follow-up work):

| Item | File | Status |
|------|------|--------|
| `WC_OVER_BASE` / `WC_BASE_WEIGHT` | `arena.py:100-101` | **Still active** in v3 arena/cycle. v4 BrainV4 doesn't use it (winner-only, no totals). Will be removed when arena ports to v4. |
| LLM prompts say "World Cup match" | `brain.py:306` | **Fixed in v4** — BrainV4 uses league-aware prompts ("Premier League match: ..."). Old BrainV2 still has WC prompts but will be archived when arena/cycle/promote port to v4. |
| ELO ratings are national teams only | `models/team_elo.json` | **Fixed in v4** — `EloIndex` loads `club_elo.json` (630 clubs) merged with national teams (58). Club teams now resolve correctly. |
| Per-category brains (not per-league) | `brain.py`, `arena.py` | **Fixed in v4** — BrainV4 has 6 per-league instances, each with own calibration and weights. Old BrainV2 still used by arena/cycle/promote until follow-up. |
| 10+ market types priced | `brain.py`, `markets.py` | **Fixed in v4** — BrainV4 is winner-only. Scanner filters to winner legs. Old multi-type code still present but not called by v4 path. |

## Environment

- **Python**: `python3` (venv at `venv/`)
- **Key env vars**: `KALSHI_KEY_ID` + RSA key in `MyPersonalAgent.txt` (Kalshi auth), `ANTHROPIC_API_KEY` (LLM), `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (bot)
- **Shell**: zsh on macOS
- **Kalshi needs no SOCKS5 proxy** — US-regulated exchange
- **Real money is DISARMED by default** — requires 3 independent switches (§Safety below)

## Ground Rules

1. **Ask before acting on real money** — Any change to `switchboard_v3.json`, the promotion path, or order execution requires explicit user sign-off. Paper arena changes are free.
2. **Append-only logs for all decisions** — `logs/promote_v3.jsonl` (real orders), `logs/predictions.jsonl` (priced legs), `logs/arena_v3.jsonl` (paper tally), `logs/evolution_v3.jsonl` (breeding events). Never mutate a logged row; append and grade.
3. **Safety-first for real money** — A real order is placed ONLY when ALL THREE hold: `master.armed = true` AND `master.kill = false` AND `--execute` flag. Otherwise: dry-run mirror (logs intended orders, places none).
4. **One source of truth per concept** — The v3 plan audit found 3× duplicated entry/exit/settle paths. When touching any of these, consolidate toward the single shared path rather than adding a 4th copy.
5. **Supervisor/Worker separation** — Orchestrator-level code (arena loop, promoter run) owns loops/retries/state. Worker code (brain, strategy, markets, kelly) is stateless: receive inputs → compute → return. The Brain's LLM cache is the one exception (performance-critical).
6. **Idempotent operations** — Real positions are read from Kalshi as source of truth every cycle. A crash/restart mid-cycle must not double-bet: next cycle re-reads real positions and continues. The prediction DB grader is idempotent (skips already-graded tickers).
7. **Python** — The codebase is Python. No Go/Elixir unless explicitly discussed.
8. **TDD (Test-Driven Development)** — Write a failing test before the implementation. Every new v4 feature or bugfix ships with tests that exercise the actual code path. 77 tests across 6 files, all green.
9. **Optimistic locking on shared mutable state** — When writing to a file or in-memory structure that could be read or written by another cycle or process, include a version/sequence guard: read → check version hasn't changed → write (incrementing version). If the version moved underneath, abort and re-read from source of truth. Applies to: `switchboard_v3.json` (real-money config), `arena_v3_state/` team files, and any per-ticker exit re-arm flags. Does NOT apply to append-only JSONL logs (no mutation, no lock needed) or Kalshi positions (Kalshi is the source of truth, not local state).
10. **TCC events for multi-step operations** — Every multi-step operation that spans an external boundary (especially real-money order placement) follows Try-Confirm-Cancel:
    - **Try** — reserve the resource / validate preconditions / log intent. On failure: abort, nothing persisted.
    - **Confirm** — execute the irreversible step (place order on Kalshi, record in promote_v3.jsonl). Idempotent: if replayed after a crash, the reconciler detects the order already exists (Kalshi positions as source of truth) and skips.
    - **Cancel** — if any Confirm step fails, unwind reserved resources (release fuse capacity, log the unwind). Never leave a reserved-but-unconfirmed resource stranded.
    This is the pattern behind the reconciler in `live_promote_v3`: Try = compute intent + check fuses → Confirm = place order + log → Cancel = release fuse cap on failure. The same pattern should govern any future multi-step operation (portfolio rebalance, batch exit, evolution breed+replace).

## Project File Structure

```
WorldCupTrading/
├── CLAUDE.md                     # This file — agent instructions
├── MyPersonalAgent.txt           # RSA private key (auth) — NEVER commit
├── wc/
│   ├── __init__.py
│   ├── paths.py                  # Central path config — all dirs defined here
│   ├── arena.py                  # Arena v3: replay + forward cron loop + evolution (still v3)
│   ├── brain.py                  # BrainV2: v3 multi-type brain (still used by arena/cycle/promote)
│   ├── brain_v4.py               # BrainV4: v4 per-league winner-only brain ★ NEW
│   ├── markets.py                # markets_v2: suffix-based leg classifier + Poisson/NB pricing
│   ├── scanner.py                # scanner_v2: tag-based discovery + per-game pricing + league routing
│   ├── strategy.py               # strategy_v3: price-band, unit-cap, favorite-tilt
│   ├── promote.py                # live_promote_v3: real-money execution bus (still v3)
│   ├── cycle.py                  # Unified v3 cron loop (still v3)
│   ├── match_sim.py              # In-play match simulation (ESPN goals + candlesticks)
│   ├── prediction_db.py          # Append-only JSONL predictions ledger
│   ├── tradetape.py              # Historical trade-tape fill model
│   ├── pilot_status.py           # Live snapshot of armed real-money positions + P&L
│   ├── telegram_bot.py           # Telegram bot for remote monitoring/control
│   ├── investigate.py            # Detail view of one agent's bets
│   ├── core/
│   │   ├── arena_base.py         # Arena v2 (legacy — some utilities still used by arena.py)
│   │   └── promote_base.py       # live_promote_v2 (legacy — some utilities still used by promote.py)
│   ├── kalshi/
│   │   ├── client.py             # Kalshi REST API v2 (RSA auth, orders, positions)
│   │   └── client_ext.py         # KalshiClientV2: throttle, retry, batched fetch, WS
│   └── lib/
│       ├── brain_model.py        # Legacy Brain (ELO→3-way LR, LLM calls. Still used by BrainV2)
│       ├── elo_index.py          # EloIndex: shared ELO resolution for v4 brains ★ NEW
│       ├── state_utils.py        # kalshi_maintenance, load_json, save_json ★ NEW
│       ├── dominance.py          # Live box-score → Poisson scoring multipliers
│       ├── exit_rules.py         # Shared re-armed exit logic (STOP/OVERPRICED/TAKE_PROFIT)
│       ├── kelly.py              # Fractional Kelly + TVM discount sizing
│       ├── live_feed.py          # ESPN free scoreboard API (game state + stats)
│       ├── market_data.py        # Goal-rate math + adjacent market fair values
│       └── paper.py              # PaperAccount: simulated trading ledger
├── models/
│   ├── corners_model.json        # Trained corner model (consumed by markets_v2)
│   ├── model.pkl                 # Trained winner model (legacy, not used by v3)
│   ├── team_elo.json             # National team ELO ratings (58 teams)
│   └── club_elo.json             # Club ELO ratings (630 teams, used by EloIndex)
├── config/
│   ├── categories_v2.json        # Super-category suffix-pattern definitions (v3)
│   ├── leagues.json              # Per-league calibration for v4 brains ★ NEW
│   ├── switchboard_v3.json       # Real-money patch-panel (ARMS REAL MONEY)
├── tests/
│   ├── test_markets.py           # Market parser + fair_yes_v2 tests (12 tests)
│   ├── test_strategy.py          # Strategy v3 entry sizing tests (12 tests)
│   ├── test_tradetape.py         # Trade-tape fill math tests (8 tests)
│   ├── test_elo_index.py         # EloIndex loading + resolution tests (10 tests) ★ NEW
│   ├── test_brain_v4.py          # BrainV4 per-league pricing tests (19 tests) ★ NEW
│   └── test_scanner_v4.py        # League routing tests (3 tests) ★ NEW
├── v3_archive/                   # Dead v2/v3 code moved here ★ NEW
│   ├── core/                     # strategy_base.py
│   ├── lib/                      # (reserved)
│   ├── config/                   # switchboard_v2.json, trainer_state.json
│   ├── models/                   # train scripts + training datasets
│   └── test_arena_v2.py          # Stale test
├── arena_v3_state/               # Persisted team/brain/meta state (v3)
├── arena_v3_cache/               # Per-ticker trade-tape cache
├── logs/                         # JSONL logs (empty until runtime)
├── edges/                        # Edge research: proposed/working/failed thesis docs
└── legacy/
    └── v1/                       # v1 island (dead code)
```

## Architecture: The Loop

```
DISCOVER  ── scanner.discover_soccer_series() — every series with tags=['Soccer']
                ↓
CLASSIFY  ── markets._classify_series() — suffix match on ticker prefix
                ↓
CATEGORIZE ── scanner.series_for() — resolve category suffixes against discovered series
                ↓
PRICE     ── scanner.price_games() — per-game: pull legs, anchor totals, price each leg
                ↓
SIZE      ── strategy_v3.Strategist.entry_size() — Kelly + tilt + unit cap + price band
                ↓
EXECUTE   ── Paper account (arena) or REAL Kalshi order (promoter)
                ↓
LOG       ── prediction_db.log_prediction() — append to predictions.jsonl
                ↓
RESOLVE   ── Settle against Kalshi result ($1 YES / $0 NO)
                ↓
LEARN     ── Brain Assistant retunes stacker weights + GA evolves strategist params
```

### Discovery & Classification (v4)

**Discovery**: `discover_soccer_series()` queries Kalshi's `/series?category=Sports`, iterates all series, keeps only those with `tags=['Soccer']`. Cached in `arena_v3_state/soccer_series_cache.json` with a TTL. The `context_series` (GAME/TOTAL/CORNERS series for metadata anchoring) are auto-detected and cached alongside.

**Classification**: `_classify_series()` in markets.py matches the series ticker prefix against an ordered list of suffixes. Longer suffixes checked first:
- `GAME` → `("winner", "full")` — match winner for any league
- `TOTAL` → `("total", "full")` — goal totals (but `1HTOTAL` matches first → `("total", "1H")`)
- `SPREAD` → `("spread", "full")`
- `CORNERS` → `("corners", "full")` (but `TCORNERS` matches first → `("team_corners", "full")`)
- `ADVANCE`, `MOV`, `MOF` → knockout progression
- etc.

**Categories**: `categories_v2.json` defines super-categories by suffix patterns:
- `winner`: any `*GAME` series → all leagues
- `game_lines`: `*SPREAD`, `*TOTAL`, `*TEAMTOTAL`, `*BTTS`, `*1H*`, `*2H*` → all leagues
- `game_props`: `*SCORE`, `*CORNERS`, `*TCORNERS`, `*FTTS` → all leagues
- `knockout`: `*ADVANCE`, `*MOV`, `*MOF` → tournament progression
- `player_props`: `*FIRSTGOAL`, `*AST`, etc. → stubbed (inactive)

`series_for("all", client)` returns the entire discovered surface. `series_for("winner", client)` returns every GAME series across every league. A new league on Kalshi → discovered automatically → classified by suffix → included in categories.

### Brain v4 (the pricing engine) ★ CURRENT

**Architecture**: 6 independent `BrainV4` instances, one per league (EPL, LaLiga, SerieA, Bundesliga, Ligue1, Other). Each instance carries:

- **League-specific calibration**: `base_goals_per_side` from `config/leagues.json` — replaces the old hardcoded `_BASE_GOALS=1.35`. EPL uses 1.42 (higher scoring), LaLiga uses 1.25 (lower scoring).
- **Shared ELO index**: `EloIndex` loads 688 teams (58 national + 630 club from `club_elo.json`). Club teams now resolve correctly — no more `elo_known=False` for club leagues.
- **Per-league stacker weights**: Tuned independently from that league's resolved outcomes via `update_stacker()`. EPL data model might be more accurate than Serie A — each league's weights reflect that.
- **Per-league LLM cache**: Prompts are league-aware (`"Premier League match: {home} vs {away}"` instead of `"World Cup match"`).
- **Winner-only**: Prices only home win / draw / away win. The deepest, most liquid market.

Pipeline: `ELO → league-calibrated goal rates → result_probs() → p_data` + optional `LLM → p_llm` + `market mid → p_mkt` → **linear logit stacker** → `p_fair`.

**Public API** (same shape as BrainV2 — existing callers compile unchanged):
- `game_prior(home, away)` → per-league goal rates
- `pfair(parsed, prior, leg_is_home, market_mid, llm)` → stacked p_fair for winner legs
- `live_prior(...)`, `live_pfair(...)` → in-play pricing
- `llm_for_game(...)`, `llm_live(...)`, `llm_pfair(...)` → league-aware LLM
- `update_stacker(resolved)` → Brain Assistant retunes weights

**State**: `EloIndex` (read-only after init, safe to share across all 6 brain instances). `BrainV4` instances are stateless except for `weights` and `llm_cache` (persisted to disk per league).

### Brain v2 (legacy — still used by arena/cycle/promote until follow-up)

Per-category multi-type brain. Same stacker/LLM architecture but: no league awareness, World Cup LLM prompts, prices 10+ market types. Will be archived when arena/cycle/promote port to v4.

### Strategy v3 (the bet sizing)

9 seed archetypes: `favorite`, `totals`, `corners`, `value_hunter`, `flow`, `aggressive_hold`, `conservative_active`, `late_scalp`, `momentum`.

Key params (all evolvable):
- **Price band** (`price_floor`/`price_ceiling`) — structural guard against longshots and no-edge near-locks
- **Unit cap** (`unit_cap_frac`) — hard per-bet limit as fraction of bankroll
- **Favorite-tilt** (`alloc_tilt`) — stake ∝ p_fair^tilt, favors high-probability legs
- **Edge-in-sigma** (`edge_sigma_k`) — require edge ≥ k × sigma
- **Market focus** — specialize (e.g. totals-only, corners-only) or bet all types
- **Exit params** — `exit_take_profit_pct`, `exit_sell_margin`, `exit_stop_fair`, `exit_fraction`

### Evolution

Every `EVOLVE_EVERY=12` resolved games:
1. Rank by **shrinkage fitness** (ROI × n/(n+K)) — prevents lucky 2-bet micro-stakers from topping the board
2. **Breed** top pairs (1×2, 3×4) → 2 children (crossover + mutate)
3. **Cull** absolute worst 2 (removed from population)
4. **Nudge** next-worst 4 toward the elite (params blended with best, small mutation)
5. Children get fresh $100; population stays at 13

## Safety (Real Money)

Live in [live_promote_v3](wc/promote.py). Three independent switches:

| Switch | Location | Effect when false |
|--------|----------|-------------------|
| `master.armed` | `switchboard_v3.json` | Dry-run mirror: logs intended orders, places none |
| `master.kill` | `switchboard_v3.json` | Cancel-all + halt. If `kill_flattens`: sell everything at market |
| `--execute` | CLI flag | No real orders without it |

Additional fuses:
- **`master.daily_cap_dollars`** — hard daily spend limit across all agents
- **`master.per_game_cap_dollars`** — max spend per event/game across all agents
- **`master.hard_stop_loss_pct`** — circuit-breaker: if position value ≤ (1-hard%) × entry → flatten
- **Per-slot**: `max_bet`, `max_open`, `fuse_per_cycle`, `member_capital`
- **Exit management runs even when disarmed** — disarming/kill never abandons open real positions
- **`--execute` required even for exits** — real-position reads gate on it

## Key Design Decisions

| Decision | Why |
|----------|-----|
| **Tag-based discovery, not regex** | `tags=['Soccer']` catches every soccer league Kalshi ever adds. Regex `^KXWC` only caught World Cup. The Kalshi ticker convention docs say to use `tags`/`category` fields, not parse ticker strings. |
| **Suffix-based classification, not prefix-based** | All soccer leagues use the same market-type suffixes (`*GAME`, `*TOTAL`, `*SPREAD`, etc.). Matching on suffix means a new league works instantly with zero code changes. |
| **Dynamic category resolution** | `categories_v2.json` defines WHAT to bet on (suffixes), not WHICH leagues. `series_for()` resolves against live discovery. New league → auto-included. |
| **Unified pool** (no per-category silos) | One population scans the whole Soccer surface; `market_focus` still gives specialization |
| **Trade-tape fills, not candlesticks** | Realism: an order fills only up to the volume the tape shows near that minute |
| **Pre-game only for promotion** | In-play P&L carries a settlement-front-running artifact in backtest; pre-game is trustworthy |
| **LLM live-only** | Too slow/rate-limited for batch replay; one call per game per category, cached |
| **WC base-rate totals prior** | Over-1.5 ~85% in World Cups — a proven edge blended into pre-game totals pricing. Still active; should be recalibrated per-league or made optional for non-WC |
| **YES-only orders** | Each game = 3 YES markets (home/draw/away); YES-only covers every view |
| **Per-league brains** (v4) | 6 independent BrainV4 instances (EPL, LaLiga, SerieA, Bundesliga, Ligue1, Other), each with league-specific calibration, stacker weights, and LLM prompts. Beats one-size-fits-all. |
| **Winner-only** (v4) | Simplifies pricing to the deepest, most liquid market. Removes thin/noisy corner/spread/score books. |
| **League-aware calibration** | `base_goals_per_side` varies by league (EPL 1.42, LaLiga 1.25). Replaces hardcoded `_BASE_GOALS=1.35`. |
| **Club ELO integration** | `EloIndex` merges 58 national + 630 club ELOs. Club teams now resolve correctly for data-model pricing. |
| **JSONL for all logs** | Append-only, no dependencies, human-readable, easy to grep/audit |

## How to Run

```bash
cd ~/Desktop/Github\ Repositories/WorldCupTrading
source venv/bin/activate

# Paper arena — one forward cycle (settle → price → enter → exit → evolve)
python3 -m wc.arena --once

# Paper arena — status report (leaderboard, evolution tracks, live games)
python3 -m wc.arena --status

# Paper arena — replay (backtest on trade tape, no LLM)
python3 -m wc.arena [max_games]

# Real-money promoter — DRY-RUN MIRROR (safe, no orders)
python3 -m wc.promote

# Real-money promoter — LIVE (requires ALL three safety switches)
KALSHI_KEY_ID=... ANTHROPIC_API_KEY=... python3 -m wc.promote --execute

# Real-money promoter — print which agents would be selected
python3 -m wc.promote --select

# Pilot status — live snapshot of armed real-money positions + P&L
python3 -m wc.pilot_status

# Prediction DB — grade unresolved predictions
python3 -m wc.prediction_db --grade

# Prediction DB — accuracy/Brier summary
python3 -m wc.prediction_db --summary

# Telegram bot — long-polling remote control
python3 -m wc.telegram_bot

# Investigate one agent's bets (real or paper)
python3 -m wc.investigate <agent_name>

# Tests (77 tests, 6 files)
python3 -m pytest tests/ -v
```

## Deploy to Droplet

```bash
# From this repo:
bash scripts/deploy.sh

# Target: root@147.182.237.14:/root/ebk-personal
# Excludes: venv, logs, state, git, MyPersonalAgent.txt, legacy, .claude
```

## Known Gaps

1. **Resolution-time sizing not implemented** — `tau_days` plumbing exists but always passed as 0; TVM discount unused.
2. **LLM blend dormant** — Fully coded but `use_llm=False` in most paths; structural model only in practice.
3. **v3 arena/cycle/promote still use BrainV2** — The v4 BrainV4 is built and tested, but arena.py, cycle.py, and promote.py still import the old BrainV2 + per-category architecture. Follow-up work needed to port them to BrainV4.
4. **v3 arena/cycle/promote still use `wc/core/` utilities** — `arena_base.py` and `promote_base.py` provide `_load`, `_save`, `kalshi_maintenance`, `_series_to_cat`, `_event_homeaway`, `_exit_decision`. The new `state_utils.py` has `load_json`/`save_json`/`kalshi_maintenance` ready as replacements.
5. **WC base-rate prior still active in v3** — `WC_OVER_BASE` and `WC_BASE_WEIGHT=0.25` in arena.py blend toward WC empirical goal rates. BrainV4 doesn't use this (winner-only), but arena.py still applies it in the replay path.
6. **ESPN live feed defaults to World Cup** — `live_feed.py` uses the FIFA World Cup league slug. Needs per-league ESPN league mapping for live in-play on club games.

## Verification Plan

1. **Tests**: `python3 -m pytest tests/ -v` — 77 tests across 6 files (all green)
2. **v4 brain instantiation**: `python3 -c "from wc.brain_v4 import BrainV4; [BrainV4(lg) for lg in ['EPL','LaLiga','SerieA','Bundesliga','Ligue1','Other']]"` — confirms all 6 leagues load
3. **v4 brain pricing**: Verify per-league calibration (EPL has higher expected goals than LaLiga for same teams), winner-only (non-winner types return None), three-way sums ~1.0
4. **League routing**: `python3 -c "from wc.scanner import league_from_game_series; ..."` — confirms KXEPLGAME→EPL, KXLALIGAGAME→LaLiga, unknown→Other
5. **Paper arena**: `python3 -m wc.arena --once` — confirms the v3 loop completes without errors (still uses BrainV2)
6. **Dry-run promoter**: `python3 -m wc.promote` — confirms mirror logs without real orders (still uses BrainV2)
7. **Discovery**: `python3 -m wc.scanner --discover` — lists all discovered Soccer series and market counts
8. **Real promoter** (only when explicitly armed): check `logs/promote_v3.jsonl` for `REAL-ORDERS` entries
9. **Kalshi auth**: `python3 -m wc.kalshi.client` — confirms RSA auth + balance read
