"""ESPN → the firm's fact store.

Facts, not beliefs: recent form, head-to-head, table position, bookmaker odds,
squad size, news volume. Every desk reads the same numbers; what they make of
them is their own.

TIMING, WHICH IS THE WHOLE GAME. A fact is stamped `as_of` = the moment we
fetched it, because that is genuinely when we knew it. ESPN serves the CURRENT
state of the world, not a historical snapshot — asking it about a match from
August returns today's form, not August's. So facts from here accrue FORWARD,
exactly like the market collector, and a fact-driven PM is forward-testable
immediately but backtestable only once history builds.

The complement is `wc/firm/sources/derived.py`: form and head-to-head
reconstructed point-in-time from settled markets we already hold, which IS
backfillable. Between them: external facts forward, derivable facts backward.

BOOKMAKER ODDS are the most valuable thing here and were not on the original
list. They are an independent market consensus — a second opinion on the same
match, formed by people with real money at stake and no view of Kalshi's book.
A disagreement between them is the cleanest edge signal in this file.
"""
from __future__ import annotations

import time

import requests

API = "https://site.api.espn.com/apis/site/v2/sports/soccer"
TIMEOUT = 25

# Kalshi's series prefix -> ESPN's league slug. Absent leagues are skipped
# rather than guessed: a wrong slug returns another league's fixtures, and
# silently attaching Brazilian form to an English match is worse than no fact.
LEAGUES = {
    "KXEPLGAME": "eng.1",
    "KXLALIGAGAME": "esp.1",
    "KXSERIEAGAME": "ita.1",
    "KXBUNDESLIGAGAME": "ger.1",
    "KXLIGUE1GAME": "fra.1",
    "KXMLSGAME": "usa.1",
    "KXEFLCHAMPIONSHIPGAME": "eng.2",
    "KXLIGAMXGAME": "mex.1",
    "KXBRASILEIROGAME": "bra.1",
    "KXUCLGAME": "uefa.champions",
    "KXUELGAME": "uefa.europa",
    "KXEREDIVISIEGAME": "ned.1",
    "KXPRIMEIRALIGAGAME": "por.1",
}


