"""v2 Phase 2: is the market weaker in some league tiers, or earlier?

    python -m wc.research.phase2 --smoke    # laptop: tune split only, test sets untouched
    python -m wc.research.phase2            # the real run (on the droplet, full data)

Protocol fixed in docs/NEXT_CHAPTER_v2.md ("Phase 2 protocol") before any run:
M2 with C = 0.01 frozen; Gate 2 per league tier (Table A test vs Pinnacle) and
per tier × entry time (Table B vs Kalshi); groups under 150 legs are reported,
not judged; a tier counts as a signal only if it passes at ≥ 2 entry times.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3

import numpy as np

from wc.research.dataset import FEATURES, OUT_DB, RESULTS
from wc.research.models import ORDER, SkModel, gate1, gate2

C_FROZEN = 0.01
MIN_LEGS = 150
TOP = {"EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "UCL"}
LOWER = {"Championship", "Bundesliga2", "BrasileiroB", "BrasileiroC", "ArgNacionalB",
         "SerieB", "SerieC", "LaLiga2", "Ligue2"}
_LOWER_NAME = re.compile(r"(2|B|C)$")        # 'Ligue2', 'SerieB', 'BrasileiroC', ...


def tier(league):
    if league in TOP:
        return "T1"
    if league in LOWER or (league and _LOWER_NAME.search(league)):
        return "T3"
    return "T2"


def group_gates(model_p, market_p, y, games, groups, min_legs=MIN_LEGS, draws=1000):
    """Gate 1 and Gate 2 for each group; groups under `min_legs` are
    reported but not judged."""
    model_p, market_p, y = (np.asarray(a, float) for a in (model_p, market_p, y))
    out = {}
    for g in sorted(set(groups)):
        idx = [i for i, x in enumerate(groups) if x == g]
        sub_games = [games[i] for i in idx]
        judged = len(idx) >= min_legs
        out[g] = {"legs": len(idx), "games": len(set(sub_games)), "judged": judged,
                  "gate1": gate1(model_p[idx], market_p[idx], y[idx], sub_games, draws=draws)
                  if judged else None,
                  "gate2": gate2(market_p[idx], model_p[idx], y[idx], sub_games, draws=draws)
                  if judged else None}
    return out


def signals(results):
    """Tiers whose Table B Gate 2 passes at two or more entry times."""
    passes = {}
    for key, r in results.items():
        parts = key.split("|")
        if parts[0] == "B" and r["judged"] and r["gate2"]["passes"]:
            passes[parts[1]] = passes.get(parts[1], 0) + 1
    return sorted(t for t, n in passes.items() if n >= 2)


def _X(rows, offset):
    return {f: np.array([np.nan if r[offset + i] is None else r[offset + i] for r in rows], float)
            for i, f in enumerate(FEATURES)}


def run(con, smoke=False):
    feats = ", ".join(FEATURES)
    fit_splits = ("train",) if smoke else ("train", "tune")
    eval_split = "tune" if smoke else "test"
    marks = ",".join("?" * len(fit_splits))
    fit = con.execute(f"SELECT target, {feats} FROM ds_matches WHERE split IN ({marks})",
                      fit_splits).fetchall()
    model = SkModel("lr", FEATURES, C=C_FROZEN).fit(_X(fit, 1), np.array([r[0] for r in fit]))

    res = {}
    rows = con.execute(f"SELECT match_id, league, target, p_book_h, p_book_d, p_book_a, {feats} "
                       "FROM ds_matches WHERE split=? AND p_book_h IS NOT NULL",
                       (eval_split,)).fetchall()
    P = model.predict(_X(rows, 6))
    m, k, y, g, t = [], [], [], [], []
    for i, r in enumerate(rows):
        for j, side in enumerate(ORDER):
            m.append(P[i, j])
            k.append(r[3 + j])
            y.append(float(r[2] == side))
            g.append(r[0])
            t.append(tier(r[1]))
    for grp, v in group_gates(m, k, y, g, t).items():
        res[f"A|{grp}"] = v

    if not smoke:
        for h in (24, 6, 1):
            rows = con.execute(f"SELECT event_ticker, league, side, mid, won, {feats} "
                               "FROM ds_kalshi WHERE entry_h=?", (h,)).fetchall()
            if not rows:
                continue
            PB = model.predict(_X(rows, 5))
            pm = [PB[i, ORDER.index(r[2])] for i, r in enumerate(rows)]
            for grp, v in group_gates(pm, [r[3] for r in rows], [r[4] for r in rows],
                                      [r[0] for r in rows], [tier(r[1]) for r in rows]).items():
                res[f"B|{grp}|{h}"] = v
    return {"smoke": smoke, "eval_split": eval_split, "C": C_FROZEN, "groups": res,
            "signals": signals(res)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=OUT_DB)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    out = run(sqlite3.connect(a.db), smoke=a.smoke)
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "v2_phase2_smoke.json" if a.smoke else "v2_phase2.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    for key, r in out["groups"].items():
        g2 = r["gate2"]
        print(f"{key:10} legs={r['legs']:6} games={r['games']:5} "
              + (f"G2 coef {g2['coef_model']:+.2f} 99% [{g2['ci99_model'][0]:+.2f},"
                 f"{g2['ci99_model'][1]:+.2f}] pass={g2['passes']}" if g2 else "not judged"))
    print("signals:", out["signals"], "| wrote", path)


if __name__ == "__main__":
    main()
