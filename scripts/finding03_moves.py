"""Charts for docs/findings/03_PRICE_MOVES.md: how prices move from t to kickoff.

    venv/bin/python -m wc.research.kickoff          # build the kickoffs table first
    venv/bin/python scripts/finding03_moves.py [data/market_history.db]

Every winner market whose game has an ESPN kickoff (status agree / disagree /
espn_only). Outcome is NOT needed: this is about the price path before the
game, so unsettled markets count too. Price = mid of the last hourly bar at or
before the moment; the kickoff price is the last bar within the hour before
kickoff. Writes PNGs to docs/findings/img/. Needs matplotlib.
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
from findings_charts import BLUE, INK, INK_2, MUTED, ORANGE, SURFACE, save  # noqa: E402
from finding02_midrange import fee  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HORIZONS = (48, 24, 12, 6, 3, 1)
MAX_SPREAD_C = 10
ESPN_BACKED = ("agree", "disagree", "espn_only")


def bar_at(con, ticker, ts, window_s):
    r = con.execute(
        "SELECT yes_bid, yes_ask FROM backfill_candles WHERE ticker=? AND interval_min=60 "
        "AND ts<=? AND ts>? AND yes_bid>0 AND yes_ask>0 ORDER BY ts DESC LIMIT 1",
        (ticker, ts, ts - window_s)).fetchone()
    if not r or r[1] - r[0] > MAX_SPREAD_C:
        return None
    return {"mid": (r[0] + r[1]) / 200, "bid": r[0] / 100, "ask": r[1] / 100}


def moves(con):
    """{horizon: [row]} with the price at kickoff-h and at kickoff."""
    q = ("SELECT m.ticker, m.event_ticker, m.sub_title, k.kickoff_ts FROM markets m "
         "JOIN kickoffs k USING(event_ticker) WHERE m.series LIKE '%GAME' AND k.status IN "
         f"({','.join('?' * len(ESPN_BACKED))})")
    out = defaultdict(list)
    for t, ev, sub, ko in con.execute(q, ESPN_BACKED).fetchall():
        k = bar_at(con, t, ko, 3600)
        if not k:
            continue
        for h in HORIZONS:
            b = bar_at(con, t, ko - h * 3600, 6 * 3600)
            if not b:
                continue
            # round trip: buy at the ask at t, sell at the bid at kickoff, two fees
            cost = (b["ask"] - b["mid"]) + (k["mid"] - k["bid"]) + fee(b["ask"]) + fee(k["bid"])
            out[h].append({"ev": ev, "tie": sub == "Tie", "p0": b["mid"], "p1": k["mid"],
                           "d": k["mid"] - b["mid"], "cost": cost})
    return out


def size_chart(mv):
    """How far prices move from 24h before kickoff to kickoff, against the cost
    of a round trip."""
    rows = mv[24]
    d = [100 * r["d"] for r in rows]
    cost = sorted(100 * r["cost"] for r in rows)[len(rows) // 2]
    beyond = sum(1 for r in rows if abs(r["d"]) > r["cost"]) / len(rows)
    flat = sum(1 for x in d if abs(x) < 0.5) / len(d)

    fig, ax = plt.subplots(figsize=(10, 5.4))
    ax.axvspan(-cost, cost, color="#f0efec", zorder=0)
    ax.hist(d, bins=range(-25, 26), color=BLUE, edgecolor=SURFACE, linewidth=1.5, zorder=2)
    ax.text(cost + 1, ax.get_ylim()[1] * 0.93,
            f"shaded: round-trip cost ≈ ±{cost:.1f}¢\n{100 * flat:.0f}% of markets didn't move",
            ha="left", va="top", color=INK_2, fontsize=10)
    ax.set_xlim(-25, 25)
    ax.set_xlabel("Price at kickoff minus price 24h before (¢)")
    ax.set_ylabel("Markets")
    ax.grid(axis="x", visible=False)
    ax.set_title(f"{100 * beyond:.0f}% of markets move further than it costs to trade them")
    fig.text(0.125, 0.0, f"{len(rows):,} markets with an ESPN kickoff. Moves beyond ±25¢ "
             "not drawn.", fontsize=8.5, color=MUTED)
    save(fig, "07_move_size.png")


def prize_chart(mv):
    """Typical move vs typical round-trip cost at each horizon."""
    hs = [h for h in HORIZONS if mv[h]]

    def median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2]

    move = [100 * median([abs(r["d"]) for r in mv[h]]) for h in hs]
    cost = [100 * median([r["cost"] for r in mv[h]]) for h in hs]

    fig, ax = plt.subplots(figsize=(10, 5.2))
    x = range(len(hs))
    ax.bar([i - 0.19 for i in x], move, 0.36, color=BLUE, edgecolor=SURFACE, linewidth=2,
           label="Typical move to kickoff (median |move|)")
    ax.bar([i + 0.19 for i in x], cost, 0.36, color=ORANGE, edgecolor=SURFACE, linewidth=2,
           label="Typical cost to trade it (buy ask, sell bid, 2 fees)")
    for i, (m, c) in enumerate(zip(move, cost)):
        ax.text(i - 0.19, m + 0.1, f"{m:.1f}¢", ha="center", va="bottom", fontsize=9.5, color=INK)
        ax.text(i + 0.19, c + 0.1, f"{c:.1f}¢", ha="center", va="bottom", fontsize=9.5, color=INK)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{h}h before\nn={len(mv[h]):,}" for h in hs], color=INK)
    ax.set_ylabel("Cents per contract")
    ax.set_ylim(0, max(cost) * 1.35)
    ax.grid(axis="x", visible=False)
    ax.set_title("Prices move ~1¢ before kickoff; trading the move costs ~5¢")
    ax.legend(loc="upper right", fontsize=9.5, labelcolor=INK)
    save(fig, "08_move_vs_cost.png")


def direction_chart(mv):
    """Which way prices drift, by starting price. Team legs and draw legs
    separately. Interval: 95%, bootstrapped by game."""
    fig, ax = plt.subplots(figsize=(10, 5.4))
    ax.axhline(0, color=INK_2, lw=1.2, ls="--")
    random.seed(0)
    for label, color, keep, off in (("Team to win", BLUE, lambda r: not r["tie"], -0.6),
                                    ("Draw", ORANGE, lambda r: r["tie"], 0.6)):
        xs, ys, lo_, hi_ = [], [], [], []
        for lo in range(5, 95, 10):
            b = [r for r in mv[24] if keep(r) and lo / 100 <= r["p0"] < (lo + 10) / 100]
            if len(b) < 25:
                continue
            ev = defaultdict(list)
            for r in b:
                ev[r["ev"]].append(r["d"])
            keys = list(ev)
            boots = sorted(sum(v for k in random.choices(keys, k=len(keys)) for v in ev[k])
                           / len(b) for _ in range(1000))
            xs.append(lo + 5 + off)
            ys.append(100 * sum(r["d"] for r in b) / len(b))
            lo_.append(100 * boots[25])
            hi_.append(100 * boots[975])
        ax.vlines(xs, lo_, hi_, color=color, lw=2, alpha=0.5)
        ax.plot(xs, ys, color=color, lw=2, marker="o", ms=8, mec=SURFACE, mew=1.5, label=label)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Price 24h before kickoff (¢)")
    ax.set_ylabel("Average move to kickoff (¢)")
    ax.set_title("Which way do prices drift before kickoff?")
    ax.legend(loc="upper right", fontsize=10, labelcolor=INK)
    fig.text(0.125, 0.0, "Bars: 95% interval, bootstrapped by game. Bins with fewer than 25 "
             "markets dropped.", fontsize=8.5, color=MUTED)
    save(fig, "09_direction_by_price.png")


def sources_chart(con):
    """Where each game's kickoff came from: ESPN (truth) vs Kalshi (check)."""
    labels = {"agree": "ESPN + Kalshi agree", "disagree": "Both found, disagree (ESPN kept)",
              "espn_only": "ESPN only", "kalshi_only": "Kalshi only (not used)",
              "missing": "Neither (not used)"}
    counts = dict(con.execute("SELECT status, COUNT(*) FROM kickoffs GROUP BY status"))
    keys = [k for k in labels if counts.get(k)]
    vals = [counts[k] for k in keys]
    colors = [BLUE if k in ESPN_BACKED else MUTED for k in keys]
    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.barh(range(len(keys)), vals, color=colors, edgecolor=SURFACE, linewidth=2, height=0.6)
    for i, v in enumerate(vals):
        ax.text(v + 15, i, f"{v:,}", va="center", fontsize=10, color=INK)
    ax.set_yticks(range(len(keys)))
    ax.set_yticklabels([labels[k] for k in keys], color=INK)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(vals) * 1.15)
    ax.set_xlabel("Games")
    used = sum(counts.get(k, 0) for k in ESPN_BACKED)
    ax.set_title(f"Kickoff times: ESPN found {used:,} of {sum(vals):,} games (blue = used)")
    save(fig, "10_kickoff_sources.png")


def summary(mv):
    for h in HORIZONS:
        rows = mv[h]
        if not rows:
            continue
        n = len(rows)
        up = sum(1 for r in rows if r["d"] > 0.005) / n
        dn = sum(1 for r in rows if r["d"] < -0.005) / n
        med = sorted(abs(r["d"]) for r in rows)[n // 2]
        sd = math.sqrt(sum(r["d"] ** 2 for r in rows) / n)
        print(f"{h:2}h n={n:5} up={up:.2f} down={dn:.2f} flat={1 - up - dn:.2f} "
              f"median|move|={100 * med:.1f}c sd={100 * sd:.1f}c "
              f"median cost={100 * sorted(r['cost'] for r in rows)[n // 2]:.1f}c")


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "market_history.db")
    con = sqlite3.connect(db)
    sources_chart(con)
    mv = moves(con)
    summary(mv)
    size_chart(mv)
    prize_chart(mv)
    direction_chart(mv)


if __name__ == "__main__":
    main()
