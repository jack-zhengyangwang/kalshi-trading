# Phase 5 — Dashboard

*Presentation. Can slip without blocking anything upstream.*

---

## 1. Tooling note

**Playwright is not a dashboard framework** — it is browser automation for
testing. Its role here is screenshotting and E2E-testing the dashboard once it
exists, not building it.

For the dashboard itself:

| Option | Fit |
|---|---|
| **Published Artifact** (static HTML from `metrics.json`) | Openable from any device, no server, no port. Best fit for reviewing results |
| **Streamlit / Plotly Dash** | Native to the Python stack, interactive, but needs a running process |
| **Terminal dashboard** | Cheapest; mirrors `kalshi-crypto-bot`'s approach; fine for live monitoring, poor for calibration plots |

Default: generate a static Artifact from `metrics.json`. Add Streamlit only if
interactive parameter sweeps prove necessary.

## 2. What it shows

Out-of-sample by default. In-sample is behind an explicit toggle and labelled
as such — the honest number is the one on screen when nobody changed anything.

**Per strategy**
- Equity curve, with drawdown shaded
- Calibration plot: predicted vs actual, in deciles, with the diagonal
- Brier score and log loss
- Segmented performance by time-to-resolution, price bucket, and series — the
  axes the research says the edge lives on
- Gross PnL, fees, net PnL as three separate numbers
- Fill rate and rejection reasons

**Across strategies**
- Leaderboard, ranked on out-of-sample net PnL
- Same-window comparison, so ranking is never an artefact of different periods
- Backtest vs paper vs live PnL side by side, once paper exists — the
  triangulation that catches an over-fit backtest

## 3. Definition of done

- [x] Renders from `metrics.json` alone, with no live API calls
- [x] Out-of-sample shown by default; in-sample labelled "reference only, not
      evidence" and same-bar fills labelled "diagnostic"
- [x] Calibration plot with the reference diagonal, dot area by sample size
- [x] Leaderboard ranked on out-of-sample net P&L
- [x] Readable on a phone; light and dark themes

### What it turned into

`wc/backtest/dashboard.py` writes ONE self-contained HTML file. No server, no
build step, and no CDN — charts are hand-drawn SVG, because a pinned CDN script
is one more thing that can go missing between generating a report and reading
it, and the page has to render identically whenever it is opened.

```bash
python3 -m wc.backtest.dashboard          # -> data/backtests/dashboard.html
```

**The leaderboard flags rows, not just ranks them.** A strategy carrying
model-fitted lookahead or a latency sensitivity above 2x is marked, because the
row most likely to top a leaderboard is the one with the most inflated number.
A flagged row is not promotion evidence however good it looks — see
[07_PROMOTION.md](07_PROMOTION.md).
