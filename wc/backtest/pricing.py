"""Bridge: our own brains -> the backtest engine's `model_prob` signal.

This is what makes `strategies/model-edge.json` — the live v4 logic expressed as
a spec — actually runnable, so the system we already trade can be graded on the
same engine, fee model, and metrics as every new idea.

╔══════════════════════════════════════════════════════════════════════════════╗
║ READ THIS BEFORE BELIEVING A NUMBER THAT CAME OUT OF HERE.                    ║
║                                                                              ║
║ The brains carry state fitted on outcomes that, relative to a historical bar, ║
║ HAVE NOT HAPPENED YET:                                                       ║
║                                                                              ║
║   • Elo ratings (models/team_elo.json, club_elo.json) are a CURRENT snapshot. ║
║     Pricing a bar from three months ago with today's Elo tells the strategy   ║
║     who went on to win.                                                      ║
║   • Stacker weights (arena_v3_state/brain_<league>.json) were trained by      ║
║     brains.train() on graded results spanning the whole period.               ║
║                                                                              ║
║ The engine's structural no-lookahead guarantee does NOT cover this. It stops  ║
║ a strategy reading a bar's own future; it cannot stop a MODEL that was fitted ║
║ on it. Fixing this properly needs point-in-time Elo snapshots, which we do    ║
║ not have and never recorded.                                                  ║
║                                                                              ║
║ So every report priced this way is stamped `lookahead_risk`, and run.py       ║
║ prints a warning. Treat the PnL as an upper bound on an upper bound, and      ║
║ treat calibration as the more honest signal — it degrades under leakage in a  ║
║ way that raw PnL does not.                                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝

See docs/backtester/02_ENGINE.md section 2 and 03_STRATEGY_DSL.md section 2.
"""
from __future__ import annotations

from wc import brains as brain_set
from wc import markets as mv

LOOKAHEAD_WARNING = (
    "model_prob came from brains carrying present-day state: Elo ratings are a "
    "current snapshot and stacker weights were trained on outcomes spanning the "
    "backtest window. This is model-fitted lookahead, which the engine's "
    "structural guarantee does not and cannot cover. PnL is optimistic by an "
    "unknown amount."
)


def event_code(ticker):
    """'KXEPLGAME-25SEP07ARSMU-ARS' -> '25SEP07ARSMU'. None when the ticker does
    not carry one."""
    parts = (ticker or "").split("-")
    return parts[1] if len(parts) > 2 else None


def _fixture(bar):
    """(home, away) for a bar, from the columns the collector resolved.

    Kalshi's market `title` is the LEG text ("Kobe wins"), not "A vs B", so the
    live scanner's home_away() cannot be reused here. The collector extracts the
    fixture from the rules prose at snapshot time and stores it; a bar collected
    before that column existed simply has no fixture and is skipped.
    """
    keys = bar.keys()
    home = bar["home"] if "home" in keys else None
    away = bar["away"] if "away" in keys else None
    return (home, away) if home else (None, None)


def _leg_is_home(parsed, home, away):
    """Which side of the fixture this leg is about.

    Returns None when the leg has no team (a draw, a total), which is correct:
    the brain's winner pricing uses `parsed['team'] is None` to mean the draw.
    """
    team = parsed.get("team")
    if not team or not home:
        return None
    t = "".join(c for c in team.lower() if c.isalnum())
    h = "".join(c for c in (home or "").lower() if c.isalnum())
    a = "".join(c for c in (away or "").lower() if c.isalnum())
    if t and h and (t in h or h in t):
        return True
    if t and a and (t in a or a in t):
        return False
    return None


def build_model_probs(bars, brains=None, use_llm=False, on_error=None):
    """{(ticker, ts): p_fair} for every bar the brains can price.

    Bars the brains cannot price are simply absent from the map, and the
    interpreter's fail-closed rule then makes any condition on `model_prob` or
    `edge` evaluate False. A market we cannot price is a market we do not trade,
    which is the correct direction to be wrong in.

    `use_llm` is False and stays False: an LLM call per bar would be neither
    affordable nor reproducible, and a backtest whose numbers change between
    runs is not a measurement.
    """
    if brains is None:
        brains = brain_set.load(use_llm=False)

    # home/away per event, recovered from the GAME legs' titles ("A vs B Winner?")
    teams = {}
    for b in bars:
        ec = event_code(b["ticker"])
        if not ec or ec in teams:
            continue
        h, a = _fixture(b)
        if h:
            teams[ec] = (h, a)

    probs, priors, skipped = {}, {}, {}
    for b in bars:
        ticker = b["ticker"]
        ec = event_code(ticker)
        if ec not in teams:
            skipped["no_fixture"] = skipped.get("no_fixture", 0) + 1
            continue
        home, away = teams[ec]

        league = brain_set.league_for_ticker(ticker)
        brain = brains.get(league)
        if brain is None:
            skipped["no_brain_for_league"] = skipped.get("no_brain_for_league", 0) + 1
            continue

        sub = b["sub_title"] if "sub_title" in b.keys() else None
        parsed = mv.parse_market_v2(ticker, sub)

        # The prior depends only on the fixture, so it is computed once per event
        # per league rather than once per bar — a full season is millions of bars.
        key = (ec, league)
        if key not in priors:
            try:
                priors[key] = brain.game_prior(home, away)
            except Exception as e:                       # a bad name must not kill the run
                priors[key] = None
                if on_error:
                    on_error(ticker, e)
        prior = priors[key]
        if prior is None:
            skipped["prior_failed"] = skipped.get("prior_failed", 0) + 1
            continue

        # market_mid is a stacker source in the live brain (weight ~0.2), so it
        # must be passed here too or the backtest would price differently from
        # production — the exact divergence this module exists to prevent.
        mid = None
        if b["yes_bid"] is not None and b["yes_ask"] is not None:
            mid = (b["yes_bid"] + b["yes_ask"]) / 200.0
        elif b["close"] is not None:
            mid = b["close"] / 100.0

        try:
            p, _sources = brain.pfair(parsed, prior, _leg_is_home(parsed, home, away),
                                      market_mid=mid, llm=None)
        except Exception as e:
            skipped["pfair_failed"] = skipped.get("pfair_failed", 0) + 1
            if on_error:
                on_error(ticker, e)
            continue

        if p is None:
            skipped["unpriceable_leg_type"] = skipped.get("unpriceable_leg_type", 0) + 1
            continue
        probs[(ticker, b["ts"])] = float(p)

    return probs, skipped
