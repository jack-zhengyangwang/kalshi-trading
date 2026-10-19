"""Chart for docs/findings/09_PHASE1_BASELINES.md: Phase 1 models vs the market.

    venv/bin/python -m wc.research.models
    venv/bin/python scripts/phase1_charts.py

Left: accuracy (leg Brier) of each model against Pinnacle closing (Table A test)
and Kalshi at 24h (Table B). Right: Gate 2, the model's weight next to the
market price, with 99% intervals.
"""
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import BLUE, INK, INK_2, ORANGE, save  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"M0_base_rate": "M0 base rates", "M1_elo": "M1 Elo", "M2_lr_all": "M2 all, linear",
         "M3_hgb_all": "M3 all, trees"}


def main():
    r = json.load(open(os.path.join(ROOT, "docs", "findings", "results", "v2_phase1.json")))
    models = [m for m in NAMES if m in r]
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    for k, (key, label, color) in enumerate((("A_test", "vs Pinnacle closing (Jan–May 2026)", BLUE),
                                             ("B_24h", "vs Kalshi 24h (Jul–Sep 2026)", ORANGE))):
        ys = [i + (k - 0.5) * 0.36 for i in range(len(models))]
        vals = [r[m][key]["gate1"]["brier_model"] for m in models]
        market = r[models[0]][key]["gate1"]["brier_market"]
        ax.barh(ys, vals, height=0.34, color=color, alpha=0.85, label=label)
        ax.axvline(market, color=color, lw=2, ls="--")
        right = market >= r[models[0]][("B_24h", "A_test")[k]]["gate1"]["brier_market"]
        ax.text(market + (0.0003 if right else -0.0003), -0.72, f"{market:.4f}", color=color,
                fontsize=9, va="bottom", ha="left" if right else "right")
        for y, v in zip(ys, vals):
            ax.text(v + 0.0004, y, f"{v:.4f}", va="center", fontsize=8.5, color=INK)
    ax.set_yticks(range(len(models)))
    ax.set_yticklabels([NAMES[m] for m in models], color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0.19, 0.222)
    ax.set_xlabel("Leg Brier score (lower = better). Dashed = the market")
    ax.grid(axis="y", visible=False)
    ax.set_title("Gate 1: no model is as accurate as the market", fontsize=12)

    for k, (key, color) in enumerate((("A_test", BLUE), ("B_24h", ORANGE))):
        for i, m in enumerate(models):
            g = r[m][key]["gate2"]
            y = i + (k - 0.5) * 0.3
            ax2.plot(g["ci99_model"], [y, y], color=color, lw=4, alpha=0.6, solid_capstyle="butt")
            ax2.plot(g["coef_model"], y, "o", color=color, ms=8, mec="white", mew=1.5)
    ax2.axvline(0, color=INK_2, lw=1.2, ls="--")
    ax2.set_yticks(range(len(models)))
    ax2.set_yticklabels([NAMES[m] for m in models], color=INK)
    ax2.invert_yaxis()
    ax2.set_xlabel("Model's weight next to the market price (99% interval); passes if > 0")
    ax2.grid(axis="y", visible=False)
    ax2.set_title("Gate 2: no model adds information to the price", fontsize=12)

    fig.suptitle("v2 Phase 1: public-data models don't beat the market (0 of 4 pass)", x=0.01,
                 ha="left", fontsize=14, fontweight="bold", color=INK)
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, fontsize=9.5, labelcolor=INK,
               frameon=False, bbox_to_anchor=(0.5, -0.04))
    ax.set_ylim(len(models) - 0.5, -0.85)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    save(fig, "18_phase1_gates.png")


if __name__ == "__main__":
    main()
