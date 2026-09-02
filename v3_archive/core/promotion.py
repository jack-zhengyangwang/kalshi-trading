"""
promotion_v2.py — selector for the Arena v2 promotion patch-panel.

Reads arena_v2_state/teams_<cat>.json and picks the best ELIGIBLE team per
category. The gate is the whole point: raw P&L is noisy (especially on the
replay seed), so a team must clear a sample + profitability bar before it can be
plugged into the real wallet.

Read-only. No orders, no state mutation — just ranking.
"""
import json
import os
from wc import paths

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = paths.STATE_V2

MIN_PROMOTE_N = 10       # min resolved bets before a team is eligible
MIN_PNL = 0.0            # must be net positive on its paper record


def _teams(cat):
    try:
        return json.load(open(os.path.join(STATE, f"teams_{cat}.json")))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def rank(cat):
    """All teams for a category, annotated with eligibility, best first."""
    out = []
    for t in _teams(cat):
        pnl = t["account"]["realized_pnl"]
        eligible = (t["n_closed"] >= MIN_PROMOTE_N and pnl > MIN_PNL
                    and t["fitness"] > 0)
        out.append({"lineage": t["lineage"], "generation": t.get("generation", 0),
                    "fitness": round(t["fitness"], 3), "pnl": round(pnl, 2),
                    "n_closed": t["n_closed"], "params": t["params"],
                    "eligible": eligible})
    out.sort(key=lambda x: x["fitness"], reverse=True)
    return out


def best(cat):
    """Best ELIGIBLE team for a category, or None if none qualify (safe default)."""
    for r in rank(cat):
        if r["eligible"]:
            return r
    return None


def top(cat, n):
    """Top n ELIGIBLE teams for a category (for ensemble promotion). May be < n."""
    return [r for r in rank(cat) if r["eligible"]][:n]


def table(cats=("winner", "game_lines", "game_props")):
    print(f"Promotion selector  (gate: >={MIN_PROMOTE_N} resolved bets, +P&L, +fitness)\n")
    for cat in cats:
        b = best(cat)
        print(f"=== {cat} ===")
        if b:
            print(f"  PROMOTE -> {b['lineage']} (gen{b['generation']})  "
                  f"fit={b['fitness']:+.2f} pnl=${b['pnl']:+.2f} n={b['n_closed']}")
        else:
            print("  (no eligible team yet — nothing would be promoted)")
        for r in rank(cat)[:4]:
            tag = "ELIGIBLE" if r["eligible"] else "—"
            print(f"     {r['lineage']:<16} fit={r['fitness']:+.2f} "
                  f"pnl=${r['pnl']:+.2f} n={r['n_closed']:<3} {tag}")
        print()


if __name__ == "__main__":
    table()
