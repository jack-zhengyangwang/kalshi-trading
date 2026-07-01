#!/usr/bin/env python3
"""
Train a multinomial logistic regression on historical WC match data.

Outcome classes (3-way moneyline, matching Kalshi "Winner?" markets):
    0 = away/second-listed team wins
    1 = draw (Tie market)
    2 = home/first-listed team wins

Features:
    elo_diff     = team1_elo - team2_elo   (sign → who is favored)
    abs_elo_diff = |elo_diff|              (lets the draw class peak at parity)

Symmetric augmentation mirrors every match (swap sides → elo_diff negates,
class 0<->2, draw unchanged) so the model is order-independent: at equal ELO,
P(home win) == P(away win) by construction.

Run: python3 models/train.py
Produces: models/model.pkl  (pickled Pipeline; predict_proba → [P0, P1, P2])
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_CSV = os.path.join(BASE_DIR, "wc_matches_1990_2022.csv")
MODEL_PKL = os.path.join(BASE_DIR, "model.pkl")

CLASSES = [0, 1, 2]  # away win, draw, home win
FEATURES = ["elo_diff", "abs_elo_diff"]


def _featurize(df):
    df = df.copy()
    df["elo_diff"] = df["team1_elo"] - df["team2_elo"]
    df["abs_elo_diff"] = df["elo_diff"].abs()
    return df


def load_data(csv_path):
    df = pd.read_csv(csv_path)
    df = df[df["outcome"].isin(CLASSES)].copy()
    df = _featurize(df)

    # Symmetric augmentation: swap home/away. elo_diff negates, abs unchanged,
    # class 0<->2, draw (1) stays.
    mirror = df.copy()
    mirror["elo_diff"] = -df["elo_diff"]
    mirror["outcome"] = df["outcome"].map({0: 2, 1: 1, 2: 0})
    df = pd.concat([df, mirror], ignore_index=True)
    return df


def train(df):
    X = df[FEATURES].values
    y = df["outcome"].values
    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)),
    ])
    pipeline.fit(X, y)
    return pipeline


def evaluate(pipeline, df):
    X = df[FEATURES].values
    y = df["outcome"].values
    probs = pipeline.predict_proba(X)
    return {
        "accuracy": accuracy_score(y, pipeline.predict(X)),
        "log_loss": log_loss(y, probs, labels=CLASSES),
        "n": len(y),
    }


def main():
    if not os.path.exists(DATA_CSV):
        print(f"ERROR: {DATA_CSV} not found.", file=sys.stderr)
        sys.exit(1)

    df = load_data(DATA_CSV)
    print(f"Training on {len(df)} rows (after symmetric augmentation)...")

    pipeline = train(df)
    metrics = evaluate(pipeline, df)
    print(f"  Accuracy:  {metrics['accuracy']:.3f}")
    print(f"  Log-loss:  {metrics['log_loss']:.4f}")
    print(f"  Samples:   {metrics['n']}")

    with open(MODEL_PKL, "wb") as f:
        pickle.dump(pipeline, f)
    print(f"\nSaved model to {MODEL_PKL}")

    print("\nCalibration check  [P(away win), P(draw), P(home win)] by ELO diff:")
    for label, diff in [("Equal teams", 0), ("Home +100", 100),
                        ("Home +300", 300), ("Home +500", 500)]:
        p = pipeline.predict_proba([[diff, abs(diff)]])[0]
        print(f"  {label:14s}: away={p[0]:.3f}  draw={p[1]:.3f}  home={p[2]:.3f}")


if __name__ == "__main__":
    main()
