# v3 Plan — World Cup Kalshi Autonomous Scanner

*Written 2026-06-29 from a full read-only audit of all ~52 Python files (4 parallel agents). This is the target design + the codebase reality: what to reuse, what's redundant, what's still to build.*

---

## 1. The goal (the loop)

One live loop, running on the knockouts, **nothing hardcoded**:

```
DISCOVER  every open KXWC* market (regex ^KXWC, no menu)
   ↓
BRAIN     p_fair = blend(trained model, LLM) when the model can price it;
          LLM-only fallback when it can't
   ↓
STRATEGIST decides bet/skip + size — accounting for the market's RESOLUTION TIME
          (a slow-resolving bet ties up an open slot = dead capital = no profit)
   ↓
BETTING AGENT executes the order on Kalshi
   ↓
PREDICTION DB records p_fair, sizing, decision, open/resolved state, result
   ↓
RESOLVE   compare actual outcome vs predicted
   ↓
LEARN (RL) both the brain AND the strategist improve every cycle
```

The point is a system that is **alive and learning**, accumulating our own
right/wrong dataset so that in ~1 month we can price partly from history, not
only the LLM.

---

## 2. What already exists — reuse per component

The good news: most of the loop is already built; it's **fragmented**, not missing.

| # | Component | Reuse (file:function) | State |
|---|-----------|----------------------|-------|
| 1 | Discover | `scanner_v2.discover_kxwc_series` (regex `^KXWC`, cached) + `kalshi_client_v2.list_markets_by_series`; `scanner_v2.price_games` does discover+price end-to-end | **built** (just added) |
| 2 | Brain p_fair | `brain_v2.pfair`/`_stack` (logit blend of {data, llm, market}, renormalizing fallback) + `markets_v2.parse_market_v2`/`fair_yes_v2` (parses & prices every leg type) | built, **LLM forced off** |
| 3 | Strategist | `strategy_v3.Strategist.entry_size` (price band, focus, edge, Kelly, favorite-tilt, unit cap) + `exit_decision` → `group/exit_rules.decide_exit` | built, **no time-awareness** |
| 4 | Betting agent | `kalshi_client._place_order_v2` + `buy`/`sell_limit` (migrated IOC `/portfolio/events/orders`, verified) driven by `live_promote_v3.run` | **built** |
| 5 | Prediction DB | `prediction_db.log_prediction`/`grade`/`summary` (JSONL ledger, Brier/accuracy by source & type) | built, **wired to nothing** |
| 6 | Resolve/compare | `prediction_db.grade` + `arena_v3._settle_live`; reads via `list_markets_by_tickers` | built (2 copies) |
| 7 | Learn (RL) | `brain_v2.update_stacker` (inverse-Brier weight retune) + `arena_v3.evolve_v3`/`_evo_fitness` (GA on strategists) | built, running in `cycle_once` |

**Load-bearing substrate (keep, do not touch):** `group/markets.py` (Poisson math kernel under both brains), `group/kelly.py` (`size_detail`, the one true sizer), `group/paper.py` (`PaperAccount` ledger), `group/live_feed.py` (`scoreboard_events`+`state_from_events`, 429-friendly live state), `group/dominance.py` (in-play multipliers), `group/exit_rules.py` (shared exit). `arena_v2.py` is the de-facto shared util library imported by v3 (`arena_v3.py:32`); `kalshi_client.py` holds the migrated order API under `kalshi_client_v2`.

---

## 3. The trained-model reality (matters for brain v3, task #25)

- The brain the user remembers — `model.pkl`, an **ELO→3-way winner logistic regression** trained by `models/train.py` — **is loaded by `group/brain.py` but its prediction is NOT used by v3.** Winner and all goal markets are priced by the **structural Poisson/ELO layer** (`group/markets.py`) blended in the logit stacker.
- The only *trained* model actually consumed live is **`corners_model.json`** (NegBin), and only its `dispersion.ratio` + `base_corners`, via `markets_v2.py`.
- So today the "blend" is `{structural data model, LLM(off), market mid}`. The plan's "blend trained model WITH the LLM" means: **(a) turn the LLM half back on, (b) decide per market type whether a real model can price it (structural Poisson where applicable; ML model.pkl/corners where trained) and blend, else LLM-only.** Brain v3 = making that routing + blend good, and feeding the prediction DB so future trained models replace LLM guesses.

---

## 4. Redundancy & cleanup (what the scan found)

