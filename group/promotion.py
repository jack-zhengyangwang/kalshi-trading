"""
promotion.py — selector for the live promotion patch-panel.

Reads the paper arena's per-team state (arena_state/team_<cat>_<suffix>.json) and
picks the best ELIGIBLE team per category. The gate is the whole point: raw P&L
is noisy (a single lucky scoreline can mint a "+134"), so a team must clear a
sample + calibration bar before it can be auto-plugged into the real wallet.

Read-only. No orders, no state mutation — just ranking.
"""
import glob
import json
import math
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARENA = os.path.join(BASE_DIR, "arena_state")

MIN_PROMOTE_N = 20      # min resolved bets before a team is eligible
MAX_BRIER = 0.25        # calibration bar (pooled Brier of its bets)


def _records():
    """One record per arena team, computed from its persisted ledger + account."""
    out = []
    for path in sorted(glob.glob(os.path.join(ARENA, "team_*.json"))):
        try:
            st = json.load(open(path))
        except Exception:
            continue
        cat = st.get("cat")
        name = os.path.basename(path)[len("team_"):-len(".json")]
        acct = st.get("account", {})
        bets = (st.get("ledger") or {}).get("bets", [])
        closed = [b for b in bets if b.get("outcome") is not None]
        cost = sum((b.get("dollars") or 0.0) for b in closed)
        pnl = float(acct.get("realized_pnl", 0.0))
        roi = (pnl / cost) if cost else 0.0
        brier = (sum((b["p_fair"] - b["outcome"]) ** 2 for b in closed
                     if b.get("p_fair") is not None) / len(closed)) if closed else None
        out.append({"name": name, "cat": cat, "pnl": round(pnl, 2),
                    "n_closed": len(closed), "cost": round(cost, 2),
                    "roi": round(roi, 4), "brier": (round(brier, 4) if brier is not None else None)})
    return out


def _eligible(r, min_n=MIN_PROMOTE_N):
    return (r["n_closed"] >= min_n and r["pnl"] > 0
            and (r["brier"] is None or r["brier"] <= MAX_BRIER))


def _score(r):
    """Risk-adjusted rank: realized P&L per sqrt-dollar-staked (a crude Sharpe),
    so a big number off a tiny, lucky stake doesn't outrank steady edge."""
    return r["pnl"] / math.sqrt(max(1.0, r["cost"]))


def category_table(min_n=MIN_PROMOTE_N):
    """{cat: [records sorted best→worst]} — every team, with eligibility flag."""
    table = {}
    for r in _records():
        r = {**r, "eligible": _eligible(r, min_n), "score": round(_score(r), 4)}
        table.setdefault(r["cat"], []).append(r)
    for cat in table:
        table[cat].sort(key=lambda x: (x["eligible"], x["score"]), reverse=True)
    return table


def select_per_category(min_n=MIN_PROMOTE_N):
    """{cat: best_eligible_team_name | None}. None = slot stays dark (nothing has
    earned promotion yet)."""
    out = {}
    for cat, rows in category_table(min_n).items():
        elig = [r for r in rows if r["eligible"]]
        out[cat] = elig[0]["name"] if elig else None
    return out


if __name__ == "__main__":
    tbl = category_table()
    sel = select_per_category()
    for cat, rows in tbl.items():
        print(f"[{cat}] auto-pick: {sel[cat] or '— (none eligible)'}")
        for r in rows:
            flag = "✓" if r["eligible"] else "·"
            print(f"   {flag} {r['name']:<18} pnl ${r['pnl']:+7.2f}  n={r['n_closed']:<3} "
                  f"roi {r['roi']:+.1%}  brier {r['brier']}  score {r['score']}")
