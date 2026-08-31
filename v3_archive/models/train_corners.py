"""
train_corners.py — fit the corner-generating model (Arena v2, Phase 0).

Input : models/corners_dataset.csv  (built by fetch_corners.py)
Output: models/corners_model.json   (consumed by brain_v2's Game-Props corner legs)

The structure mirrors the GOALS model in group/markets.py so corner legs price
the same way goals do:

    home corners ~ Poisson(mu_home),  away corners ~ Poisson(mu_away)

with a per-match expected TOTAL (anchored to market at inference, like goals) split
into the two sides by an ELO supremacy through a tanh:

    home_share = 0.5 * (1 + tanh(elo_diff / supremacy_scale_corners))

This file estimates, from ~79k club matches:
  • base corner rates (home / away / total) and the home-advantage ratio
  • over-dispersion (var/mean of total corners) -> Poisson vs Negative-Binomial
  • supremacy_scale_corners IN ELO UNITS, by joining club-ELO and regressing the
    realised home corner share on the ELO difference (directly reusable at the
    World Cup, which is priced off ELO)
  • per-club attack/defense corner factors (for future club-market pricing)
  • a held-out calibration check of P(total corners > line)

A full bivariate-Poisson / GBT upgrade comes once a per-match feature join exists;
this is the transferable structural baseline.

Usage:
    python3 models/train_corners.py
"""
import json
import math
import os
import re

import numpy as np
import pandas as pd

HERE = os.path.dirname(__file__)
DATA = os.path.join(HERE, "corners_dataset.csv")
ELO = os.path.join(HERE, "club_elo.json")
OUT = os.path.join(HERE, "corners_model.json")


def _norm(name: str) -> str:
    """Loose club-name key for fuzzy ELO joins."""
    s = str(name).lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    for junk in (" fc", " cf", " afc", " sc", " ac", " calcio", " 1900", " 04"):
        s = s.replace(junk, "")
    return s.strip()


def poisson_sf(lmbda: float, line: float) -> float:
    """P(X > line) for X ~ Poisson(lmbda), fractional line (no push)."""
    thr = math.floor(line) + 1
    # P(X >= thr) = 1 - CDF(thr-1)
    cdf = 0.0
    term = math.exp(-lmbda)
    for k in range(thr):
        if k > 0:
            term *= lmbda / k
        cdf += term
    return max(0.0, 1.0 - cdf)


def nb_params(mean: float, var: float):
    """Negative-Binomial (r, p) matched to a mean/variance. var > mean required.
    Parameterised so E[X]=mean, Var[X]=mean + mean^2/r."""
    r = mean * mean / (var - mean)
    p = r / (r + mean)            # P(success); X = number of failures
    return r, p


def nb_sf(r: float, p: float, line: float) -> float:
    """P(X > line) for X ~ NegBin(r, p), fractional line (no push)."""
    thr = math.floor(line) + 1
    # pmf(k) = C(k+r-1, k) p^r (1-p)^k ; iterate the recurrence
    cdf = 0.0
    term = p ** r                 # pmf(0)
    for k in range(thr):
        if k > 0:
            term *= (k + r - 1) / k * (1 - p)
        cdf += term
    return max(0.0, 1.0 - cdf)


