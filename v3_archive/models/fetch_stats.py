#!/usr/bin/env python3
"""
Fetch current ELO ratings for World Cup 2026 teams from api.clubelo.com.
Run once before the tournament: python3 models/fetch_stats.py

Writes: models/team_elo.json  {team_name: elo_float, ...}
"""
import json
import os
import sys
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(BASE_DIR, "team_elo.json")

# World Cup 2026 qualified teams (48 teams)
WC2026_TEAMS = [
    # CONCACAF
    "USA", "Mexico", "Canada", "Panama", "Honduras", "Jamaica",
    # CONMEBOL
    "Argentina", "Brazil", "Colombia", "Uruguay", "Ecuador", "Venezuela",
    "Paraguay", "Bolivia", "Chile",
    # UEFA
    "Germany", "Spain", "France", "England", "Portugal", "Netherlands",
    "Belgium", "Italy", "Croatia", "Austria", "Switzerland", "Poland",
    "Turkey", "Serbia", "Denmark", "Scotland", "Ukraine", "Hungary",
    "Slovenia", "Slovakia", "Czech Republic",
    # AFC
    "Japan", "South Korea", "Iran", "Australia", "Saudi Arabia",
    "Uzbekistan", "Jordan", "Qatar", "Iraq", "Oman",
    # CAF
    "Morocco", "Senegal", "Nigeria", "Egypt", "Algeria", "Ghana",
    "Cameroon", "Tunisia", "Ivory Coast", "Mali", "Angola",
    # OFC
    "New Zealand",
]

# Mapping from common name variants to clubelo.com spelling
CLUBELO_NAME_MAP = {
    "USA": "USA",
    "South Korea": "SouthKorea",
    "Czech Republic": "CzechRepublic",
    "Saudi Arabia": "SaudiArabia",
    "Ivory Coast": "IvoryCoast",
    "New Zealand": "NewZealand",
}

# Fallback ELO values (used if API is unavailable or team not found)
FALLBACK_ELO = {
    "Argentina": 2141, "France": 2000, "Brazil": 2050, "England": 1960,
    "Spain": 2030, "Portugal": 2012, "Germany": 1988, "Netherlands": 1980,
    "Belgium": 1922, "Croatia": 1929, "Italy": 1900, "Uruguay": 1862,
    "USA": 1790, "Mexico": 1841, "Canada": 1820, "Denmark": 1843,
    "Switzerland": 1879, "Colombia": 1870, "Poland": 1885, "Australia": 1784,
    "Morocco": 1750, "Senegal": 1800, "Japan": 1803, "South Korea": 1808,
    "Serbia": 1887, "Austria": 1840, "Turkey": 1810, "Ukraine": 1800,
    "Ecuador": 1780, "Panama": 1720, "Honduras": 1690, "Jamaica": 1660,
    "Venezuela": 1760, "Paraguay": 1750, "Bolivia": 1640, "Chile": 1810,
    "Nigeria": 1750, "Egypt": 1720, "Algeria": 1740, "Ghana": 1693,
    "Cameroon": 1696, "Tunisia": 1705, "Iran": 1740, "Saudi Arabia": 1765,
    "Uzbekistan": 1730, "Jordan": 1700, "Qatar": 1640, "Iraq": 1680,
    "Oman": 1640, "Ivory Coast": 1760, "Mali": 1710, "Angola": 1660,
    "New Zealand": 1600, "Hungary": 1810, "Slovenia": 1760, "Slovakia": 1780,
    "Scotland": 1800, "Czech Republic": 1800,
}


def clubelo_name(team):
    return CLUBELO_NAME_MAP.get(team, team.replace(" ", ""))


def fetch_elo(team):
    name = clubelo_name(team)
    url = f"http://api.clubelo.com/{name}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        # Format: Rank,Club,Country,Level,Elo,From,To
        last = lines[-1].split(",")
        if len(last) < 5:
            return None
        return float(last[4])
    except Exception:
        return None


def main():
    result = {}
    failed = []

    print(f"Fetching ELO for {len(WC2026_TEAMS)} teams...")
    for team in WC2026_TEAMS:
        elo = fetch_elo(team)
        if elo is not None:
            result[team] = elo
            print(f"  {team}: {elo:.0f}")
        else:
            fallback = FALLBACK_ELO.get(team, 1700)
            result[team] = fallback
            failed.append(team)
            print(f"  {team}: {fallback:.0f} (fallback)")

    with open(OUTPUT, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)

    print(f"\nWrote {len(result)} teams to {OUTPUT}")
    if failed:
        print(f"Used fallback for: {', '.join(failed)}")


if __name__ == "__main__":
    main()
