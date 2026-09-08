# Backtester + Strategy DSL — Overview

*Written 2026-09-07 on branch `DSL`, after a web survey of the Kalshi API, the
open-source Kalshi tooling landscape, and the prediction-market research
literature. Research findings and sources: [06_RESEARCH.md](06_RESEARCH.md).*

> **Status (2026-09-08):** all five phases built. Historical backfill is the
> one deliberate omission — archived history will still be there next month,
> whereas un-collected forward history is gone forever.
>
> **The collector is not on cron yet, so no history is accruing.** Until it is,
> every backtest runs on a single snapshot and every P&L is noise.
>
> The droplet is deployed but **idle** and **disarmed**.

---

## 1. The goal

Turn a strategy idea — typed in English, or spoken in a YouTube video — into a
number that says whether it would have made money.

```
IDEA        plain English, or a video URL
   ↓
AUTHOR      LLM translates it into a strategy SPEC (JSON, schema-validated)
   ↓
BACKTEST    one interpreter replays the spec over historical candlesticks
   ↓
METRICS     PnL, Brier, calibration, drawdown, Sharpe, turnover, fill rate
   ↓
DASHBOARD   equity curves, calibration plots, per-strategy leaderboard
```

Two design commitments shape everything below.

**The backtester is market-agnostic.** It consumes candlesticks keyed by
ticker, close time, and price. It does not know or care whether a market is
soccer, weather, or Fed rates. This is deliberate: the strongest documented
edge in prediction markets (favorite–longshot bias) is *not* in sports, and a
soccer-only backtester could never find it.

**Strategies are data, not code.** An LLM never emits Python. It fills in a
constrained schema, and one hand-written interpreter executes it. See
[03_STRATEGY_DSL.md](03_STRATEGY_DSL.md) for why this is non-negotiable.

---

## 2. Where it lives

Inside this repo — **not** a new one. The backtester needs the same Kalshi
client, the same brains, and the same Kelly sizing as the live path. A separate
repo would fork all three and reintroduce the two-sources-of-truth problem that
the 2026-09-07 droplet wipe just cleaned up.

```
wc/
├── backtest/
│   ├── data.py          SQLite store + idempotent writes          DONE
│   ├── collect.py       forward collector (cron on the droplet)   DONE
│   ├── quality.py       gap / settlement / sanity report          DONE
│   ├── engine.py        the replay loop                           DONE
│   ├── metrics.py       PnL, Brier, calibration, drawdown, Sharpe DONE
│   ├── spec.py          DSL schema + validation                   DONE
│   ├── interpret.py     the ONLY code that executes a strategy    DONE
│   ├── pricing.py       our brains -> the model_prob signal       DONE
│   ├── run.py           walk-forward runner + CLI                 DONE
│   ├── author.py        English/video -> spec via LLM             DONE
│   └── dashboard.py     self-contained HTML from metrics.json     DONE
├── kalshi/              EXISTING — reused as-is
└── lib/kelly.py         EXISTING — reused for sizing
strategies/              NEW — one JSON file per strategy, in git
docs/backtester/         these plans
```

---

## 3. Build order

Each phase is independently useful and independently testable. Do not start a
phase before the one above it passes its own tests.

| # | Phase | Doc | Output |
|---|-------|-----|--------|
| 1 | **Data layer** | [01_DATA.md](01_DATA.md) | SQLite of candlesticks + trades; forward collector running |
| 2 | **Engine** | [02_ENGINE.md](02_ENGINE.md) | Replay loop + metrics, validated against a known-answer case |
| 3 | **Strategy DSL** | [03_STRATEGY_DSL.md](03_STRATEGY_DSL.md) | Schema, interpreter, one hand-written baseline strategy |
| 4 | **Authoring** | [04_AUTHORING.md](04_AUTHORING.md) | English -> spec, then video -> spec |
| 5 | **Dashboard** | [05_DASHBOARD.md](05_DASHBOARD.md) | Equity curves, calibration, leaderboard |
| — | **Promotion criteria** | [07_PROMOTION.md](07_PROMOTION.md) | The gates from backtest to paper to real money |

Phases 1–3 are the foundation. Phase 4 is cheap *only because* phase 3 exists.
Phase 5 is presentation and can slip without blocking anything.

---

## 4. What we reuse vs. build

The survey found four open-source Kalshi frameworks. We take ideas from them
and vendor none. Reasoning in [06_RESEARCH.md](06_RESEARCH.md#3-existing-frameworks).

| Component | Decision |
|---|---|
| Kalshi API client | **Reuse ours** (`wc/kalshi/`) — RSA-PSS auth works, verified against live API 2026-09-07 |
| Execution model (fees, slippage, latency) | **Port the ideas** from `Quentin-Piot/prediction-market-backtester` (MIT) |
| Pipeline shape | **Follow** `kapelame/kalshi-crypto-bot` (MIT): collect -> QA -> train -> backtest -> paper -> live |
| Strategy safety (AST checks) | **Sidestep entirely** — our DSL means there is no generated code to analyse |
| `homerun` | **Read only. Do not vendor.** AGPL-3.0 is viral over a network; serving its dashboard obliges publishing our strategies |
| Kelly sizing, exit rules, brains | **Reuse ours** — already built and tested |

---

## 5. Known risks

| Risk | Mitigation |
|---|---|
| **No L2 order-book history from Kalshi's API.** Candlesticks only; realistic fill modelling needs paid vendor data | Start the forward collector in phase 1 so history accrues while we build. Backtest at candlestick resolution first and be explicit that fills are optimistic |
| **Backtest overfitting.** Any strategy can be tuned to win on one window | Walk-forward by default; hold out the most recent period; report out-of-sample separately. A single in-sample PnL number is never sufficient to promote |
| **The schema is a ceiling.** A strategy it can't express can't be traded | Accepted deliberately. Extend the schema as a conscious design step, never fall back to generated code |
| **Survivorship bias in archived markets** | Record every market seen at scan time, not only those that resolved |
| **Fees are material at prediction-market scale** | Kalshi's fee formula is modelled explicitly in the engine, not approximated |

---

## 6. Open questions

Resolve before or during the phase noted.

1. **Which markets to collect first?** (phase 1) Collecting everything is
   expensive in API budget. The research points at low-liquidity, long-horizon
   markets for the longshot edge — the opposite of the current soccer focus.
2. **How far back does Kalshi's `/historical/` archive actually go?** (phase 1)
   Determines whether backtests start at months or years of history.
3. **Do we model maker fills at all in v1,** or taker-only? (phase 2)
   Taker-only is simpler and pessimistic, which is the safe direction.
4. ~~**Promotion criteria**~~ — **resolved**, written down before the first
   strategy was tested: [07_PROMOTION.md](07_PROMOTION.md).
