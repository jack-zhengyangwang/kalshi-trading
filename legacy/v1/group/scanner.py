"""
scanner.py — Find candidate Kalshi markets worth evaluating.

Returns normalized market dicts for the Brain to enrich.
"""
import re
from datetime import datetime, timezone

# Game-adjacent market series (Phase 4) keyed by the winner series. Each shares
# the event-ticker SUFFIX (date+codes) with its winner market, e.g.
# KXWCSPREAD-26JUN12USAPAR ↔ KXWCGAME-26JUN12USAPAR, which is how we recover the
# full matchup (the adjacent market's title only names one team).
ADJACENT_SERIES = {
    "KXWCGAME": ["KXWCSPREAD", "KXWCTOTAL", "KXWCTEAMTOTAL", "KXWCBTTS"],
    "KXUCLGAME": ["KXUCLSPREAD", "KXUCLTOTAL", "KXUCLTEAMTOTAL", "KXUCLBTTS"],
    "KXEPLGAME": ["KXEPLSPREAD", "KXEPLTOTAL", "KXEPLTEAMTOTAL", "KXEPLBTTS"],
}

_VS_RE = re.compile(
    r"([A-Z][A-Za-z .'-]+?)\s+(?:vs\.?|v\.?)\s+([A-Z][A-Za-z .'-]+?)(?:\s+Winner)?\??$",
    re.IGNORECASE,
)


def _event_suffix(event_ticker):
    """'KXWCSPREAD-26JUN12USAPAR' -> '26JUN12USAPAR' (the matchup key)."""
    parts = (event_ticker or "").split("-", 1)
    return parts[1] if len(parts) == 2 else None


def _hours_until(iso):
    """Hours from now until an ISO timestamp; None if unparseable."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (t - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return None


def _dollars_to_cents(v):
    """'0.5300' (dollar string) -> 53 cents, or None if absent/zero."""
    if v in (None, ""):
        return None
    try:
        c = int(round(float(v) * 100))
        return c if c > 0 else None
    except (TypeError, ValueError):
        return None


def _normalize(m):
    """
    Normalize a raw Kalshi market dict. Kalshi's current API is dollar-
    denominated: yes_bid_dollars / yes_ask_dollars (e.g. "0.5300"), volume_fp,
    and occurrence_datetime for the actual event (kick-off) time.
    """
    return {
        "ticker": m.get("ticker", "?"),
        "title": m.get("title", "?"),
        # yes_sub_title is the authoritative label for what YES means
        # ("Canada" / "Bosnia and Herzegovina" / "Tie").
        "yes_sub_title": m.get("yes_sub_title", "") or "",
        "category": m.get("category", "") or "",
        "yes_bid_cents": _dollars_to_cents(m.get("yes_bid_dollars")),
        "yes_ask_cents": _dollars_to_cents(m.get("yes_ask_dollars")),
        "volume": float(m.get("volume_fp") or 0),
        # New schema has no per-side volume split; Brain falls back to mid.
        "volume_yes": 0,
        "volume_no": 0,
        # Prefer the real event/kick-off time for TVM; fall back to settlement.
        "close_time": (m.get("occurrence_datetime")
                       or m.get("expected_expiration_time")
                       or m.get("close_time", "") or ""),
    }


def _fetch_series(client, series_ticker, limit=200):
    """All open markets in a Kalshi series (e.g. KXWCGAME). Public endpoint."""
    try:
        data = client._get(
            "/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": limit},
            auth=False,
        )
        return data.get("markets", [])
    except Exception as e:
        print(f"  [SCANNER WARN] series '{series_ticker}' fetch failed: {e}", flush=True)
        return []


def scan(client, config):
    """
    Return list of candidate market dicts worth evaluating.

    Filters:
      - status = open
      - volume >= config["min_volume"]
      - has a valid YES bid price (illiquid markets with no book are skipped)
      - not already in our open positions

    Sources, in order:
      - config["series_tickers"]  → fetched directly by series (preferred for WC,
        e.g. ["KXWCGAME"] — the three-way game markets live here)
      - config["scan_queries"] / config["categories"] → text search fallback
    """
    # Tickers we already hold — skip them
    try:
        held = {p.get("ticker") for p in client.get_positions()}
    except Exception as e:
        print(f"  [SCANNER WARN] get_positions failed: {e}", flush=True)
        held = set()

    min_volume = config.get("min_volume", 0)
    max_hours = config.get("max_hours_to_kickoff", 72)  # focus on imminent games
    # Events we will not enter (e.g. a game already in play). Applies to every
    # caller — the live trader and all paper teams.
    exclude = config.get("exclude_event_suffixes", [])
    seen = set()
    candidates = []
    raw_markets = []

    # 1. Series-targeted fetch (the reliable path for World Cup games).
    for series in config.get("series_tickers", []):
        raw_markets.extend(_fetch_series(client, series))

    # 2. Text-search fallback (only if no series configured).
    if not config.get("series_tickers"):
        queries = config.get("scan_queries") or config.get("categories") or ["World Cup"]
        for query in queries:
            try:
                raw_markets.extend(client.search_markets(query, status="open", limit=50))
            except Exception as e:
                print(f"  [SCANNER WARN] search '{query}' failed: {e}", flush=True)

    for raw in raw_markets:
        m = _normalize(raw)
        ticker = m["ticker"]
        if ticker in seen or ticker in held:
            continue
        if any(suf in ticker for suf in exclude):   # skip excluded games (e.g. US live)
            continue
        if m["yes_bid_cents"] is None:   # no book yet — can't price/exit
            continue
        if m["volume"] < min_volume:
            continue
        # Only evaluate games kicking off within the window (cost + focus).
        # Keep markets with an unknown time rather than dropping silently.
        hrs = _hours_until(m["close_time"])
        if hrs is not None and (hrs < -3 or hrs > max_hours):
            continue
        seen.add(ticker)
        candidates.append(m)

    return candidates


def _matchup_map(client, winner_series):
    """suffix -> (home_name, away_name) from the winner series' titles."""
    out = {}
    for raw in _fetch_series(client, winner_series):
        suffix = _event_suffix(raw.get("event_ticker"))
        if not suffix or suffix in out:
            continue
        m = _VS_RE.search(raw.get("title", ""))
        if m:
            out[suffix] = (m.group(1).strip(), m.group(2).strip())
    return out


