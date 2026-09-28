"""Charts for docs/findings/02_MIDRANGE.md: is the 40-60c range mispriced?

    venv/bin/python scripts/finding02_midrange.py [data/market_history.db]

Uses EVERY settled winner market in the history DB (not the trades a desk
chose), priced from the last hourly bar 2h / 6h / 24h before close. Writes PNGs
to docs/findings/img/. Needs matplotlib.
"""
import math
import os
import random
import sqlite3
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import BLUE, INK, INK_2, MUTED, ORANGE, AQUA, SURFACE, save  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOURS = (24, 6, 2)
COLORS = {24: BLUE, 6: ORANGE, 2: AQUA}
MAX_SPREAD_C = 10


def fee(p):
    """Kalshi taker fee for a one-contract order, rounded up to the cent."""
    return math.ceil(0.07 * p * (1 - p) * 100) / 100


def snapshot(con, hours):
    """One row per settled market: bid/ask from the last hourly bar at least
    `hours` before close (and no more than 48h older than that)."""
    rows = []
    q = ("SELECT yes_bid, yes_ask FROM backfill_candles WHERE ticker=? AND interval_min=60 "
         "AND ts<=? AND ts>=? AND yes_bid>0 AND yes_ask>0 ORDER BY ts DESC LIMIT 1")
    for t, ev, ct, res in con.execute(
            "SELECT ticker, event_ticker, close_time, result FROM markets "
            "WHERE result IN ('yes','no') AND series LIKE '%GAME'"):
        r = con.execute(q, (t, ct - hours * 3600, ct - (hours + 48) * 3600)).fetchone()
        if not r or r[1] - r[0] > MAX_SPREAD_C:
            continue
        rows.append({"ev": ev, "mid": (r[0] + r[1]) / 200, "ask": r[1] / 100,
                     "y": 1 if res == "yes" else 0})
    return rows


def gap_chart(snaps):
    """Won minus price, per 5c bin, 30-75c, one line per snapshot time."""
    fig, ax = plt.subplots(figsize=(10, 5.6))
    ax.axvspan(40, 55, color="#f0efec", zorder=0)
    ax.text(47.5, 14.5, "the range you spotted", ha="center", color=INK_2, fontsize=10)
    ax.axhline(0, color=INK_2, lw=1.2, ls="--")
    for k, h in enumerate(HOURS):
        xs, ys, es = [], [], []
        for lo in range(30, 75, 5):
            b = [x for x in snaps[h] if lo / 100 <= x["mid"] < (lo + 5) / 100]
            if len(b) < 20:
                continue
            w = sum(x["y"] for x in b) / len(b)
            m = sum(x["mid"] for x in b) / len(b)
            xs.append(lo + 2.5 + (k - 1) * 1.1)
            ys.append(100 * (w - m))
            es.append(196 * math.sqrt(w * (1 - w) / len(b)))
        ax.errorbar(xs, ys, yerr=es, color=COLORS[h], lw=2, marker="o", ms=7,
                    mec=SURFACE, mew=1.5, elinewidth=1.2, alpha=0.95, capsize=0,
                    label=f"{h}h before close")
    ax.set_xlim(30, 75)
    ax.set_ylim(-22, 17)
    ax.set_xlabel("Market price (mid, ¢)")
    ax.set_ylabel("Won minus price (percentage points)")
    ax.set_title("40–45¢ wins more than priced at every time — but inside the noise")
    ax.legend(loc="lower left", fontsize=10, labelcolor=INK, ncol=3)
    n = len(snaps[6])
    fig.text(0.125, 0.0, f"All {n:,} settled Kalshi soccer winner markets in the local store "
             "(not desk picks). Bars: 95% interval.", fontsize=8.5, color=MUTED)
    save(fig, "05_midrange_gap.png")


def pnl_chart(snaps):
    """Buy YES at the ask + fee in a price band: cents per contract, with a
    95% bootstrap interval clustered by game."""
    bands = [(0.40, 0.45), (0.40, 0.55), (0.40, 0.60)]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.axhline(0, color=INK_2, lw=1.2)
    random.seed(0)
    for k, h in enumerate(HOURS):
        for j, (lo, hi) in enumerate(bands):
            b = [x for x in snaps[h] if lo <= x["mid"] < hi]
            ev = defaultdict(list)
            for x in b:
                ev[x["ev"]].append(x["y"] - x["ask"] - fee(x["ask"]))
            keys = list(ev)
            boots = sorted(
                sum(v for key in random.choices(keys, k=len(keys)) for v in ev[key]) / len(b)
                for _ in range(2000))
            mean = sum(v for vs in ev.values() for v in vs) / len(b)
            x = j + (k - 1) * 0.22
            ax.plot([x, x], [100 * boots[50], 100 * boots[1950]], color=COLORS[h], lw=2.5,
                    alpha=0.5, solid_capstyle="round")
            ax.plot(x, 100 * mean, "o", color=COLORS[h], ms=9, mec=SURFACE, mew=1.5,
                    label=f"{h}h before close" if j == 0 else None)
            ax.annotate(f"{100 * mean:+.1f}¢", (x, 100 * mean), xytext=(7, 0),
                        textcoords="offset points", va="center", fontsize=9, color=INK)
    ax.set_xticks(range(len(bands)))
    ax.set_xticklabels([f"buy YES at {int(lo * 100)}–{int(hi * 100)}¢\n"
                        f"n≈{sum(1 for x in snaps[6] if lo <= x['mid'] < hi)}"
                        for lo, hi in bands], color=INK)
    ax.set_xlim(-0.6, 2.6)
    ax.set_ylabel("Profit per $1 contract after spread + fee (¢)")
    ax.grid(axis="x", visible=False)
    ax.set_title("After paying the ask and the fee, every interval still includes $0")
    ax.legend(loc="upper right", fontsize=10, labelcolor=INK)
    save(fig, "06_midrange_pnl.png")


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "market_history.db")
    con = sqlite3.connect(db)
    snaps = {h: snapshot(con, h) for h in HOURS}
    gap_chart(snaps)
    pnl_chart(snaps)


if __name__ == "__main__":
    main()
