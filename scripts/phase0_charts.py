"""Chart for docs/findings/08_DATASET.md: what the v2 dataset covers.

    venv/bin/python -m wc.research.dataset
    venv/bin/python scripts/phase0_charts.py

Left: Table A matches per year, with and without bookmaker odds. Middle: Kalshi
games joined to a soccer.db match. Right: first Brier bars to beat.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from findings_charts import BLUE, INK, INK_2, MUTED, ORANGE, save  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    con = sqlite3.connect(os.path.join(ROOT, "data", "research", "v2_dataset.db"))
    info = json.load(open(os.path.join(ROOT, "docs", "findings", "results", "v2_phase0.json")))
    years = {}
    for ko, has_odds in con.execute("SELECT kickoff, p_book_h IS NOT NULL FROM ds_matches"):
        y = datetime.fromtimestamp(ko, timezone.utc).year
        years.setdefault(y, [0, 0])[0 if has_odds else 1] += 1
    ys = sorted(years)

    fig, (a1, a2, a3) = plt.subplots(1, 3, figsize=(14, 4.8),
                                     gridspec_kw={"width_ratios": [1.5, 0.9, 1.2]})
    a1.bar(ys, [years[y][0] for y in ys], color=BLUE, label="with bookmaker odds")
    a1.bar(ys, [years[y][1] for y in ys], bottom=[years[y][0] for y in ys], color=MUTED,
           label="no odds")
    a1.set_title(f"Table A: {info['matches']:,} matches to learn from", fontsize=11.5)
    a1.set_ylabel("Finished matches")
    a1.grid(axis="x", visible=False)
    a1.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=2, fontsize=9, labelcolor=INK)
    a1.text(2026, years[2026][0] + years[2026][1] + 300, "odds end\nMay 13", ha="center",
            fontsize=8.5, color=INK_2)

    j, n = info["kalshi_joined"], info["kalshi_games"]
    a2.bar([0], [n], color=MUTED, width=0.6)
    a2.bar([0], [j], color=ORANGE, width=0.6)
    a2.text(0, n + 15, f"{n} Kalshi games\n(Jul–Sep, ESPN kickoff)", ha="center", fontsize=9.5,
            color=INK)
    a2.text(0.36, j, f"← {j} joined\n   ({info['kalshi_rows']:,} leg × entry-time rows)",
            va="center", fontsize=9.5, color=ORANGE)
    a2.set_xlim(-0.6, 1.4)
    a2.set_ylim(0, n * 1.3)
    a2.set_xticks([])
    a2.set_yticks([])
    a2.grid(axis="x", visible=False)
    a2.set_title("Table B: Kalshi games joined", fontsize=11.5)

    b = info["baselines"]
    rows = [("A: base rates", b["A_test"]["brier_base_rate"], MUTED),
            ("A: Pinnacle closing", b["A_test"]["brier_bookmaker"], BLUE),
            ("B: base rates", b["B_24h"]["brier_base_rate"], MUTED),
            ("B: Kalshi mid, 24h", b["B_24h"]["brier_kalshi_mid"], ORANGE)]
    for i, (lab, v, c) in enumerate(rows):
        a3.barh(i, v, color=c, height=0.6)
        a3.text(v + 0.0005, i, f"{v:.4f}", va="center", fontsize=10, color=INK)
    a3.set_yticks(range(len(rows)))
    a3.set_yticklabels([r[0] for r in rows], color=INK)
    a3.invert_yaxis()
    a3.set_xlim(0.19, 0.224)
    a3.set_xlabel("Leg Brier score (lower = better)")
    a3.grid(axis="y", visible=False)
    a3.set_title(f"Bars to beat (A: Jan–May 2026, {b['A_test']['matches']:,} matches;\n"
                 f"B: Jul–Sep 2026, {b['B_24h']['legs']:,} legs)", fontsize=10.5)

    fig.suptitle("v2 Phase 0: the dataset — 55k matches to learn from, 457 Kalshi games to test on",
                 x=0.01, ha="left", fontsize=14, fontweight="bold", color=INK)
    fig.tight_layout()
    save(fig, "17_phase0_dataset.png")


if __name__ == "__main__":
    main()
