"""v2 Phase 1: baseline prediction models and the two gates.

    python -m wc.research.dataset     # build the tables first
    python -m wc.research.models

The protocol (models, tuning grid, gates, 99% bar) is fixed in
docs/NEXT_CHAPTER_v2.md, "Phase 1 protocol", and was committed before this ran.

    Gate 1  more accurate than the market?   leg Brier(model) − leg Brier(market),
                                             99% interval entirely below 0
    Gate 2  adds information to the market?  won ~ 1 + logit(market) + logit(model),
                                             model coefficient's 99% interval above 0
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from wc import paths
from wc.research.dataset import FEATURES, OUT_DB, RESULTS

ORDER = ["home", "draw", "away"]
PREDS_DB = os.path.join(paths.ROOT, "data", "research", "v2_phase1.db")
EPS = 1e-6


# ── scores ────────────────────────────────────────────────────────────────────

def _onehot(y):
    return np.array([[1.0 if t == o else 0.0 for o in ORDER] for t in y])


def logloss3(P, y):
    idx = [ORDER.index(t) for t in y]
    return float(-np.mean(np.log(np.clip(np.asarray(P)[np.arange(len(idx)), idx], 1e-12, 1))))


def leg_brier3(P, y):
    return float(np.mean((np.asarray(P) - _onehot(y)) ** 2))


# ── models ────────────────────────────────────────────────────────────────────

class BaseRate:
    """M0: the league's home / draw rates over the previous 365 days."""

    def __init__(self, fallback=(0.45, 0.27, 0.28)):
        self.fallback = fallback

    def fit(self, X, y):
        self.fallback = tuple(_onehot(y).mean(axis=0))
        return self

    def predict(self, X):
        h, d = np.asarray(X["lg_home"], float), np.asarray(X["lg_draw"], float)
        bad = np.isnan(h) | np.isnan(d)
        h = np.where(bad, self.fallback[0], h)
        d = np.where(bad, self.fallback[1], d)
        P = np.clip(np.column_stack([h, d, 1 - h - d]), 0.001, None)
        return P / P.sum(axis=1, keepdims=True)


class SkModel:
    """M1–M3: a scikit-learn classifier on named features, returning
    probabilities in ORDER (home, draw, away) whatever order sklearn sorts."""

    def __init__(self, kind, features, **params):
        self.kind, self.features, self.params = kind, list(features), params

    def _X(self, X):
        return np.column_stack([np.asarray(X[f], float) for f in self.features])

    def fit(self, X, y):
        if self.kind == "lr":
            self.m = make_pipeline(SimpleImputer(strategy="median", add_indicator=True),
                                   StandardScaler(),
                                   LogisticRegression(C=self.params.get("C", 1.0), max_iter=3000))
        else:
            self.m = HistGradientBoostingClassifier(
                learning_rate=self.params.get("learning_rate", 0.1),
                max_leaf_nodes=self.params.get("max_leaf_nodes", 31),
                max_iter=300, random_state=0)
        self.m.fit(self._X(X), np.asarray(y))
        return self

    def predict(self, X):
        P = self.m.predict_proba(self._X(X))
        cls = list(self.m.classes_)
        return np.column_stack([P[:, cls.index(o)] for o in ORDER])


# ── gates ─────────────────────────────────────────────────────────────────────

def _groups(groups):
    by = {}
    for i, g in enumerate(groups):
        by.setdefault(g, []).append(i)
    return list(by.values())


def gate1(model_p, market_p, y, groups, draws=2000, seed=0):
    """Leg Brier of model minus market, 99% interval by bootstrapping games."""
    m, k, y = (np.asarray(a, float) for a in (model_p, market_p, y))
    d = (m - y) ** 2 - (k - y) ** 2
    idx_groups, rng = _groups(groups), random.Random(seed)
    boots = []
    for _ in range(draws):
        idx = [i for g in rng.choices(idx_groups, k=len(idx_groups)) for i in g]
        boots.append(float(d[idx].mean()))
    lo, hi = np.percentile(boots, [0.5, 99.5])
    return {"diff": float(d.mean()), "ci99": [float(lo), float(hi)], "passes": bool(hi < 0),
            "brier_model": float(((m - y) ** 2).mean()),
            "brier_market": float(((k - y) ** 2).mean())}