def _get(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def scoreboard(league):
    return _get(f"{API}/{league}/scoreboard")


def summary(league, event_id):
    return _get(f"{API}/{league}/summary", params={"event": event_id})


# ── extraction ────────────────────────────────────────────────────────────────

def _team_name(block):
    """ESPN returns `team` as an object in most places and a bare string in
    some standings payloads. Guessing one shape crashes the whole league fetch
    over a field we only need for a label."""
    t = block.get("team") if isinstance(block, dict) else None
    if isinstance(t, dict):
        return t.get("displayName") or t.get("name") or t.get("shortDisplayName")
    return t if isinstance(t, str) and t.strip() else None


def _parse_ts(iso):
    import datetime as dt
    try:
        return int(dt.datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _oriented(e):
    """(goals_for, goals_against) for the team whose block this is.

    `score` is the MATCH score, not for/against — `atVs` says which side the
    team was on ('vs' = home, 'at' = away). Reading `score` left-to-right would
    invert every away result, turning a good defensive record into a bad one.
    """
    try:
        h = int(e.get("homeTeamScore"))
        a = int(e.get("awayTeamScore"))
    except (TypeError, ValueError):
        return None, None
    return (h, a) if str(e.get("atVs", "")).lower() == "vs" else (a, h)


def team_facts(summary_json, as_of):
    """[{entity, feature, value, as_of}] for both teams in one match.

    Form is stamped as-of the LAST GAME IT REFLECTS, not the fetch time. A
    last-five record became true when the fifth game finished; dating it today
    would claim we knew it earlier than we did in one direction and later in the
    other.
    """
    out = []

    for block in (summary_json.get("lastFiveGames") or []):
        team = _team_name(block)
        if not team:
            continue

        pts = played = gf_total = ga_total = 0
        latest = None
        for e in (block.get("events") or []):
            res = e.get("gameResult")
            if res not in ("W", "D", "T", "L"):
                continue
            played += 1
            pts += 3 if res == "W" else (1 if res in ("D", "T") else 0)
            gf, ga = _oriented(e)
            if gf is not None:
                gf_total += gf
                ga_total += ga
            ts = _parse_ts(e.get("gameDate"))
            if ts and (latest is None or ts > latest):
                latest = ts

        if not played:
            continue
        known_at = min(latest or as_of, as_of)   # never claim to know it early
        out += [
            {"entity": team, "feature": "form_last5_ppg",
             "value": pts / played, "as_of": known_at},
            {"entity": team, "feature": "form_last5_games",
             "value": played, "as_of": known_at},
            {"entity": team, "feature": "form_last5_gd",
             "value": (gf_total - ga_total) / played, "as_of": known_at},
            {"entity": team, "feature": "form_last5_gf",
             "value": gf_total / played, "as_of": known_at},
        ]

    for r in (summary_json.get("rosters") or []):
        team = _team_name(r)
        players = r.get("roster") or []
        if team and players:
            out.append({"entity": team, "feature": "squad_listed",
                        "value": len(players), "as_of": as_of})

    for group in ((summary_json.get("standings") or {}).get("groups") or []):
        for entry in ((group.get("standings") or {}).get("entries") or []):
            team = _team_name(entry)
            stats = {s.get("name"): s.get("value")
                     for s in (entry.get("stats") or []) if isinstance(s, dict)}
            if not team:
                continue
            if stats.get("rank") is not None:
                out.append({"entity": team, "feature": "table_rank",
                            "value": stats["rank"], "as_of": as_of})
            if stats.get("pointsFor") is not None:
                out.append({"entity": team, "feature": "season_goals_for",
                            "value": stats["pointsFor"], "as_of": as_of})
    return out


def implied_from_moneyline(ml):
    """American moneyline -> implied probability.

    +115 means stake 100 to win 115, so p = 100/(115+100). -150 means stake 150
    to win 100, so p = 150/(150+100). Getting the sign wrong inverts every
    favourite, which is the kind of error that looks like a strategy.
    """
    if ml is None:
        return None
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml == 0:
        return None
    return 100.0 / (ml + 100.0) if ml > 0 else abs(ml) / (abs(ml) + 100.0)


def devig(probs):
    """Normalise home/draw/away to sum to 1.

    Raw implied probabilities sum to MORE than 1 — the overround is the
    bookmaker's margin. Comparing a vigged 1.06-sum book against Kalshi would
    show a systematic edge that is really just the margin, in every market, in
    the same direction. That is exactly what a fake edge looks like.
    """
    live = {k: v for k, v in probs.items() if v is not None}
    total = sum(live.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in live.items()}


def event_facts(summary_json, event_key, as_of):
    """Match-level facts, keyed by the EVENT rather than a team."""
    out = []

    # Bookmaker consensus — the most useful thing ESPN gives us, and the only
    # signal here formed by other people with money at stake and no sight of
    # Kalshi's book. A disagreement between the two is a real edge candidate.
    for pc in (summary_json.get("pickcenter") or []):
        raw = {
            "book_prob_home": implied_from_moneyline(
                (pc.get("homeTeamOdds") or {}).get("moneyLine")),
            "book_prob_away": implied_from_moneyline(
                (pc.get("awayTeamOdds") or {}).get("moneyLine")),
            "book_prob_draw": implied_from_moneyline(
                (pc.get("drawOdds") or {}).get("moneyLine")),
        }
        fair = devig(raw)
        if len(fair) < 2:
            continue
        for feat, v in fair.items():
            out.append({"entity": event_key, "feature": feat,
                        "value": v, "as_of": as_of})
        overround = sum(v for v in raw.values() if v is not None)
        out.append({"entity": event_key, "feature": "book_overround",
                    "value": overround, "as_of": as_of})
        if isinstance(pc.get("overUnder"), (int, float)):
            out.append({"entity": event_key, "feature": "book_total_goals",
                        "value": float(pc["overUnder"]), "as_of": as_of})
        break                                        # first provider is enough

    for ss in (summary_json.get("seasonseries") or []):
        events = ss.get("events") or []
        if events:
            out.append({"entity": event_key, "feature": "h2h_games",
                        "value": len(events), "as_of": as_of})

    news = (summary_json.get("news") or {}).get("articles") or []
    if news:
        out.append({"entity": event_key, "feature": "news_articles",
                    "value": len(news), "as_of": as_of})
        out.append({"entity": event_key, "feature": "news_headline",
                    "value": None,
                    "text_value": str(news[0].get("headline"))[:300],
                    "as_of": as_of})
    return out


def event_key(ev):
    comps = ((ev.get("competitions") or [{}])[0].get("competitors") or [])
    names = sorted((c.get("team") or {}).get("displayName") or "" for c in comps)
    return "|".join(n for n in names if n)


def collect_league(con, league, as_of=None, max_events=None):
    """Fetch one league's fixtures and write every fact we can extract."""
    from wc.firm import facts
    as_of = int(as_of if as_of is not None else time.time())
    try:
        sb = scoreboard(league)
    except Exception as e:
        return {"league": league, "error": str(e)[:120], "facts": 0, "events": 0}

    events = sb.get("events") or []
    if max_events:
        events = events[:max_events]

    rows, n_events = [], 0
    for ev in events:
        try:
            s = summary(league, ev["id"])
        except Exception:
            continue                                 # one bad event, not a lost run
        rows += team_facts(s, as_of)
        rows += event_facts(s, event_key(ev), as_of)
        n_events += 1

    written = facts.put_many(con, rows, source=f"espn:{league}") if rows else 0
    return {"league": league, "events": n_events, "facts": written}
