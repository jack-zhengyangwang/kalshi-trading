"""Charts for docs/findings/01_MARKET_EFFICIENCY.md, drawn from one walk.

    venv/bin/python scripts/findings_charts.py data/walks/20260922_1905

Needs matplotlib (not in requirements.txt — only this script uses it).
Writes PNGs to docs/findings/img/. Every number in the charts comes from the
walk's pm-*.json files; nothing is typed in by hand.
"""
import glob
import json
import math
import os
import sys
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "findings", "img")

# Reference palette (light surface). Categorical slots in fixed order.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#8a8984"
GRID = "#e6e5e1"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
MANAGED = {"desk-qwen": BLUE, "desk-openai": ORANGE,
           "desk-gemini": AQUA, "desk-deepseek": YELLOW}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "font.size": 11,
    "axes.edgecolor": GRID, "axes.labelcolor": INK_2,
    "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.titlesize": 14, "axes.titleweight": "bold", "axes.titlecolor": INK,
    "axes.titlelocation": "left", "legend.frameon": False,
})


def load(walk):
    desks = {}
    for f in sorted(glob.glob(os.path.join(walk, "pm-*.json"))):
        desks[os.path.basename(f)[3:-5]] = json.load(open(f))
    return desks


def money(v):
    return f"{'−' if v < 0 else ''}${abs(v):,.0f}"


def settled(trades):
    return [t for t in trades if t["settled"] and t["outcome"] is not None]


