"""Chart for docs/findings/04_STRATEGY_IDEAS.md: 12 candidate rules on one map.

    venv/bin/python scripts/strategy_map.py

Evidence grade and testability are judgements from the literature review in
that doc (A/B/C; ours / partial / new data), not measurements.
"""
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import AQUA, BLUE, INK, INK_2, ORANGE, SURFACE, save  # noqa: E402

# (number, short name, evidence 1-3, testable with our data 1-3, where the evidence comes from)
IDEAS = [
    (1, "Maker orders, never take", 3, 2, "Kalshi"),
    (2, "Sell longshots (NO ≤10¢)", 2, 3, "Kalshi"),
    (3, "Bookmaker value gap", 2, 2.4, "Betfair / bookmakers"),
    (4, "Three-way sum arbitrage", 2, 2.8, "Polymarket"),
    (5, "Kalshi vs Polymarket arbitrage", 1, 1, "Polymarket"),
    (6, "Draw in tight matches", 1.6, 3, "Betfair / bookmakers"),
    (7, "Buy home/away longshots", 1, 2.7, "Betfair / bookmakers"),
    (8, "In-play: surprise-goal overreaction", 2, 1, "Betfair / bookmakers"),
    (9, "Late-game fade of the trailing team", 1, 1.3, "Kalshi"),
    (10, "Fade popular clubs", 1, 2.2, "Betfair / bookmakers"),
    (11, "Neutral-venue home fade", 1.2, 2.5, "Betfair / bookmakers"),
    (12, "Liquidity-incentive rebates", 2.7, 1.4, "Kalshi"),
]
COLORS = {"Kalshi": BLUE, "Polymarket": AQUA, "Betfair / bookmakers": ORANGE}
GRADE = {3: "A", 2.7: "A", 2: "B", 1.6: "B–C", 1.2: "C", 1: "C"}
TEST = {3: "our data", 2.8: "our data", 2.7: "our data", 2.5: "our data", 2.4: "our data*",
        2.2: "our data*", 2: "partly", 1.4: "new data", 1.3: "new data", 1: "new data"}


def main():
    rows = sorted(IDEAS, key=lambda r: r[2] * r[3], reverse=True)
    fig, ax = plt.subplots(figsize=(10.5, 7))
    y = range(len(rows))
    ax.barh(y, [r[2] * r[3] for r in rows], color=[COLORS[r[4]] for r in rows],
            edgecolor=SURFACE, linewidth=2, height=0.62)
    for i, (n, name, ev, te, venue) in enumerate(rows):
        ax.text(ev * te + 0.12, i, f"evidence {GRADE[ev]} · testable: {TEST[te]}",
                va="center", fontsize=9, color=INK_2)
    ax.set_yticks(list(y))
    ax.set_yticklabels([f"{r[0]:>2}. {r[1]}" for r in rows], color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 11.5)
    ax.set_xticks([])
    ax.grid(visible=False)
    ax.spines["bottom"].set_visible(False)
    ax.set_xlabel("Priority = evidence strength × how testable it is with our data", color=INK_2)
    ax.set_title("12 rule-based strategy ideas, ranked")
    for venue, c in COLORS.items():
        ax.barh([0], [0], color=c, label=f"evidence from {venue}")
    ax.legend(loc="lower right", fontsize=9.5, labelcolor=INK)
    fig.text(0.02, 0.0, "* our data, but ESPN odds / fill estimates are approximate. "
             "Grades are judgements from the literature review, not measurements.",
             fontsize=8.5, color=INK_2)
    save(fig, "11_strategy_map.png")


if __name__ == "__main__":
    main()
