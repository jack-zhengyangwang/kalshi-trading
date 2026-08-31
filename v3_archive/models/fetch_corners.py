"""
fetch_corners.py — build the historical corner-statistics training corpus
(Arena v2, Phase 0).

Source: football-data.co.uk season CSVs:
    https://www.football-data.co.uk/mmz4281/{season}/{div}.csv
where season is e.g. "2324" (2023-24) and div is a league code (E0 = EPL).
Those CSVs carry per-match corner counts HC (home corners) / AC (away corners)
plus shots (HS/AS), shots on target (HST/AST), and goals — from ~2000/01 for the
big leagues. We train the corner-GENERATING process on this large club corpus;
World Cup team strength is supplied separately at inference (club-ELO fuzzy match).

Output: models/corners_dataset.csv — one tidy row per match with the columns
brain_v2's corner model consumes:
    div, season, date, home, away, fthg, ftag, hs, as_, hst, ast, hc, ac

Usage:
    python3 models/fetch_corners.py                       # 2010-11 .. 2024-25, default leagues
    python3 models/fetch_corners.py --start-year 2005 --end-year 2024
    python3 models/fetch_corners.py --divs E0 SP1 I1 D1 F1
"""
import argparse
import io
import os
import sys
import time

import pandas as pd
import requests

BASE = "https://www.football-data.co.uk/mmz4281"

# League codes that reliably carry HC/AC. Big-5 first, then liquid extras.
DEFAULT_DIVS = [
    "E0", "E1", "E2", "E3",          # England (Prem + EFL)
    "SC0",                            # Scotland Prem
    "D1", "D2",                       # Germany
    "I1", "I2",                       # Italy
    "SP1", "SP2",                     # Spain
    "F1", "F2",                       # France
    "N1",                             # Netherlands
    "B1",                             # Belgium
    "P1",                             # Portugal
    "T1",                             # Turkey
    "G1",                             # Greece
]

# football-data column -> our tidy name. Only these are kept.
COL_MAP = {
    "Div": "div",
    "Date": "date",
    "HomeTeam": "home",
    "AwayTeam": "away",
    "FTHG": "fthg",
    "FTAG": "ftag",
    "HS": "hs",
    "AS": "as_",
    "HST": "hst",
    "AST": "ast",
    "HC": "hc",
    "AC": "ac",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36"}


def season_code(start_year: int) -> str:
    """2023 -> '2324'  (the 2023-24 season).  2000 -> '0001'."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def fetch_one(season: str, div: str, timeout: int = 30) -> pd.DataFrame | None:
    """Download + parse one season/division CSV. Returns a tidy frame or None."""
    url = f"{BASE}/{season}/{div}.csv"
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        print(f"  [skip] {season}/{div}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200 or not r.content:
        return None
    # football-data CSVs are usually latin-1 and occasionally have ragged tails.
    try:
        raw = pd.read_csv(io.BytesIO(r.content), encoding="latin-1",
                          on_bad_lines="skip")
    except Exception as e:
        print(f"  [skip] {season}/{div}: parse error {e}", file=sys.stderr)
        return None

    have = [c for c in COL_MAP if c in raw.columns]
    if "HC" not in have or "AC" not in have:
        return None                      # no corner data in this file
    df = raw[have].rename(columns=COL_MAP)

    df["season"] = season
    if "div" not in df.columns:
        df["div"] = div

    # Require the essentials; drop rows missing corners or teams.
    df = df.dropna(subset=["home", "away", "hc", "ac"])
    # Coerce numerics; anything non-numeric becomes NaN then dropped for corners.
    for c in ("fthg", "ftag", "hs", "as_", "hst", "ast", "hc", "ac"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["hc", "ac"])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    return df if len(df) else None


def build(start_year, end_year, divs, out_path, pause=0.3):
    frames = []
    seasons = [season_code(y) for y in range(start_year, end_year + 1)]
    print(f"Fetching corners: {len(seasons)} seasons x {len(divs)} leagues "
          f"({seasons[0]}..{seasons[-1]})")
    for season in seasons:
        got = 0
        for div in divs:
            df = fetch_one(season, div)
            if df is not None:
                frames.append(df)
                got += len(df)
            time.sleep(pause)             # be polite to the host
        print(f"  {season}: {got} matches")

    if not frames:
        print("No data fetched.", file=sys.stderr)
        return 1

    full = pd.concat(frames, ignore_index=True)
    # Order columns; keep only the tidy set.
    cols = ["div", "season", "date", "home", "away",
            "fthg", "ftag", "hs", "as_", "hst", "ast", "hc", "ac"]
    full = full[[c for c in cols if c in full.columns]]
    full = full.drop_duplicates(subset=["season", "div", "date", "home", "away"])
    full = full.sort_values(["date", "div"]).reset_index(drop=True)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    full.to_csv(out_path, index=False)

    tot = (full["hc"] + full["ac"]).mean()
    print(f"\nWrote {len(full):,} matches -> {out_path}")
    print(f"  leagues: {sorted(full['div'].unique())}")
    print(f"  date range: {full['date'].min()} .. {full['date'].max()}")
    print(f"  mean total corners/match: {tot:.2f}  "
          f"(home {full['hc'].mean():.2f} / away {full['ac'].mean():.2f})")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Build historical corner dataset.")
    ap.add_argument("--start-year", type=int, default=2010)
    ap.add_argument("--end-year", type=int, default=2024)
    ap.add_argument("--divs", nargs="*", default=DEFAULT_DIVS)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__),
                                                  "corners_dataset.csv"))
    ap.add_argument("--pause", type=float, default=0.3)
    args = ap.parse_args()
    sys.exit(build(args.start_year, args.end_year, args.divs, args.out, args.pause))


if __name__ == "__main__":
    main()
