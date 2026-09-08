# Phase 2 — Backtest engine

*Replays a strategy spec over historical candlesticks and reports what would
have happened. Market-agnostic by construction.*

---

## 1. The loop

```
for each bar in chronological order across ALL markets:
    mark open positions to current bid
    check exits   -> close positions whose exit conditions fire
    check entries -> for markets passing the universe filter
    apply fees, slippage, volume cap
    record fill / rejection with a reason
at settlement:
    realise PnL against the market's actual result
```

**Chronological across all markets, not market-by-market.** Iterating one market
to completion before starting the next silently breaks portfolio-level caps —
daily spend, total exposure, position count — because the engine would never see
two markets open at once. This is a subtle bug that produces plausible-looking
wrong numbers, so it is a structural decision, not an optimisation.

---

## 2. No lookahead

The single most common way a backtest lies. Enforced structurally, not by
discipline:

- The engine hands the interpreter a **bar view** containing only data at or
  before the current timestamp. It is not given the dataframe.
- `result` is unreadable until the bar passes `close_time`.
- Any derived feature (moving average, volatility) is computed on the trailing
  window only.
- A test asserts that a deliberately cheating strategy — one that reads the
  outcome — cannot see it.

---

## 3. Cost model

Modelled explicitly. Fees at prediction-market prices are large relative to
edge, and approximating them is how a losing strategy looks profitable.

| Cost | Treatment |
|---|---|
| **Kalshi trading fee** | Exact published formula, not a flat rate. It is non-linear in price and peaks near 50¢ |
| **Settlement fee** | Applied where it applies |
| **Spread** | Buy at `yes_ask`, sell at `yes_bid`. Never at `close` |
| **Slippage** | Configurable cents on top of the spread |
| **Volume cap** | Fill at most `max_volume_share` of the bar's volume |
| **Latency** | v1: assume the next bar, not this one |

Every cost is a config value with a documented default, and the metrics output
reports gross PnL, total fees, and net PnL separately — so it is always visible
how much of an edge the fees ate.

---

## 4. Metrics

Trading metrics answer "did it make money". Forecasting metrics answer "was it
right". A prediction-market system needs both, because a strategy can be
profitable through sizing luck while being badly calibrated — and that does not
survive contact with a new regime.

**Trading**

| Metric | Why |
|---|---|
| Net PnL | The headline. Always alongside gross + fees |
| Max drawdown | What it felt like to hold |
| Sharpe / Sortino | Return per unit of risk |
| Win rate, avg win/loss | Shape of the distribution |
| Turnover | Fee drag proxy |
| Fill rate, rejection reasons | How much of the strategy was actually executable |
| Exposure over time | Whether caps were the binding constraint |

**Forecasting**

| Metric | Why |
|---|---|
| **Brier score** | Mean squared error of the probability estimate |
| **Calibration curve** | Predicted vs actual, in deciles. The honest picture |
| **Log loss** | Punishes confident wrongness |
| Coverage | Fraction of the market surface the strategy touched |

**Segmented by** time-to-resolution bucket, price bucket, and series. The
research shows edge varies sharply along exactly these axes — under a week,
sports markets are near-efficient; beyond a month, the bias is strong. A single
aggregate number hides the effect being traded.

---

## 5. Walk-forward by default

A single in-sample backtest is not evidence. The engine's default mode is
walk-forward: fit or tune on a window, test on the next unseen window, roll,
and report the concatenated out-of-sample result as the headline number.

The most recent period is held out entirely and touched once, at the end.

`metrics.json` records in-sample and out-of-sample separately, and the dashboard
shows OOS by default. Anyone reading only the big number should be reading the
honest one.

---

## 6. Validation — how we know the engine itself is right

The engine is the thing every future decision rests on, so it gets tested
harder than anything else.

- **Known-answer case.** A hand-computed 3-bar scenario with one entry and one
  exit, where PnL is arithmetic on paper. Any refactor must reproduce it.
- **Zero-edge case.** A random strategy on real data must lose approximately
  the fee take. If random trading looks profitable, the cost model is wrong.
- **Perfect-foresight case.** A strategy given the outcome must show a huge
  profit. If it does not, the settlement logic is wrong.
- **No-lookahead test.** As in §2.
- **Cap enforcement.** Daily and per-market caps hold across concurrent
  positions — the case the chronological loop exists to make possible.

---

## 7. Definition of done

- [ ] All five validation cases pass
- [ ] Runs a full series end-to-end and emits `metrics.json`
- [ ] Walk-forward split is the default, in-sample requires an explicit flag
- [ ] Gross PnL, fees, and net PnL reported separately
- [ ] Metrics segmented by time-to-resolution, price bucket, and series
- [ ] Rejection reasons counted, so unexecutable strategies are visible
