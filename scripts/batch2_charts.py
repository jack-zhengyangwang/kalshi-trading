"""Chart for docs/findings/12_BATCH2_RESULTS.md, from the saved results JSON.

    venv/bin/python scripts/batch2_charts.py
"""
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import BLUE, INK_2, MUTED, ORANGE, save  # noqa: E402

RES = os.path.join(ROOT, "docs", "findings", "results")
NAMES = {"h1": "H1 fade big clubs", "h2": "H2 fade overreaction",
         "h3": "H3 buy the draw", "h4": "H4 fade busy favourites"}


def main():
    with open(os.path.join(RES, "batch2_develop.json")) as f:
        dev = json.load(f)
    with open(os.path.join(RES, "batch2_test.json")) as f:
        test = json.load(f)
    with open(os.path.join(RES, "batch2_confirm.json")) as f:
        conf = json.load(f)
    keys = list(NAMES)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.6), sharey=True)
    panels = (("Net ¢ per contract, after the ask and fees", "net"),
              ("Bias before costs: won − mid, ¢", "bias"))
    for ax, (title, what) in zip(axes, panels):
        for i, k in enumerate(keys):
            d = dev[k]["grid"][dev[k]["selected"]]
            t, c = test[k], conf[k]
            for off, r, color, label in ((-0.22, d, MUTED, "develop (Jul–Aug)"),
                                         (0.0, t, BLUE, "test (Sep 1–14), once"),
                                         (0.22, c, ORANGE, "confirm (Sep 16–Oct 17), once")):
                s = r if what == "net" else r["bias_before_costs"]
                lo, hi = s["ci99"]
                ax.plot([lo, hi], [i + off] * 2, color=color, lw=2.5, solid_capstyle="round")
                ax.plot(s["mean"], i + off, "o", color=color, ms=7,
                        label=label if i == 0 and ax is axes[0] else None)
                if what == "net" and r is not d:
                    ax.text(hi + 1, i + off, f"n={r['n_trades']}", va="center",
                            fontsize=9, color=INK_2)
        ax.axvline(0, color=INK_2, lw=1)
        ax.set_title(title, fontsize=11, loc="left", color=INK_2)
        ax.set_xlabel("¢ per contract, dots = mean, bars = 99% interval")
    axes[0].set_yticks(range(len(keys)), [NAMES[k] for k in keys])
    axes[0].invert_yaxis()
    fig.legend(loc="upper right", ncol=3, fontsize=9, frameon=False)
    fig.suptitle("Batch 2: 0 of 4 'dumb money' bets pass, in either test period",
                 x=0.01, ha="left", fontsize=13)
    save(fig, "20_batch2_results.png")


if __name__ == "__main__":
    main()
