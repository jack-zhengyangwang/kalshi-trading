"""Chart for docs/findings/07_PRICE_VS_MODEL.md: does the price matter once
we have a model?

    venv/bin/python scripts/finding07_price_vs_model.py [data/walks/20260922_1905]

Left: expected profit of a fixed $10 bet when the model says 75%, as a
function of the price paid. Right: logistic regression of the outcome on
logit(model) + logit(price) over elo-desk-fixed's settled bets; 99% intervals
from a bootstrap by game. Needs numpy and matplotlib.
"""
import json
import math
import os
import random
import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import BLUE, INK, INK_2, MUTED, ORANGE, save  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def logit(p):
    p = min(max(p, 0.001), 0.999)
    return math.log(p / (1 - p))


def fit(X, y):
    """Logistic regression by Newton's method."""
    b = np.zeros(X.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ b))
        w = p * (1 - p)
        b += np.linalg.solve(X.T @ (X * w[:, None]) + 1e-9 * np.eye(len(b)), X.T @ (y - p))
    return b


def main():
    walk = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "walks", "20260922_1905")
    d = json.load(open(os.path.join(walk, "pm-elo-desk-fixed.json")))
    trades = [t for t in d["trades"] if t["settled"] and t["outcome"] is not None
              and t.get("model_prob") and 0 < t["entry_price"] < 1]
    X = np.array([[1, logit(t["model_prob"]), logit(t["entry_price"])] for t in trades])
    y = np.array([t["outcome"] for t in trades], float)
    b = fit(X, y)
    games = {}
    for i, t in enumerate(trades):
        games.setdefault(t["ticker"].rsplit("-", 1)[0], []).append(i)
    keys, rng = list(games), random.Random(0)
    boots = []
    for _ in range(1000):
        idx = [i for k in rng.choices(keys, k=len(keys)) for i in games[k]]
        boots.append(fit(X[idx], y[idx]))
    lo, hi = np.percentile(np.array(boots), [0.5, 99.5], axis=0)
    brier_m = float(np.mean((np.array([t["model_prob"] for t in trades]) - y) ** 2))
    brier_p = float(np.mean((np.array([t["entry_price"] for t in trades]) - y) ** 2))
    print(f"n={len(trades)} games={len(keys)} coef model={b[1]:.3f} [{lo[1]:.3f},{hi[1]:.3f}] "
          f"market={b[2]:.3f} [{lo[2]:.3f},{hi[2]:.3f}] brier model={brier_m:.4f} "
          f"market={brier_p:.4f}")

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(12.5, 4.9),
                                  gridspec_kw={"width_ratios": [1.25, 1]})
    prices = np.linspace(0.3, 0.95, 200)
    ev = 10 * (0.75 / prices - 1)
    ax.axhline(0, color=INK_2, lw=1)
    ax.fill_between(prices, ev, 0, where=ev >= 0, color=BLUE, alpha=0.15)
    ax.fill_between(prices, ev, 0, where=ev < 0, color=ORANGE, alpha=0.15)
    ax.plot(prices, ev, color=INK, lw=2)
    for p, off in ((0.40, (10, 6)), (0.75, (-12, -24)), (0.80, (10, 14))):
        v = 10 * (0.75 / p - 1)
        ax.plot(p, v, "o", color=INK, ms=7)
        ax.annotate(f"{int(p * 100)}¢: {10 / p:.1f} contracts → {'+' if v >= 0 else '−'}\\${abs(v):.2f}",
                    (p, v), xytext=off, textcoords="offset points", fontsize=9.5, color=INK,
                    ha="right" if off[0] < 0 else "left")
    ax.set_xlabel("Price paid for the home-win contract")
    ax.set_ylabel(r"Expected profit of a \$10 bet (\$)")
    ax.set_title("Model says 75%: the price decides the profit", fontsize=12)

    names = ["Elo model", "Market price"]
    for i, (c, color) in enumerate(zip((1, 2), (MUTED, BLUE))):
        ax2.plot([lo[c], hi[c]], [i, i], color=color, lw=5, solid_capstyle="butt", alpha=0.7)
        ax2.plot(b[c], i, "o", color=color, ms=10, mec="white", mew=1.5)
        ax2.text(hi[c] + 0.08, i, f"{b[c]:+.2f}", va="center", fontsize=10, color=INK)
    ax2.axvline(0, color=INK_2, lw=1.2, ls="--")
    ax2.set_yticks([0, 1])
    ax2.set_yticklabels(names, color=INK)
    ax2.set_ylim(-0.7, 1.7)
    ax2.set_xlabel("Weight in predicting the outcome, both together (99% interval)")
    ax2.grid(axis="y", visible=False)
    ax2.set_title("Once you know the price, this model adds nothing", fontsize=12)
    fig.suptitle("Is the price independent of the outcome? No.", x=0.02, ha="left",
                 fontsize=14, fontweight="bold", color=INK)
    fig.text(0.02, -0.03, f"Right: {len(trades)} settled bets by elo-desk-fixed (walk 2026-06-03 → 09-14), "
             f"logistic regression, bootstrap by game. Brier: market {brier_p:.3f}, model {brier_m:.3f}.",
             fontsize=8.5, color=MUTED)
    fig.tight_layout()
    save(fig, "16_price_vs_model.png")


if __name__ == "__main__":
    main()
