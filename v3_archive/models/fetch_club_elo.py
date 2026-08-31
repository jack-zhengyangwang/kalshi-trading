#!/usr/bin/env python3
"""
fetch_club_elo.py — pull a full club-ELO snapshot from clubelo.com (one call
returns every club's current rating) → models/club_elo.json {club: elo}.

Run once before backtesting club competitions: python3 models/fetch_club_elo.py
"""
import json
import os
import sys

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "club_elo.json")
SNAPSHOT = "2026-06-01"   # any recent date; clubelo returns all clubs active then


def main():
    url = f"http://api.clubelo.com/{SNAPSHOT}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    elo = {}
    for line in r.text.strip().split("\n")[1:]:   # skip header
        p = line.split(",")
        if len(p) >= 5 and p[1]:
            try:
                elo[p[1]] = round(float(p[4]), 1)
            except ValueError:
                pass
    if not elo:
        print("No clubs parsed — aborting.", file=sys.stderr)
        sys.exit(1)
    json.dump(elo, open(OUT, "w"), indent=2, sort_keys=True)
    print(f"Wrote {len(elo)} clubs to {OUT}")
    for c in ("Arsenal", "Real Madrid", "Paris SG", "Liverpool", "Inter", "Villarreal"):
        if c in elo:
            print(f"  {c}: {elo[c]}")


if __name__ == "__main__":
    main()