def _logit(p):
    p = np.clip(np.asarray(p, float), 0.001, 0.999)
    return np.log(p / (1 - p))


def _fit_logistic(X, y):
    b = np.zeros(X.shape[1])
    for _ in range(60):
        p = 1 / (1 + np.exp(-X @ b))
        w = p * (1 - p)
        b += np.linalg.solve(X.T @ (X * w[:, None]) + EPS * np.eye(len(b)), X.T @ (y - p))
    return b


def gate2(market_p, model_p, y, groups, draws=1000, seed=0):
    """won ~ 1 + logit(market) + logit(model): does the model add information
    once the market price is known? 99% interval by bootstrapping games."""
    y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(y)), _logit(market_p), _logit(model_p)])
    b = _fit_logistic(X, y)
    idx_groups, rng = _groups(groups), random.Random(seed)
    boots = []
    for _ in range(draws):
        idx = [i for g in rng.choices(idx_groups, k=len(idx_groups)) for i in g]
        boots.append(_fit_logistic(X[idx], y[idx]))
    lo, hi = np.percentile(np.array(boots), [0.5, 99.5], axis=0)
    return {"coef_market": float(b[1]), "coef_model": float(b[2]),
            "ci99_market": [float(lo[1]), float(hi[1])],
            "ci99_model": [float(lo[2]), float(hi[2])], "passes": bool(lo[2] > 0)}


# ── data ──────────────────────────────────────────────────────────────────────

def _cols(rows, names):
    return {n: np.array([np.nan if r[i] is None else r[i] for r in rows], float)
            for i, n in enumerate(names)}


def load_A(con, splits):
    marks = ",".join("?" * len(splits))
    rows = con.execute(f"SELECT match_id, target, p_book_h, p_book_d, p_book_a, "
                       f"{', '.join(FEATURES)} FROM ds_matches WHERE split IN ({marks})",
                       splits).fetchall()
    X = _cols([r[5:] for r in rows], FEATURES)
    book = np.array([[np.nan if v is None else v for v in r[2:5]] for r in rows], float)
    return X, np.array([r[1] for r in rows]), [r[0] for r in rows], book


def load_B(con, entry_h):
    rows = con.execute(f"SELECT event_ticker, side, mid, won, {', '.join(FEATURES)} "
                       "FROM ds_kalshi WHERE entry_h=?", (entry_h,)).fetchall()
    X = _cols([r[4:] for r in rows], FEATURES)
    return X, [r[1] for r in rows], np.array([r[2] for r in rows], float), \
        np.array([r[3] for r in rows], float), [r[0] for r in rows]


# ── run ───────────────────────────────────────────────────────────────────────

GRIDS = {
    "M2_lr_all": [{"C": c} for c in (0.01, 0.1, 1.0)],
    "M3_hgb_all": [{"learning_rate": lr, "max_leaf_nodes": n}
                   for lr in (0.03, 0.1) for n in (15, 31)],
}
MODELS = ("M0_base_rate", "M1_elo", "M2_lr_all", "M3_hgb_all")


def make(name, params=None):
    params = params or {}
    if name == "M0_base_rate":
        return BaseRate()
    if name == "M1_elo":
        return SkModel("lr", ["elo_diff", "lg_home", "lg_draw"], C=1.0)
    if name == "M2_lr_all":
        return SkModel("lr", FEATURES, **params)
    return SkModel("hgb", FEATURES, **params)