def main():
    df = pd.read_csv(DATA, parse_dates=["date"])
    df = df.dropna(subset=["hc", "ac", "home", "away"]).copy()
    df["hc"] = df["hc"].astype(float)
    df["ac"] = df["ac"].astype(float)
    df["total"] = df["hc"] + df["ac"]
    n = len(df)
    print(f"Loaded {n:,} matches, {df['div'].nunique()} leagues")

    # ── recency weighting ─────────────────────────────────────────────────────
    # Corner rates drift DOWN over seasons (held-out Poisson/NB ran ~3-4pp hot on
    # 'over'), so weight recent seasons more when fitting the baseline + dispersion.
    DECAY = 0.85                                   # ~per-season; half-life ~4 seasons
    syear = df["season"].astype(str).str[:2].astype(int)
    w = DECAY ** (syear.max() - syear)

    def wmean(x):
        return float((x * w).sum() / w.sum())

    def wvar(x):
        m = wmean(x)
        return float((w * (x - m) ** 2).sum() / w.sum())

    # ── base rates + dispersion (recency-weighted) ────────────────────────────
    base_home = wmean(df["hc"])
    base_away = wmean(df["ac"])
    base_total = wmean(df["total"])
    tot_var = wvar(df["total"])
    disp = tot_var / base_total
    print(f"Base corners  home {base_home:.3f} / away {base_away:.3f} / "
          f"total {base_total:.3f}")
    print(f"Total corners var/mean = {disp:.3f}  "
          f"({'over-dispersed -> Negative-Binomial advised' if disp > 1.15 else 'Poisson OK'})")

    # ── per-club attack/defense corner factors (for club markets later) ────────
    league_for = df.groupby("div")["hc"].mean() + df.groupby("div")["ac"].mean()
    league_avg = (df["total"].mean()) / 2.0  # avg corners won per team-match
    factors = {}
    # home perspective: team=home, won=hc, conceded=ac ; away: team=away won=ac
    rows = pd.concat([
        df.rename(columns={"home": "team", "hc": "won", "ac": "conceded"})[
            ["div", "team", "won", "conceded"]],
        df.rename(columns={"away": "team", "ac": "won", "hc": "conceded"})[
            ["div", "team", "won", "conceded"]],
    ], ignore_index=True)
    grp = rows.groupby(["div", "team"]).agg(
        n=("won", "size"), won=("won", "mean"), conceded=("conceded", "mean"))
    grp = grp[grp["n"] >= 30]  # need a reasonable sample
    for (div, team), r in grp.iterrows():
        factors[f"{div}:{team}"] = {
            "n": int(r["n"]),
            "attack": round(float(r["won"]) / league_avg, 4),
            "defense": round(float(r["conceded"]) / league_avg, 4),
        }
    print(f"Per-club factors: {len(factors)} clubs (>=30 matches)")

    # ── ELO-unit strength -> corner-share calibration ─────────────────────────
    elo = json.load(open(ELO))
    elo_norm = {_norm(k): v for k, v in elo.items()}

    def lookup(team):
        return elo_norm.get(_norm(team))

    df["elo_h"] = df["home"].map(lookup)
    df["elo_a"] = df["away"].map(lookup)
    j = df.dropna(subset=["elo_h", "elo_a"]).copy()
    j = j[j["total"] > 0]
    cov = len(j) / n
    print(f"ELO join coverage: {len(j):,}/{n:,} ({cov:.1%})")

    supremacy_scale = None
    share_fit = None
    if len(j) >= 1000:
        j["elo_diff"] = j["elo_h"] - j["elo_a"]
        j["share"] = j["hc"] / j["total"]
        # slope of corner share wrt elo_diff (linear, robust)
        b, a = np.polyfit(j["elo_diff"], j["share"], 1)
        # match the tanh slope at 0: d/dx[0.5(1+tanh(x/s))]|0 = 1/(2s)  ->  s = 1/(2b)
        supremacy_scale = float(1.0 / (2.0 * b)) if b > 0 else None
        ss_res = float(((j["share"] - (a + b * j["elo_diff"])) ** 2).sum())
        ss_tot = float(((j["share"] - j["share"].mean()) ** 2).sum())
        r2 = 1 - ss_res / ss_tot
        share_fit = {"intercept": float(a), "slope_per_elo": float(b),
                     "r2": round(r2, 4), "n": int(len(j))}
        print(f"Share fit: slope {b:.3e}/ELO, intercept {a:.3f}, R^2 {r2:.4f}")
        print(f"supremacy_scale_corners = {supremacy_scale:.1f} ELO  "
              f"(goals model uses 600)")

    # ── held-out calibration of P(total > line) ───────────────────────────────
    df_sorted = df.sort_values("date")
    cut = int(len(df_sorted) * 0.8)
    train, test = df_sorted.iloc[:cut], df_sorted.iloc[cut:]
    tsy = train["season"].astype(str).str[:2].astype(int)
    tw = DECAY ** (tsy.max() - tsy)
    lam_unw = float(train["total"].mean())
    lam = float((train["total"] * tw).sum() / tw.sum())     # recency-weighted
    tvar = float((tw * (train["total"] - lam) ** 2).sum() / tw.sum())
    r, p = nb_params(lam, tvar)
    calib = {}
    for line in (8.5, 9.5, 10.5, 11.5):
        emp = float((test["total"] > line).mean())
        calib[str(line)] = {"poisson_unweighted": round(poisson_sf(lam_unw, line), 4),
                            "nb_recency": round(nb_sf(r, p, line), 4),
                            "empirical": round(emp, 4)}
    print(f"Held-out P(total > line)  [old Poisson(unweighted {lam_unw:.2f}) | "
          f"new NB(recency {lam:.2f}) | empirical]:")
    for k, v in calib.items():
        print(f"  over {k}: {v['poisson_unweighted']:.3f} | {v['nb_recency']:.3f} "
              f"| {v['empirical']:.3f}")

    # ── save ──────────────────────────────────────────────────────────────────
    model = {
        "_doc": "Corner-generating model for Arena v2 brain_v2 Game-Props legs. "
                "Mirrors group/markets.py goals: home/away corners ~ Poisson, "
                "per-match total anchored to market, split by ELO via tanh.",
        "n_matches": n,
        "leagues": sorted(df["div"].unique().tolist()),
        "date_range": [str(df["date"].min().date()), str(df["date"].max().date())],
        "base_corners": {"home": round(base_home, 4), "away": round(base_away, 4),
                         "total": round(base_total, 4)},
        "home_advantage_ratio": round(base_home / base_away, 4),
        "dispersion": {"total_mean": round(base_total, 4),
                       "total_var": round(tot_var, 4), "ratio": round(disp, 4),
                       "use_negative_binomial": disp > 1.15,
                       "nb_r": round(float(nb_params(base_total, tot_var)[0]), 4),
                       "nb_p": round(float(nb_params(base_total, tot_var)[1]), 4)},
        "supremacy_scale_corners": (round(supremacy_scale, 1)
                                    if supremacy_scale else None),
        "share_fit": share_fit,
        "elo_join_coverage": round(cov, 4),
        "calibration_total_over": calib,
        "team_factors": factors,
    }
    with open(OUT, "w") as f:
        json.dump(model, f, indent=2)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