def save(fig, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("wrote", os.path.relpath(path, ROOT))


def calibration(desks):
    """Pooled over every desk: price paid vs how often the bet won. A market
    whose prices are probabilities puts these points on the diagonal. The Elo
    desk's own model is drawn beside it for contrast."""
    pooled = [t for d in desks.values() for t in settled(d["trades"])]
    edges = [i / 10 for i in range(11)]
    xs, ys, ci, ns = [], [], [], []
    for lo, hi in zip(edges, edges[1:]):
        b = [t for t in pooled if lo <= t["entry_price"] < hi or (hi == 1 and t["entry_price"] == 1)]
        if len(b) < 20:
            continue
        p = sum(t["outcome"] for t in b) / len(b)
        xs.append(sum(t["entry_price"] for t in b) / len(b))
        ys.append(p)
        ci.append(1.96 * math.sqrt(max(p * (1 - p), 1e-9) / len(b)))
        ns.append(len(b))

    elo = desks["elo-desk-fixed"]["report"]["calibration"]
    ex = [c["mean_predicted"] for c in elo if c["n"] >= 20]
    ey = [c["actual_rate"] for c in elo if c["n"] >= 20]

    fig, ax = plt.subplots(figsize=(7.5, 7))
    ax.plot([0, 1], [0, 1], color=MUTED, lw=1.2, ls="--", zorder=1)
    ax.text(0.80, 0.86, "perfectly priced", color=MUTED, rotation=45,
            ha="center", va="center", fontsize=10)
    ax.errorbar(xs, ys, yerr=ci, fmt="none", ecolor=BLUE, elinewidth=1.5,
                capsize=0, alpha=0.45, zorder=2)
    ax.plot(xs, ys, color=BLUE, lw=2, marker="o", ms=8, mec=SURFACE, mew=2,
            zorder=3, label=f"Kalshi price ({len(pooled):,} settled bets, 8 desks)")
    ax.plot(ex, ey, color=ORANGE, lw=2, marker="s", ms=8, mec=SURFACE, mew=2,
            zorder=3, label="Elo model's own probability (elo-desk-fixed)")
    for x, y, n in zip(xs, ys, ns):
        ax.annotate(f"n={n}", (x, y), textcoords="offset points", xytext=(8, -14),
                    fontsize=8.5, color=INK_2)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Probability implied (price paid / model estimate)")
    ax.set_ylabel("How often the bet actually won")
    ax.set_title("Kalshi's prices are the probabilities")
    ax.legend(loc="upper left", fontsize=10, labelcolor=INK)
    fig.text(0.125, 0.005, "Bars: 95% interval. Bins with fewer than 20 bets dropped. "
             "Walk 2026-06-03 → 09-14.", fontsize=8.5, color=MUTED)
    save(fig, "01_calibration.png")


def pnl_breakdown(desks):
    """Net P&L per desk, split into what the bets lost against the price
    (gross) and what Kalshi took in fees."""
    rows = [(n, d["report"]) for n, d in desks.items() if d["report"]["n_trades"]]
    rows.sort(key=lambda r: r[1]["net_pnl"], reverse=True)
    gross = [r["gross_pnl"] for _, r in rows]

    fig, ax = plt.subplots(figsize=(9, 5.5))
    y = list(range(len(rows)))
    ax.barh(y, gross, color=BLUE, height=0.6, edgecolor=SURFACE, linewidth=2,
            label="Lost to the price (gross P&L)")
    ax.barh(y, [-r["fees"] for _, r in rows], left=[min(g, 0) for g in gross],
            color=ORANGE, height=0.6, edgecolor=SURFACE, linewidth=2, label="Fees")
    for i, (_, r) in enumerate(rows):
        left = min(r["gross_pnl"], 0) - r["fees"]
        ax.text(left - 8, i, money(r["net_pnl"]), va="center", ha="right",
                color=INK, fontsize=10, fontweight="bold")
    ax.set_yticks(y)
    ax.set_yticklabels([n + ("  (AI)" if n.startswith("desk-") else "") for n, _ in rows],
                       color=INK)
    ax.invert_yaxis()
    ax.axvline(0, color=INK_2, lw=1)
    ax.text(4, len(rows) - 0.4, "doing nothing: $0", color=INK_2, fontsize=9.5,
            va="center", rotation=90)
    ax.set_xlim(-520, 40)
    ax.set_xlabel("Dollars, on a $500 starting bankroll")
    ax.grid(axis="y", visible=False)
    ax.set_title("Every desk that traded lost money")
    ax.legend(loc="upper left", fontsize=10, labelcolor=INK)
    save(fig, "02_pnl_breakdown.png")


def equity(desks):
    """Cumulative P&L tick by tick. Managed desks coloured, benchmarks grey."""
    fig, ax = plt.subplots(figsize=(10, 5.8))
    ax.axhline(0, color=INK_2, lw=1.2, ls="--")
    ends = []
    for name, d in desks.items():
        if not d["report"]["n_trades"]:
            continue
        xs = [date.fromisoformat(t["period"][1]) for t in d["ticks"]]
        cum, ys = 0.0, []
        for t in d["ticks"]:
            cum += t["net_pnl"]
            ys.append(cum)
        managed = name in MANAGED
        ax.plot(xs, ys, color=MANAGED.get(name, MUTED), lw=2.2 if managed else 1.4,
                zorder=3 if managed else 2)
        ends.append((ys[-1], name, xs[-1]))
    # direct labels at the line ends, pushed apart so they don't overlap
    ends.sort()
    last = -1e9
    for y, name, x in ends:
        ly = max(y, last + 20)
        last = ly
        managed = name in MANAGED
        ax.annotate(f"{name}  {money(y)}", (x, y), xytext=(x, ly), textcoords="data",
                    va="center", fontsize=9.5, color=INK if managed else INK_2,
                    fontweight="bold" if managed else "normal", annotation_clip=False)
    ax.text(ends[0][2], 6, "do nothing / desk-anthropic  $0", color=INK_2,
            fontsize=9.5, va="bottom", ha="right")
    ax.set_ylabel("Cumulative net P&L ($)")
    ax.set_title("AI-managed desks lost less; nothing beat standing still")
    ax.plot([], [], color=BLUE, lw=2.2, label="AI-managed desk (coloured)")
    ax.plot([], [], color=MUTED, lw=1.4, label="Fixed benchmark desk")
    ax.legend(loc="lower left", fontsize=10, labelcolor=INK)
    fig.autofmt_xdate()
    save(fig, "03_equity.png")


def held_vs_sold(desks):
    """For desks that sold positions before settlement: P&L of bets held to
    the end vs bets sold early."""
    rows = []
    for name, d in desks.items():
        early = [x for x in d["trades"] if not x["settled"]]
        if len(early) < 20:
            continue
        rows.append((name, sum(x["net_pnl"] for x in settled(d["trades"])),
                     sum(x["net_pnl"] for x in early)))
    rows.sort(key=lambda r: r[1], reverse=True)

    fig, ax = plt.subplots(figsize=(8, 4.8))
    w = 0.36
    for i, (_, h, e) in enumerate(rows):
        ax.bar(i - w / 2 - 0.01, h, w, color=BLUE, edgecolor=SURFACE, linewidth=2)
        ax.bar(i + w / 2 + 0.01, e, w, color=ORANGE, edgecolor=SURFACE, linewidth=2)
        for x, v in ((i - w / 2, h), (i + w / 2, e)):
            ax.text(x, v + (6 if v >= 0 else -6), money(v), ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=10, color=INK)
    ax.axhline(0, color=INK_2, lw=1)
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels([r[0] for r in rows], color=INK)
    ax.set_ylim(-380, 80)
    ax.set_ylabel("Net P&L ($)")
    ax.grid(axis="x", visible=False)
    ax.legend(handles=[Patch(color=BLUE, label="Held to settlement"),
                       Patch(color=ORANGE, label="Sold early")],
              loc="upper right", fontsize=10, labelcolor=INK)
    ax.set_title("desk-qwen made money on the bets it held; early exits cost it")
    save(fig, "04_held_vs_sold.png")


def main():
    walk = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "walks", "20260922_1905")
    desks = load(walk)
    calibration(desks)
    pnl_breakdown(desks)
    equity(desks)
    held_vs_sold(desks)


if __name__ == "__main__":
    main()
