# Promotion Patch-Panel — Build Plan

**Status:** PROPOSED — not started. Real money. Every live escalation is gated on explicit user sign-off.
**Author context:** drafted 2026-06-14. Goal from `wc-betting-status` memory: promote best-per-category arena teams to the real wallet after WC group-stage round 1.

---

## 1. Goal

Turn the paper **arena** (16 paper teams + the new `scoreline_S` specialist, ranked best-per-category) into a **patch panel** that can route a *winning* team's decisions to **real Kalshi orders** — per category, hot-swappable, auto or manual, with hard real-money guardrails.

User's metaphor (the spine of this design):
- **Wall socket** = the real-money order path (the wallet).
- **4-way cord** = a live execution bus, one **slot per category** (winner, spread, team_props, game_events, scoreline = 5).
- **Plug** = an arena team `(category, strategy)` + its tuned params + its category's learned brain.
- **Switch** = auto (selector picks best-per-category) or manual (you pin a team).
- **Fuses / breaker** = per-slot $ caps + a global kill switch.

---

## 2. Design principle: ISOLATE real money from the paper arena

The paper arena (`arena.py`) stays **100% paper** — untouched. A **separate** executor (`live_promote.py`) owns all real-order placement. It *read-only* reuses the arena's pricing helpers (`price`, `_build_brain`, `_open_events`, `_live_book`) and the plugged team's params/brain, but maintains its **own** real-order execution and reconciles against **Kalshi's real positions as source of truth**. Rationale: a bug in the paper arena (reset, replay, seeding) must never be able to place a real order.

```
arena.py            → paper only (brain trust + standings)   [UNCHANGED]
arena_state/        → per-team params + per-category brains   [read by executor]
switchboard.json    → the patch panel (slots, modes, pins, caps)   [NEW]
group/promotion.py  → selector: best-per-category, risk/sample-gated   [NEW]
live_promote.py     → the execution bus: real orders for plugged teams   [NEW]
logs/promote.jsonl  → every intended/placed real order + reconcile   [NEW]
```

---

## 3. What exists vs. what we build

**Exists (reuse, no rewrite):**
- Per-category learned brains (`arena_state/brain_<cat>.json`: weights, calibration, llm_addendum).
- Per-team tuned params (`arena_state/team_<cat>_<suffix>.json`: kelly_fraction, min_edge, exit thresholds).
- Live pricing for every category: `brain.evaluate` / `evaluate_market` / `evaluate_scoreline`, wrapped by `arena.price()`.
- Per-team entry/exit decisioning on the real book: `strategy.entry_dollars` / `exit_decision` (already consume live bid/ask).
- Real-order primitives: `kalshi_client.buy/sell/get_best_bid_cents/get_best_ask_cents`, `KalshiClient` RSA auth; `agents/trader.py` + `agents/keeper.py` as reference real-order + exit implementations.

