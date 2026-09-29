"""Chart for docs/findings/10_PHASE2_TIERS.md: Gate 2 by league tier and entry time.

    venv/bin/python scripts/phase2_charts.py

Reads docs/findings/results/v2_phase2.json (the full-data run on the droplet).
"""
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import AQUA, BLUE, INK, INK_2, MUTED, ORANGE, save  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIERS = {"T1": "T1 top leagues", "T2": "T2 other first divisions", "T3": "T3 lower divisions"}
SERIES = [("A", None, "vs Pinnacle closing (Jan–May 2026)", MUTED),
          ("B", "24", "vs Kalshi, 24h before", BLUE),
          ("B", "6", "vs Kalshi, 6h before", ORANGE),
          ("B", "1", "vs Kalshi, 1h before", AQUA)]


def main():
    r = json.load(open(os.path.join(ROOT, "docs", "findings", "results", "v2_phase2.json")))
    g = r["groups"]
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.4), sharey=True,
                                  gridspec_kw={"width_ratios": [1.3, 1]})
    for k, (tbl, h, label, color) in enumerate(SERIES):
        for i, t in enumerate(TIERS):
            key = f"A|{t}" if tbl == "A" else f"B|{t}|{h}"
            v = g.get(key)
            if not v or not v["judged"]:
                continue
            y = i + (k - 1.5) * 0.19
            g2, g1 = v["gate2"], v["gate1"]
            ax.plot(g2["ci99_model"], [y, y], color=color, lw=4, alpha=0.6, solid_capstyle="butt")
            ax.plot(g2["coef_model"], y, "o", color=color, ms=7, mec="white", mew=1.2,
                    label=label if i == 0 else None)
            ax2.barh(y, 1000 * (g1["brier_model"] - g1["brier_market"]), height=0.17,
                     color=color)
            ax2.text(1000 * (g1["brier_model"] - g1["brier_market"]) + 0.15, y,
                     f"{v['legs']:,} legs", va="center", fontsize=7.5, color=INK_2)
    ax.axvline(0, color=INK_2, lw=1.2, ls="--")
    ax.set_yticks(range(len(TIERS)))
    ax.set_yticklabels(list(TIERS.values()), color=INK)
    ax.invert_yaxis()
    ax.set_xlabel("Model's weight next to the market price (99% interval); passes if > 0")
    ax.grid(axis="y", visible=False)
    ax.set_title("Gate 2: no tier, no entry time adds information", fontsize=12)
    ax2.axvline(0, color=INK_2, lw=1.2)
    ax2.set_xlabel("Model Brier minus market Brier (×1000); < 0 = model better")
    ax2.grid(axis="y", visible=False)
    ax2.set_title("Gate 1: the market is more accurate everywhere", fontsize=12)
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, fontsize=9.5, labelcolor=INK,
               frameon=False, bbox_to_anchor=(0.5, -0.03))
    fig.suptitle("v2 Phase 2 (full droplet data): the market isn't weaker in any tier",
                 x=0.01, ha="left", fontsize=14, fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    save(fig, "19_phase2_tiers.png")


if __name__ == "__main__":
    main()
