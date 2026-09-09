"""Backtest metrics.

Two families, and both are needed. Trading metrics answer "did it make money".
Forecasting metrics answer "was it right". A strategy can be profitable through
sizing luck while being badly calibrated, and that does not survive a new
regime — so calibration is reported alongside P&L, never instead of it.

See docs/backtester/02_ENGINE.md section 4.
"""
from __future__ import annotations

import datetime as dt
import math

DAY = 86400.0


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# ── trading ───────────────────────────────────────────────────────────────────

def equity_curve(trades, starting_bankroll):
    """[(ts, equity)] after each closed trade, in chronological order."""
    curve, eq = [], float(starting_bankroll)
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        eq += t["net_pnl"]
        curve.append((t["exit_ts"], eq))
    return curve


def max_drawdown(curve):
    """Largest peak-to-trough fall. Returns (absolute, fraction_of_peak)."""
    if not curve:
        return 0.0, 0.0
    peak, worst, worst_frac = curve[0][1], 0.0, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = peak - eq
        if dd > worst:
            worst, worst_frac = dd, (dd / peak if peak else 0.0)
    return worst, worst_frac


def sharpe(trades, periods_per_year=252):
    """Sharpe on per-trade returns. Zero when there is no dispersion — with
    fewer than two trades the number is meaningless, not infinite."""
    rets = [t["return_pct"] for t in trades if t.get("return_pct") is not None]
    sd = _stdev(rets)
    if len(rets) < 2 or sd == 0:
        return 0.0
    return (_mean(rets) / sd) * math.sqrt(periods_per_year)


def sortino(trades, periods_per_year=252):
    rets = [t["return_pct"] for t in trades if t.get("return_pct") is not None]
    downside = [r for r in rets if r < 0]
    dd = _stdev(downside)
    if len(rets) < 2 or dd == 0:
        return 0.0
    return (_mean(rets) / dd) * math.sqrt(periods_per_year)


# ── forecasting ───────────────────────────────────────────────────────────────

def brier(predictions):
    """Mean squared error of probability estimates.
    predictions: [(p, outcome)] with outcome in {0, 1}. Lower is better;
    0.25 is what you get by always saying 50%."""
    if not predictions:
        return None
    return _mean([(p - o) ** 2 for p, o in predictions])


def log_loss(predictions, eps=1e-15):
    """Punishes confident wrongness far harder than Brier does."""
    if not predictions:
        return None
    total = 0.0
    for p, o in predictions:
        p = min(1 - eps, max(eps, p))
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(predictions)


def calibration(predictions, bins=10):
    """Predicted vs actual, bucketed. The honest picture of whether the
    probabilities mean anything.

    Returns [{bin_low, bin_high, n, mean_predicted, actual_rate}], skipping
    empty buckets so the plot never implies data we do not have.
    """
    if not predictions:
        return []
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        # last bucket is closed on the right so p == 1.0 is counted
        got = [(p, o) for p, o in predictions
               if (lo <= p < hi) or (i == bins - 1 and p == 1.0)]
        if not got:
            continue
        out.append({
            "bin_low": lo, "bin_high": hi, "n": len(got),
            "mean_predicted": _mean([p for p, _ in got]),
            "actual_rate": _mean([o for _, o in got]),
        })
    return out


# ── segmentation ──────────────────────────────────────────────────────────────
# The research shows edge varies sharply along these axes: under a week sports
# markets are near-efficient (slope 0.90-1.10); beyond a month the
# favorite-longshot bias is strong (slope 1.74). A single aggregate number
# hides the very effect being traded.

DTR_BUCKETS = [(0, 1), (1, 7), (7, 30), (30, 90), (90, float("inf"))]
PRICE_BUCKETS = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.50),
                 (0.50, 0.75), (0.75, 0.90), (0.90, 1.0)]


def _bucket(value, buckets):
    for lo, hi in buckets:
        if lo <= value < hi:
            return f"{lo}-{hi}"
    return f">={buckets[-1][0]}"


def segment(trades, key):
    """Group trades by 'days_to_resolution', 'price', 'series', or 'month'."""
    groups = {}
    for t in trades:
        if key == "month":
            # WHEN it makes money. An edge that lived in one month and died is
            # the single most common thing a headline P&L hides.
            ts = t.get("exit_ts")
            name = (dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m")
                    if ts else "unknown")
        elif key == "days_to_resolution":
            v = t.get("entry_days_to_resolution")
            name = _bucket(v, DTR_BUCKETS) if v is not None else "unknown"
        elif key == "price":
            v = t.get("entry_price")
            name = _bucket(v, PRICE_BUCKETS) if v is not None else "unknown"
        else:
            name = t.get(key) or "unknown"
        groups.setdefault(name, []).append(t)

    return {name: {
        "n": len(ts),
        "net_pnl": round(sum(t["net_pnl"] for t in ts), 4),
        "win_rate": round(_mean([1.0 if t["net_pnl"] > 0 else 0.0 for t in ts]), 4),
        "brier": brier([(t["model_prob"], t["outcome"]) for t in ts
                        if t.get("model_prob") is not None and t.get("outcome") is not None]),
    } for name, ts in sorted(groups.items())}


# ── report ────────────────────────────────────────────────────────────────────

def summarize(trades, starting_bankroll, rejections=None, label="backtest"):
    """The full metrics report.

    Gross P&L, fees, and net P&L are reported SEPARATELY and always. Fees at
    prediction-market prices are large relative to edge, and a strategy that
    looks profitable gross and loses net is the most common failure mode.
    """
    curve = equity_curve(trades, starting_bankroll)
    dd_abs, dd_frac = max_drawdown(curve)

    gross = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    net = sum(t["net_pnl"] for t in trades)

    preds = [(t["model_prob"], t["outcome"]) for t in trades
             if t.get("model_prob") is not None and t.get("outcome") is not None]
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]

    return {
        "label": label,
        "n_trades": len(trades),
        "n_settled": sum(1 for t in trades if t.get("settled")),
        "n_liquidated_at_end": sum(1 for t in trades if t.get("liquidated_at_end")),
        # Voids are counted but excluded from every forecasting metric: a
        # cancelled match makes a forecast unresolved, not wrong.
        "n_voided": sum(1 for t in trades if t.get("voided")),
        "starting_bankroll": starting_bankroll,
        "ending_bankroll": round(starting_bankroll + net, 4),

        "gross_pnl": round(gross, 4),
        "fees": round(fees, 4),
        "net_pnl": round(net, 4),
        "return_pct": round(net / starting_bankroll, 6) if starting_bankroll else None,

        "max_drawdown": round(dd_abs, 4),
        "max_drawdown_pct": round(dd_frac, 6),
        "sharpe": round(sharpe(trades), 4),
        "sortino": round(sortino(trades), 4),

        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "avg_win": round(_mean([t["net_pnl"] for t in wins]), 4) if wins else 0.0,
        "avg_loss": round(_mean([t["net_pnl"] for t in losses]), 4) if losses else 0.0,
        "turnover": round(sum(t["cost"] for t in trades), 4),

        "brier": brier(preds),
        "log_loss": log_loss(preds),
        "calibration": calibration(preds),

        "by_days_to_resolution": segment(trades, "days_to_resolution"),
        "by_price": segment(trades, "price"),
        "by_series": segment(trades, "series"),
        "by_month": segment(trades, "month"),

        "rejections": rejections or {},
        "equity_curve": [[ts, round(eq, 4)] for ts, eq in curve],
        "caveat": ("Fills are modelled at candlestick resolution with no L2 book. "
                   "Backtest P&L is an UPPER BOUND; paper trading is the real check."),
    }
