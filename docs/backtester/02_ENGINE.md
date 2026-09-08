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
| **Latency** | Fill on the NEXT bar (`Costs.fill_delay_bars`, default 1). Entries and exits both. Intents older than `max_fill_age_seconds` expire unfilled |

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

- [x] All five validation cases pass
- [x] Runs a full series end-to-end and emits `metrics.json`
- [x] Walk-forward split is the default, in-sample requires an explicit flag
- [x] Gross PnL, fees, and net PnL reported separately
- [x] Metrics segmented by time-to-resolution, price bucket, and series
- [x] Rejection reasons counted, so unexecutable strategies are visible
- [x] Most recent period held out entirely (`--holdout-frac`, default 25%) and
      scored once, reported separately from the walk-forward result
- [x] In-sample and out-of-sample recorded in the SAME `metrics.json`
- [x] **Latency modelled.** Fills happen on the bar AFTER the deciding bar,
      for exits as well as entries, with committed-but-unfilled capital counted
      against the caps. Every run also reports the same-bar result as a
      diagnostic and warns when a strategy is mostly an execution artefact

### Latency, and why it is not a P&L haircut

Filling on the bar that triggered a strategy is not merely imprecise — it is
**biased, always in the flattering direction**. The price that caused the signal
is the price the strategy gets, and trigger prices (the dip below 10c, the
blown-out spread) are the least likely to still be there when an order arrives.
The error never averages out: every trade takes the same small gift, and across
thousands of trades that compounds into an edge that does not exist.

The fix is **not** a correction factor applied to P&L. A haircut would be a
constant where the real effect is not (it is large for threshold-triggered
strategies and near zero for slow ones), it would hide the assumption inside a
number nobody can audit, and it would make strategies incomparable. Instead the
engine changes *where the fill price comes from*, and P&L falls out of that
honestly.

Both directions are kept: if the market moved in our favour between decision and
fill, the strategy keeps it. A delay that only ever hurt would be a different
kind of fudge factor.

**The useful output is the comparison.** Every run also replays the strategy
with same-bar fills and reports `latency_sensitivity`:

| Ratio | Reading |
|---|---|
| ~1.0 | The edge survives execution |
| >2.0 | Most of the "edge" is the strategy capturing its own trigger price — warned about explicitly |
| Profitable same-bar, unprofitable next-bar | An execution artefact, not a strategy |

At 5-minute snapshots, next-bar means **5 minutes** of latency, which is far
slower than the live path really is — just as same-bar is faster than possible.
The truth is between them, and finer resolution is not purchasable: Kalshi sells
no L2 history. The bracket is the honest shape for the uncertainty, and the
pessimistic end is the safe one to act on.

### Model-fitted lookahead — the limit of the guarantee

Section 2's no-lookahead guarantee is structural and holds: a strategy is handed
a `BarView` and cannot read a future bar or an unsettled result.

It does **not** cover a MODEL fitted on the future. `wc/backtest/pricing.py`
supplies `model_prob` from our own brains, whose Elo ratings are a present-day
snapshot and whose stacker weights were trained on outcomes spanning the
backtest window. Fixing this properly needs point-in-time Elo snapshots, which
were never recorded.

So every report produced with `--brains` carries a `lookahead_risk` field and a
printed warning. Calibration degrades under leakage in a way raw P&L does not,
which makes Brier the more honest number on those runs.
