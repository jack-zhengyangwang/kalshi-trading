"""Charts for docs/findings/06_BATCH1_RESULTS.md.

    venv/bin/python scripts/batch1_charts.py [data/market_history.db]

Needs the kickoffs table (python -m wc.research.kickoff) and matplotlib.
"""
import os
import sqlite3
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import BLUE, INK, INK_2, MUTED, ORANGE, save  # noqa: E402
from wc.research import batch1 as b1  # noqa: E402


def closest_three_way(con):
    """Per game: the cheapest the whole YES set ever got before kickoff, with
    and without taker fees. Under 100c = free money."""
    with_fee, no_fee = [], []
    for period in b1.PERIODS:
        for g in b1.load_games(con, period):
            if not b1.common_bars(g):
                continue
            legs, ko = g["legs"], g["kickoff"]
            ts = set.intersection(*[{t for t, q in b.items() if t < ko and b1.valid(q)}
                                    for b in legs.values()])
            no_fee.append(min(sum(b[t][1] for b in legs.values()) for t in ts))
            with_fee.append(min(sum(b[t][1] + b1.taker_fee(b[t][1]) for b in legs.values())
                                for t in ts))
    under = sum(1 for x in no_fee if x < 100)

    fig, ax = plt.subplots(figsize=(10, 5.2))
    bins = [x - 0.5 for x in range(94, 114)]
    ax.hist(no_fee, bins=bins, histtype="step", lw=2.2, color=BLUE,
            label="before fees: Σ YES asks")
    ax.hist(with_fee, bins=bins, histtype="step", lw=2.2, color=ORANGE,
            label="after taker fees: Σ asks + Σ fees")
    ax.axvline(100, color=INK_2, lw=1.2, ls="--")
    ax.text(99.6, ax.get_ylim()[1] * 0.95, "free money ←", ha="right", va="top",
            color=INK_2, fontsize=10)
    ax.text(93.8, ax.get_ylim()[1] * 0.55,
            f"{under} of {len(no_fee)} games dipped\nunder $1 before fees", color=BLUE,
            fontsize=10)
    ax.text(107.5, ax.get_ylim()[1] * 0.55,
            f"0 after fees\n(closest: {min(with_fee)}¢)", color=ORANGE, fontsize=10)
    ax.set_xlim(93.5, 113.5)
    ax.set_xlabel("Cheapest cost of buying all three outcomes, per game (¢)")
    ax.set_ylabel("Games")
    ax.grid(axis="x", visible=False)
    ax.set_title("#4 Three-way sum: fees close every gap")
    ax.legend(loc="upper right", fontsize=10, labelcolor=INK)
    fig.text(0.125, 0.0, f"{len(no_fee)} games with all three legs quoted in the same hour, "
             "develop + test periods.", fontsize=8.5, color=MUTED)
    save(fig, "13_threeway_closest.png")


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "market_history.db")
    con = sqlite3.connect(db)
    closest_three_way(con)


if __name__ == "__main__":
    main()