def _implied_total_map(client, total_series):
    """suffix -> market-implied E[total goals], summed from the over-N ladder's
    mid prices (E[N] = Σ P(N≥k) = over0.5 + over1.5 + …). Used to anchor the
    Brain's total to the market per game (calibration)."""
    ladders = {}
    for raw in _fetch_series(client, total_series):
        suffix = _event_suffix(raw.get("event_ticker"))
        b = raw.get("yes_bid_dollars"); a = raw.get("yes_ask_dollars")
        b = float(b) if b not in (None, "") else None
        a = float(a) if a not in (None, "") else None
        mid = (b + a) / 2 if (b is not None and a is not None) else (a or b)
        if suffix and mid is not None:
            ladders.setdefault(suffix, []).append(mid)
    return {s: sum(ps) for s, ps in ladders.items() if len(ps) >= 4}


def scan_adjacent(client, brain, config):
    """
    Phase 4 — discover + price game-adjacent markets (spread / total /
    team-total / BTTS) and return those with positive edge vs the ask.

    Read-only: no orders, no position/cap mutation. Each candidate carries the
    Brain's `evaluate_market` pricing plus the recovered matchup, ready for a
    strategy/tournament layer to act on (gated behind a per-game exposure cap,
    which needs explicit sign-off before it touches the live wallet).
    """
    min_volume = config.get("min_volume", 0)
    max_hours = config.get("max_hours_to_kickoff", 72)
    min_edge = config.get("adjacent_min_edge", config.get("min_edge", 0.03))

    candidates = []
    for winner_series, adj_list in ADJACENT_SERIES.items():
        if winner_series not in config.get("series_tickers", ["KXWCGAME"]):
            continue
        matchups = _matchup_map(client, winner_series)
        if not matchups:
            continue
        # Per-game market-implied total (the TOTAL series is named …TOTAL).
        total_series = next((s for s in adj_list if s.endswith("TOTAL")
                             and not s.endswith("TEAMTOTAL")), None)
        totals = _implied_total_map(client, total_series) if total_series else {}
        for series in config.get("adjacent_series", adj_list):
            for raw in _fetch_series(client, series):
                m = _normalize(raw)
                if m["volume"] < min_volume or m["yes_ask_cents"] is None:
                    continue
                hrs = _hours_until(m["close_time"])
                if hrs is not None and (hrs < -3 or hrs > max_hours):
                    continue
                matchup = matchups.get(_event_suffix(raw.get("event_ticker")))
                if not matchup:
                    continue
                home, away = matchup
                mkt_total = totals.get(_event_suffix(raw.get("event_ticker")))
                pricing = brain.evaluate_market(m, home, away, market_total=mkt_total)
                if not pricing or pricing.get("edge_vs_ask") is None:
                    continue
                if pricing["edge_vs_ask"] >= min_edge:
                    candidates.append({**m, "home": home, "away": away,
                                       "pricing": pricing})
    candidates.sort(key=lambda c: c["pricing"]["edge_vs_ask"], reverse=True)
    return candidates