def run(con):
    Xtr, ytr, _, _ = load_A(con, ["train"])
    Xtu, ytu, _, _ = load_A(con, ["tune"])
    Xall, yall, _, _ = load_A(con, ["train", "tune"])
    Xte, yte, _, book = load_A(con, ["test"])
    has_book = ~np.isnan(book).any(axis=1)

    out = {"n_train": len(ytr), "n_tune": len(ytu), "n_test_with_pinnacle": int(has_book.sum())}
    preds = []
    for name in MODELS:
        chosen, tune_scores = None, {}
        for params in GRIDS.get(name, [None]):
            ll = logloss3(make(name, params).fit(Xtr, ytr).predict(Xtu), ytu)
            tune_scores[json.dumps(params)] = ll
            if chosen is None or ll < tune_scores[json.dumps(chosen)]:
                chosen = params
        model = make(name, chosen).fit(Xall, yall)

        # Table A test vs Pinnacle closing
        P = model.predict({k: v[has_book] for k, v in Xte.items()})
        yA = yte[has_book]
        legs_m, legs_k, legs_y, legs_g = [], [], [], []
        for i, (p, b, t) in enumerate(zip(P, book[has_book], yA)):
            for j, side in enumerate(ORDER):
                legs_m.append(p[j])
                legs_k.append(b[j])
                legs_y.append(float(t == side))
                legs_g.append(f"A{i}")
        res = {"params": chosen, "tune_logloss": tune_scores,
               "A_test": {"logloss_model": logloss3(P, yA),
                          "logloss_pinnacle": logloss3(book[has_book], yA),
                          "gate1": gate1(legs_m, legs_k, legs_y, legs_g),
                          "gate2": gate2(legs_k, legs_m, legs_y, legs_g)}}
        preds += [("A", g, name, float(m), float(k), y)
                  for m, k, y, g in zip(legs_m, legs_k, legs_y, legs_g)]

        # Table B vs Kalshi
        for h in (24, 6, 1):
            XB, sides, mid, won, evs = load_B(con, h)
            PB = model.predict(XB)
            pm = np.array([PB[i, ORDER.index(s)] for i, s in enumerate(sides)])
            res[f"B_{h}h"] = {"legs": len(won), "gate1": gate1(pm, mid, won, evs),
                              "gate2": gate2(mid, pm, won, evs)}
            if h == 24:
                preds += [("B24", g, name, float(m), float(k), float(y))
                          for m, k, y, g in zip(pm, mid, won, evs)]
        out[name] = res
        g1, g2 = res["A_test"]["gate1"], res["A_test"]["gate2"]
        b1, b2 = res["B_24h"]["gate1"], res["B_24h"]["gate2"]
        print(f"{name:13} {chosen}\n  A test: brier {g1['brier_model']:.4f} vs Pinnacle "
              f"{g1['brier_market']:.4f} G1={g1['passes']}  G2 coef {g2['coef_model']:+.2f} "
              f"99% [{g2['ci99_model'][0]:+.2f},{g2['ci99_model'][1]:+.2f}] G2={g2['passes']}\n"
              f"  B 24h : brier {b1['brier_model']:.4f} vs Kalshi {b1['brier_market']:.4f} "
              f"G1={b1['passes']}  G2 coef {b2['coef_model']:+.2f} "
              f"99% [{b2['ci99_model'][0]:+.2f},{b2['ci99_model'][1]:+.2f}] G2={b2['passes']}")
    return out, preds


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=OUT_DB)
    a = ap.parse_args()
    out, preds = run(sqlite3.connect(a.db))
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "v2_phase1.json"), "w") as f:
        json.dump(out, f, indent=1)
    if os.path.exists(PREDS_DB):
        os.remove(PREDS_DB)
    pc = sqlite3.connect(PREDS_DB)
    pc.execute("CREATE TABLE preds (tbl TEXT, gid TEXT, model TEXT, p REAL, market REAL, y REAL)")
    pc.executemany("INSERT INTO preds VALUES (?,?,?,?,?,?)", preds)
    pc.commit()


if __name__ == "__main__":
    main()
