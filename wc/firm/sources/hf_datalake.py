"""Hugging Face `eatpizzanot/soccer-dataset` -> normalised match rows.

673,966 fixtures across 271 leagues, 2008-2027, CC-BY-4.0. Sourced from
API-Football and football-data.co.uk, deduplicated and quality-gated.

What it uniquely carries: the AMERICAS. MLS, Liga MX, Brasileirao and the
Argentine divisions — which dominate the Kalshi surface and which openfootball
does not reach — plus bookmaker odds and shot counts.

WE STORE SHOTS, NOT xG — and that is a deliberate choice, not a shortcut.

The dataset's own README admits its xG is a deterministic shots-by-zone
formula, roughly

    xg = 0.115*shots_in_box + 0.035*shots_outside + 0.648*penalties

correlating only ~0.4 with goals. That is not a per-shot model; it is an
opinion about shots. And `match_stats` carries the shot counts themselves.

So the ingredients go in the shared store as facts, and any desk that believes
that formula can apply it inside its own view — while another weights shots
differently, or ignores them. Importing the derived column instead would have
put one provider's belief into the shared store and made every desk agree with
it by default. Facts are shared; beliefs are not.

Missing values stay NULL and must never be read as zero: an uncovered
league-season would otherwise look like a league where nobody shoots.

The fixtures table is normalised, so this needs real joins:

    fixtures.league_id     -> leagues.id      (name, country)
    fixtures.home_team_id  -> teams.id        (name)
    fixtures.id            -> match_stats.fixture_id
    fixtures.id            -> odds.fixture_id (carries its own `known_at`)

We pull only the leagues in config/leagues_map.json, not all 271: smaller,
faster, and it keeps the team registry from colliding clubs across continents.
"""
from __future__ import annotations

import datetime as dt

RESOLVE = "https://huggingface.co/datasets/eatpizzanot/soccer-dataset/resolve/main"
FIXTURES = f"{RESOLVE}/fixtures.parquet"
MATCH_STATS = f"{RESOLVE}/match_stats.parquet"
ODDS = f"{RESOLVE}/odds.parquet"


def _col(df, *names):
    """First column present under any of `names`. Schemas drift between
    dataset releases; guessing one name breaks the whole pull."""
    for n in names:
        if n in df.columns:
            return n
    return None


def _read_parquet(url):
    """Fetch with requests, then parse from memory.

    pandas reads a URL through urllib, which uses a different trust store and
    fails with CERTIFICATE_VERIFY_FAILED on some machines. requests is already
    a dependency and already works everywhere else in this repo.
    """
    import io

    import pandas as pd
    import requests

    r = requests.get(url, timeout=300)
    r.raise_for_status()
    return pd.read_parquet(io.BytesIO(r.content))


def wanted_leagues(leagues_df, leagues):
    """{league_id: our_league_name} for the leagues we actually trade.

    Matched on name AND country, always. "Serie A" alone matches Italy, Brazil
    and Ecuador — three different competitions that would otherwise merge into
    one league's history, and one team registry.
    """
    want = {}
    for name, cfg in leagues.items():
        af, country = cfg.get("af_name"), cfg.get("country")
        if af:
            want[(af.strip().lower(), (country or "").strip().lower())] = name

    out = {}
    for _, r in leagues_df.iterrows():
        key = (str(r["name"]).strip().lower(), str(r["country"]).strip().lower())
        if key in want:
            out[r["id"]] = want[key]
    return out


def collect(leagues, since_year=2021, on_progress=None, with_stats=True,
            with_odds=True):
    """Every played match in our leagues, joined to names, shots and odds."""
    import pandas as pd

    say = on_progress or (lambda *_: None)

    leagues_df = _read_parquet(f"{RESOLVE}/leagues.parquet")
    league_ids = wanted_leagues(leagues_df, leagues)
    say(f"  matched {len(league_ids)} of {len(leagues_df)} catalogue leagues")
    if not league_ids:
        return []

    fx = _read_parquet(FIXTURES)
    say(f"  fixtures: {len(fx):,}")

    fx = fx[fx["league_id"].isin(league_ids.keys())].copy()
    if "is_played" in fx.columns:
        fx = fx[fx["is_played"].astype(bool)]
    ts = pd.to_datetime(fx["date_utc"], utc=True, errors="coerce")
    keep = ts.dt.year >= since_year
    fx = fx[keep].copy()
    # datetime64 may be ns, us, or ms depending on the release. Dividing by a
    # hardcoded 10**9 silently produced 1970 timestamps; casting to seconds
    # first is unit-independent and cannot drift with the dataset.
    fx["_ts"] = ts[keep].dt.tz_localize(None).astype("datetime64[s]").astype("int64")
    fx["_league"] = fx["league_id"].map(league_ids)
    say(f"  in our leagues, played, since {since_year}: {len(fx):,}")
    if fx.empty:
        return []

    teams_df = _read_parquet(f"{RESOLVE}/teams.parquet")
    names = dict(zip(teams_df["id"], teams_df["name"]))

    stats = odds = None
    if with_stats:
        s = _read_parquet(f"{RESOLVE}/match_stats.parquet")
        stats = s[s["fixture_id"].isin(fx["id"])].set_index("fixture_id")
        say(f"  match_stats joined: {len(stats):,}")
    if with_odds:
        o = _read_parquet(f"{RESOLVE}/odds.parquet")
        o = o[o["fixture_id"].isin(fx["id"])]
        # one bookmaker per fixture; the first is enough for a consensus signal
        odds = o.groupby("fixture_id").first()
        say(f"  odds joined: {len(odds):,}")

    def stat(fid, col):
        if stats is None or fid not in stats.index:
            return None
        v = stats.at[fid, col] if col in stats.columns else None
        return None if v is None or pd.isna(v) else float(v)

    def odd(fid, col):
        if odds is None or fid not in odds.index:
            return None
        v = odds.at[fid, col] if col in odds.columns else None
        return None if v is None or pd.isna(v) else float(v)

    rows = []
    for _, r in fx.iterrows():
        try:
            hg, ag = int(r["goals_home"]), int(r["goals_away"])
        except (TypeError, ValueError):
            continue
        home, away = names.get(r["home_team_id"]), names.get(r["away_team_id"])
        if not home or not away:
            continue
        fid = r["id"]
        rows.append({
            "league": r["_league"],
            "season": None,
            "kickoff_ts": int(r["_ts"]),
            "home_raw": str(home),
            "away_raw": str(away),
            "home_goals": hg,
            "away_goals": ag,
            "ht_home_goals": None,
            "ht_away_goals": None,
            # Shot counts are OBSERVATIONS. xG over them is a belief, and
            # belongs in a view, not here.
            "shots_home": stat(fid, "home_shots_total"),
            "shots_away": stat(fid, "away_shots_total"),
            "shots_in_box_home": stat(fid, "home_shots_inside_box"),
            "shots_in_box_away": stat(fid, "away_shots_inside_box"),
            "shots_out_box_home": stat(fid, "home_shots_outside_box"),
            "shots_out_box_away": stat(fid, "away_shots_outside_box"),
            "corners_home": stat(fid, "home_corners"),
            "corners_away": stat(fid, "away_corners"),
            "odds_home": odd(fid, "home_win"),
            "odds_draw": odd(fid, "draw"),
            "odds_away": odd(fid, "away_win"),
            "source": "hf_datalake",
        })

    if on_progress:
        by = {}
        for x in rows:
            by[x["league"]] = by.get(x["league"], 0) + 1
        for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
            say(f"  {k:<14} {v:>6} matches")
    return rows
