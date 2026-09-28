"""Chart for docs/findings/05_PREREG_BATCH1.md: the develop / test / forward split.

    venv/bin/python scripts/prereg_split_chart.py [data/market_history.db]

Settled Kalshi soccer winner games per day, coloured by the period each falls
in. Writes docs/findings/img/12_data_split.png. Needs matplotlib.
"""
import os
import sqlite3
import sys
from datetime import date, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import BLUE, INK_2, MUTED, ORANGE, SURFACE, save  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVELOP_END = date(2026, 8, 31)     # last day of the develop period, inclusive
TEST_END = date(2026, 9, 14)        # last day the local store covers


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "market_history.db")
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT date(close_time, 'unixepoch'), COUNT(DISTINCT event_ticker) FROM markets "
        "WHERE series LIKE '%GAME' AND result IN ('yes','no') GROUP BY 1 ORDER BY 1").fetchall()
    days = [(date.fromisoformat(d), n) for d, n in rows]
    dev = sum(n for d, n in days if d <= DEVELOP_END)
    test = sum(n for d, n in days if DEVELOP_END < d <= TEST_END)

    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.bar([d for d, _ in days], [n for _, n in days], width=0.8,
           color=[BLUE if d <= DEVELOP_END else ORANGE for d, _ in days],
           edgecolor=SURFACE, linewidth=0.6)
    fwd_start = TEST_END + timedelta(days=14)
    fwd_end = fwd_start + timedelta(days=30)
    ax.axvspan(fwd_start, fwd_end, color="#f0efec")
    top = max(n for _, n in days)
    ax.text(fwd_start + (fwd_end - fwd_start) / 2, top * 0.55,
            "FORWARD\npaper trading\n≥30 days, ≥100 bets\nrules untouched",
            ha="center", va="center", color=INK_2, fontsize=10)
    first = days[0][0]
    ax.text(first + (DEVELOP_END - first) / 2, top * 0.92,
            f"DEVELOP  ·  {dev:,} games\ntune thresholds freely", ha="center", va="top",
            color=BLUE, fontsize=10.5, fontweight="bold")
    ax.text(DEVELOP_END + (TEST_END - DEVELOP_END) / 2, top * 1.12,
            f"TEST  ·  {test:,} games\nrun once, frozen rules", ha="center", va="top",
            color=ORANGE, fontsize=10.5, fontweight="bold")
    ax.axvline(DEVELOP_END + timedelta(hours=12), color=INK_2, lw=1, ls="--")
    ax.set_ylim(0, top * 1.18)
    ax.set_ylabel("Settled games per day")
    ax.grid(axis="x", visible=False)
    ax.set_title("Batch 1 data split: develop → test once → forward paper")
    fig.autofmt_xdate()
    fig.text(0.125, -0.02, "Local store (hourly bars). Forward period starts at sign-off; "
             "dates shown are illustrative.", fontsize=8.5, color=MUTED)
    save(fig, "12_data_split.png")


if __name__ == "__main__":
    main()
