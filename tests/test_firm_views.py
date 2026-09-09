"""Pricing views — where PM independence actually lives.

The rule these enforce: SHARE OBSERVATIONS, NEVER SHARE BELIEFS. Two PMs may
read the same market; the moment they share a fitted model their P&Ls
re-correlate and the firm is one opinion wearing several hats.
"""
import pytest

from wc.firm import views


def bar(ticker="T", ts=0, bid=40, ask=44, home="Arsenal", away="Chelsea",
        sub="Arsenal", result=None, close_time=10_000, event="E1"):
    return {"ticker": ticker, "ts": ts, "yes_bid": bid, "yes_ask": ask,
            "close": (bid + ask) // 2 if bid is not None else None,
            "home": home, "away": away, "sub_title": sub, "result": result,
            "close_time": close_time, "event_ticker": event,
            "series": "S", "title": "t", "volume": 100, "open_interest": 50}


# ── registry ──────────────────────────────────────────────────────────────────

def test_every_view_is_registered_and_buildable():
    for name in views.REGISTRY:
        v = views.build({"view": name})
        assert v.name == name
        assert hasattr(v, "price")


def test_unknown_view_is_refused_with_the_options():
    with pytest.raises(ValueError, match="unknown view"):
        views.build({"view": "crystal_ball"})


def test_params_reach_the_view():
    v = views.build({"view": "momentum", "params": {"lookback": 7, "strength": 0.9}})
    assert v.lookback == 7 and v.strength == 0.9


# ── the baseline ──────────────────────────────────────────────────────────────

def test_market_view_has_exactly_zero_edge():
    """The honest zero that promotion gate 2.5 measures against. A PM that
    cannot beat this has variance, not edge."""
    b = bar(bid=40, ask=44)
    p = views.MarketView().price([b])[("T", 0)]
    mid = (40 + 44) / 200.0
    assert p == pytest.approx(mid), "p_fair must equal mid, so edge is 0"


def test_market_view_skips_a_bar_with_no_price():
    assert views.MarketView().price([bar(bid=None, ask=None)]) == {}


# ── independence ──────────────────────────────────────────────────────────────

def test_two_elo_views_with_different_params_genuinely_disagree():
    """Same code, independent state. This is the cheapest real diversity
    available, and it is the whole reason views are per-PM."""
    bars = []
    for i in range(6):
        bars += [bar(ticker=f"m{i}h", ts=i * 100, sub="Arsenal", event=f"E{i}",
                     result="yes", close_time=i * 100),
                 bar(ticker=f"m{i}a", ts=i * 100, sub="Chelsea", event=f"E{i}",
                     close_time=10_000)]
    slow = views.EloView(k=8.0).price(bars)
    fast = views.EloView(k=64.0).price(bars)
    assert slow != fast, "different k must produce different beliefs"


def test_momentum_and_reversion_disagree_by_construction():
    """Holding both means their divergence tells you the regime, instead of a
    backtest average that hides both."""
    rising = [bar(ts=i * 60, bid=30 + 3 * i, ask=34 + 3 * i) for i in range(6)]
    up = views.MomentumView().price(rising)
    down = views.MeanReversionView().price(rising)
    last = rising[-1]["ts"]
    assert up[("T", last)] > down[("T", last)]


# ── no lookahead ──────────────────────────────────────────────────────────────

def test_elo_learns_only_from_matches_already_finished():
    """A rating used at time t must reflect only matches finished before t.
    This is the property `models/team_elo.json` cannot have — it is a
    present-day snapshot, which is the lookahead stamped on --brains runs."""
    early = bar(ticker="g1", ts=0, sub="Arsenal", event="E1",
                result="yes", close_time=500)
    later = bar(ticker="g2", ts=100, sub="Arsenal", event="E2",
                result=None, close_time=9999)
    v = views.EloView(k=100.0)
    first = v.price([early])[("g1", 0)]

    # the same first bar must price identically whether or not a later,
    # already-settled match exists in the data
    settled_first = bar(ticker="g0", ts=-100, sub="Arsenal", event="E0",
                        result="yes", close_time=-100)
    with_history = views.EloView(k=100.0).price([settled_first, early])[("g1", 0)]
    assert with_history > first, "a prior win should raise the rating"

    after = views.EloView(k=100.0).price([early, later])
    assert after[("g1", 0)] == pytest.approx(first), \
        "the first bar's price must not change because of a later match"


def test_momentum_uses_only_the_trailing_window():
    rising = [bar(ts=i * 60, bid=30 + 3 * i, ask=34 + 3 * i) for i in range(6)]
    full = views.MomentumView().price(rising)
    prefix = views.MomentumView().price(rising[:4])
    for k, val in prefix.items():
        assert full[k] == pytest.approx(val), "future bars changed a past price"


def test_first_bars_are_unpriced_rather_than_guessed():
    """No history means no view. A market a PM cannot price is one it does not
    trade — the interpreter's fail-closed rule does the rest."""
    assert views.MomentumView().price([bar(ts=0)]) == {}
    assert views.MeanReversionView().price([bar(ts=0), bar(ts=60)]) == {}


# ── bounds ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["momentum", "mean_reversion", "elo"])
def test_probabilities_stay_in_range(name):
    bars = [bar(ts=i * 60, bid=max(1, 90 - 20 * i), ask=max(2, 94 - 20 * i))
            for i in range(6)]
    for p in views.build({"view": name}).price(bars).values():
        assert 0.0 < p < 1.0, f"{name} produced {p}"


def test_elo_skips_bars_with_no_fixture():
    assert views.EloView().price([bar(home=None, away=None)]) == {}


def test_structural_view_declares_its_lookahead():
    """A PM using it must be markable as ranking-only, without the reader
    having to know which views are safe."""
    assert views.StructuralView().lookahead is True
    assert getattr(views.MarketView(), "lookahead", False) is False
    assert getattr(views.EloView(), "lookahead", False) is False