**Build (new):**
1. `switchboard.json` — the panel config (schema in §5).
2. `group/promotion.py` — the selector (§6).
3. `live_promote.py` — the execution bus (§7) + reconciliation (§8) + risk layer (§9).
4. Wiring: a cron/launchd entry that runs `live_promote.py --once` (mirrors arena's cadence), gated behind `--execute`.

---

## 4. The unit of promotion

A **slot = a category**. The **plug = the best strategy within that category** (e.g. `team_props_D`), carrying:
- its params (`arena_state/team_team_props_D.json` → `params`),
- its category brain (`arena_state/brain_team_props.json`).

The executor, for a plugged slot, trades that category's Kalshi series (`categories.json[cat].series`) using the slot's brain+params — exactly what the arena does on paper, but with real orders.

---

## 5. The switchboard schema (`switchboard.json`)

```jsonc
{
  "master": { "armed": false, "daily_cap_dollars": 20.0, "kill": false },
  "slots": {
    "winner":      { "mode": "manual", "pin": "winner_A",      "fuse_per_cycle": 4.0, "max_open": 3, "max_bet": 2.0 },
    "spread":      { "mode": "off",    "pin": null,            "fuse_per_cycle": 0.0, "max_open": 0, "max_bet": 0.0 },
    "team_props":  { "mode": "off",    "pin": null,            "fuse_per_cycle": 0.0, "max_open": 0, "max_bet": 0.0 },
    "game_events": { "mode": "off",    "pin": null,            "fuse_per_cycle": 0.0, "max_open": 0, "max_bet": 0.0 },
    "scoreline":   { "mode": "off",    "pin": null,            "fuse_per_cycle": 0.0, "max_open": 0, "max_bet": 0.0 }
  }
}
```

- `mode`: `off` (no real orders) | `manual` (trade `pin`) | `auto` (selector picks).
- `master.armed`: global enable. If false → dry-run mirror only (logs intended orders, places none).
- `master.kill`: hard breaker — if true, cancel-all + place nothing.
- `fuse_per_cycle` / `max_open` / `max_bet`: per-slot dollar fuses.
- `daily_cap_dollars`: master fuse across all slots per day.

Defaults ship **safe**: `armed:false`, all slots `off` except a manually-pinned `winner` with tiny caps.

---

## 6. The selector (`group/promotion.py`)

`select_per_category(arena_state, gate) -> {cat: team_name | None}`

Don't plug in luck (memory: `team_props_D +134` is a *longshot mirage*). Gate before ranking:
- **Eligibility:** `n_closed_bets >= MIN_PROMOTE_N` (e.g. 20) AND realized P&L > 0 AND calibration sane (pooled Brier <= 0.25).
- **Rank** eligible teams by a risk-adjusted score (P&L per $ staked, penalized by variance / few-bet uncertainty) — NOT raw P&L.
- Returns the winner per category, or `None` (slot stays dark) if nothing clears the gate.

Used only by `auto` slots. `manual` slots ignore it and use `pin`.

---

## 7. The execution bus (`live_promote.py`)

Per `--once` cycle (cron, same 2-min cadence as the arena):
1. Load `switchboard.json`. If `master.kill` → cancel all open orders, exit. If not `master.armed` → **dry-run mirror** (compute + log intended orders, place none).
2. Resolve each active slot's plugged team (`manual.pin` or `selector`).
3. For each plugged slot, for each open/live event in that category's series:
   - price each leg via `arena.price(cat_cfg, brain, mkt, matchup, mtotal, gs)` (live book),
   - build `ctx` and ask the team's strategy for `entry_dollars` / `exit_decision`,
   - translate to a **real** order intent (buy YES at ask / sell at bid), sized by the slot's `max_bet` and remaining fuses.
4. Hand intents to the **reconciler** (§8) which diffs against real positions and places only the deltas.
5. Log every intent + placement + skip-reason to `logs/promote.jsonl`.

The pricing/sizing is *identical* to the arena's `live_step`/`pre_step` — we import and reuse those code paths read-only, swapping the paper account write for a reconciled real order.

---

## 8. Reconciliation (idempotency & crash safety)

**Kalshi positions are the source of truth**, never a local file. Each cycle:
- Fetch real open positions for the slot's series tickers.
- **Entry:** only if not already holding that ticker (mirrors arena's `if tk not in positions`) AND fuses allow.
- **Exit:** when the strategy says OVERPRICED/TAKE_PROFIT/STOP, sell the real held quantity (re-armed once-per-episode, same `exit_rules` semantics as the Keeper).
- A crash/restart mid-cycle can't double-bet: next cycle re-reads real positions and continues.

Local state (`promote_state.json`) holds only: slot→team bindings, fuse $ spent this cycle/day, and exit re-arm flags keyed by ticker.

**Hot-swap rule (auto):** when the selector changes a slot's team, the **outgoing** team's params keep managing its *open* positions to exit; the **incoming** team only takes *new* entries. No orphaned live positions on a swap.

---

## 9. Risk layer (fuses + breaker)

- **Per-slot fuse:** `max_bet` (per order), `max_open` (positions), `fuse_per_cycle` ($ deployed per cycle). Exceed → skip + log.
- **Master breaker:** `daily_cap_dollars` across all slots; `master.kill` cancel-all; `master.armed=false` = dry-run mirror.
- **Invariant preserved:** YES-only orders (never NO), inherited from the whole system.
- **Reuse existing caps** where sensible (`max_bet_dollars`, `max_open_positions`, `max_cycle_dollars` already in `config.json`).
- **Every real order requires `--execute` AND `master.armed=true`** — two independent switches.

---

## 10. Phased rollout — each step gated on your sign-off

| Phase | Deliverable | Real money? | Gate |
|---|---|---|---|
| **0** | `switchboard.json` + `promotion.py` selector + `live_promote.py` in **dry-run mirror** (logs intended orders, places none). Verify intents match arena paper decisions. | No | Review mirror logs |
| **1** | **Winner socket LIVE**, manual pin (`winner_A`), tiny caps ($1–2/bet, $4/cycle, $20/day breaker). Most-validated category. | Yes (tiny) | **Explicit sign-off** |
| **2** | Build the **cord**: generalize executor to spread / team_props / scoreline series (real-order pricing already exists via `evaluate_market`/`evaluate_scoreline`). Each new socket: dry-run mirror first → then live tiny. | Yes (tiny) | Sign-off per socket |
| **3** | **Auto-selector** (risk/sample-gated) + graceful hot-swap; manual override always wins; master breaker. | Yes | Sign-off to flip `auto` |

We do not advance a phase without you approving the prior phase's logs/results.

---

## 11. Test plan

- **Unit:** selector gating (rejects low-N / negative / mis-calibrated); fuse math; reconciler diff logic (no double-entry given an existing position); hot-swap (outgoing exits, incoming entries only).
- **Dry-run mirror (Phase 0):** run alongside the live arena for ≥1 match day; assert the executor's intended orders equal the plugged team's arena paper fills, leg-for-leg.
- **Live smoke (Phase 1):** 1-contract / min-size real order on the winner market → confirm fill + reconcile + exit, mirroring the 2026-06-12 order-path verification.
- **Breaker drills:** `master.kill=true` cancels all; `armed=false` places nothing; fuse trips skip cleanly.

---

## 12. Open decisions (recommended defaults baked in; confirm at build time)

1. **Sockets = categories (5)** incl. scoreline. ✅ recommended.
2. **Start scope:** winner socket only, Phase 0→1. ✅ recommended.
3. **Default mode:** manual-pin first; auto only after the selector is trusted. ✅ recommended.
4. **Selector metric:** risk/sample-gated, not raw P&L. ✅ recommended.
5. **Hot-swap:** graceful (outgoing exits, incoming new-entries-only). ✅ recommended.
6. **Cadence/host:** run `live_promote.py --once` via the **droplet** cron (where the live arena already runs), NOT the laptop. (Scoreline backfill blocker is independent — see `wc-betting-status` memory.)

---

## 13. Non-goals / explicitly out of scope (for now)

- The scoreline specialist going live (still pending its rate-limit-resilient backfill — separate thread).
- NO-side / second-wallet strategies (needs a 2nd Kalshi wallet).
- Player-prop markets (no Kalshi market + no model; user bets manually).
- Any change to the paper arena's behavior or standings.