**Duplicated logic to consolidate:**
- **Entry (size→fill→record) ×3:** `arena_v3._enter` (tape replay) · `arena_v3._enter_live` (forward paper) · `live_promote_v3.run` 339-387 (real orders). Same NC guard + BET_PERIODS + one-leg-per-event + tilt sizing, three copies.
- **Exit ×3:** `arena_v3._exit_step` · `arena_v3._exit_live` · `live_promote_v3.manage_exits` (via `live_promote_v2._exit_decision`). Two different exit-rule entrypoints (`group/exit_rules.decide_exit` vs `live_promote_v2._exit_decision`).
- **WC base-rate prior ×2:** `arena_v3._base_rate_legs` ≈ `live_promote_v3._apply_base_rate`.
- **Settle/grade ×2:** `arena_v3._settle_live` ≈ `prediction_db.grade`.
- **Replay/candlestick data ×3–4:** `arena_v2._candle_quote`, `backtest.get_price_path`, `arena.py._pregame_quote*`, `train_replay._pregame_quote` → should be one shared `replay_data.py`.
- **Event chronology ×2:** `scanner_v2._event_when` vs `arena_v2._event_date`.

**Dead code (no importer; safe to delete):** `train_replay.py`, `rebuild_aggressive.py`, `leaderboard.py`; `strategy_v3.perturb`, `tradetape.fill`, `kalshi_client.sell_market`, `kalshi_client.KOREA_TONIGHT`, `kalshi_client_v2.KalshiWS` (whole WebSocket class, unused), `group/notify.py`, `group/env_portable.py`.

**The v1 "island" — retire as one unit** (none touch v3, but they import each other): `arena.py`, `live_promote.py`, `backtest.py`, `tournament_live.py`, `train_replay.py`, `rebuild_aggressive.py`, `group/scanner.py`, `group/strategy.py`, `group/promotion.py`, `group/run.py`, the `agents/` 4-agent system (`trader/keeper/trainer/tournament/tournament_trainer`). Salvage their reusable bits first (see below).

**Salvage before deleting:** `backtest.py` ESPN/candlestick helpers (`find_espn`, `get_goals`, `score_at`, `_canon`/`_teams_match` alias matching) — clean and re-implemented 3–4×; `arena.py._pregame_quote_repr` (skip 1¢/99¢ placeholder books); `leaderboard._max_drawdown` + Sharpe (better promotion gate than current risk-adj fitness).

**Stale test:** `test_arena_v2.py::test_evolve` calls a non-existent `a._evolve`/`child` API (now `_evolve_cat` / `g{gen}<…>` lineage) — fix or drop.

---

## 5. Gaps vs the plan (the real work)

1. **Resolution-time / opportunity-cost sizing is NOT implemented (component 3).** `entry_size(tau_days=...)` is always called with `0`; `tvm_rate` sits unused. There is a flat `max_open` slot cap but no penalty for slow-resolving markets hogging a slot. **This is the clearest plan-vs-code gap** and central to the knockout markets (advance/finalist resolve far out).
2. **Prediction DB is wired to nothing (component 5).** `prediction_db.log_prediction` is called by nobody; predictions live only inside per-team JSON blobs in `arena_v3_state/teams_*.json`. The "record p_fair, sizing, decision, state, result" exists in two disconnected half-forms.
3. **LLM blend dormant (component 2).** Fully coded in `brain_v2`/`group/brain.py` but `use_llm=False` in every live path → live p_fair is model+market only. The "blend with the LLM" isn't actually happening.
4. **Three loops, not one.** `arena_v3.cycle_once` (paper, all cats, evolves) and `live_promote_v3.run` (real money, 2 cats) on separate crons, with the 3× duplicated entry/exit paths above.
5. **Discovery not yet feeding the loop.** `discover_kxwc_series` exists but `cycle_once`/`run` still price via the hardcoded `categories_v2.json` series lists — discovery is built but not plugged into pricing/betting yet.

---

## 6. Build order

1. **Brain v3 (task #25)** — per-market-type routing: structural/trained model where it can price, blend with LLM (turn LLM back on, throttled/cached), LLM-only fallback; emit `(p_fair, sources)` for *any* KXWC leg. Log every prediction to the DB.
2. **Wire discovery → pricing** — `price_games` consumes `discover_kxwc_series` (the full wild surface), not the categories_v2 lists. New knockout/outright series price automatically.
3. **Prediction DB in the loop (component 5/6)** — `log_prediction` at decision time; `grade` on resolution; one ledger, retire the duplicate settle path.
4. **Strategist time-awareness (component 3)** — pass real `tau_days` (resolution time) into `entry_size`; penalize slow-resolving bets for the slot they occupy; make it an **evolution dimension** (fitness already risk-adjusts P&L — add capital-tied-up/time term).
5. **Converge to one loop + de-dup** — single entry/exit path shared by paper and real; collapse the base-rate and settle duplicates.
6. **Cleanup** — salvage v1 helpers into a shared module, then delete the v1 island + dead code.

**Safety:** novel/LLM-priced markets accumulate in the **paper** arena + DB first; real money stays on the proven forward-only `select()` until a market shows out-of-sample edge in the DB. Real-money arming unchanged (switchboard_v3 `armed` + `--execute` + `!kill`).
