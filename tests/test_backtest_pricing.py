"""The brains -> engine bridge.

This is the module that lets us grade the system we already trade. It is also
the one place where lookahead can enter through a door the engine cannot guard,
so the tests cover both the wiring and the honesty about that.
"""
import pytest

from wc.backtest import data, engine, interpret, pricing


class FakeBrain:
    """A brain with the same surface as BrainV4, without Elo or an LLM."""

    def __init__(self, p=0.7, prior_raises=False, pfair_raises=False):
        self.p, self.prior_raises, self.pfair_raises = p, prior_raises, pfair_raises
        self.prior_calls = 0

    def game_prior(self, home, away, market_total=None):
        self.prior_calls += 1
        if self.prior_raises:
            raise ValueError("unknown team")
        return {"home": home, "away": away, "mu_home": 1.4, "mu_away": 1.2}

    def pfair(self, parsed, prior, leg_is_home, market_mid=None, llm=None):
        if self.pfair_raises:
            raise ValueError("cannot price")
        return self.p, {"data": self.p, "market": market_mid}


def row(ticker="KXEPLGAME-25SEP07ARSMU-ARS", ts=1000, title="Arsenal wins",
        sub="Arsenal", bid=40, ask=44, home="Arsenal", away="Man United"):
    """A bar shaped like data.load_bars returns.

    Note `title` is the LEG text ("Arsenal wins"), which is what Kalshi actually
    sends — the fixture lives in the home/away columns the collector resolved.
    """
    return {"ticker": ticker, "ts": ts, "title": title, "sub_title": sub,
            "home": home, "away": away, "event_ticker": "KXEPLGAME-25SEP07ARSMU",
            "yes_bid": bid, "yes_ask": ask, "close": 42,
            "series": "KXEPLGAME", "volume": 100, "open_interest": 50,
            "close_time": 99999, "status": "active", "result": None}


class Row(dict):
    def keys(self):                       # sqlite3.Row-like
        return super().keys()


def bars(*rows):
    return [Row(r) for r in rows]


# ── wiring ────────────────────────────────────────────────────────────────────

def test_prices_a_bar_the_brain_can_handle():
    probs, skips = pricing.build_model_probs(
        bars(row()), brains={"EPL": FakeBrain(0.62)})
    assert probs[("KXEPLGAME-25SEP07ARSMU-ARS", 1000)] == pytest.approx(0.62)
    assert skips == {}


def test_market_mid_is_passed_to_the_brain():
    """market_mid is a stacker source in the live brain, so omitting it here
    would make the backtest price differently from production — the exact
    divergence this module exists to prevent."""
    seen = {}

    class Spy(FakeBrain):
        def pfair(self, parsed, prior, leg_is_home, market_mid=None, llm=None):
            seen["mid"] = market_mid
            return 0.5, {}

    pricing.build_model_probs(bars(row(bid=40, ask=44)), brains={"EPL": Spy()})
    assert seen["mid"] == pytest.approx(0.42)


def test_prior_is_computed_once_per_fixture_not_once_per_bar():
    """A season is millions of bars; the prior depends only on the fixture."""
    brain = FakeBrain()
    pricing.build_model_probs(
        bars(row(ts=1), row(ts=2), row(ts=3)), brains={"EPL": brain})
    assert brain.prior_calls == 1


def test_leg_is_home_resolves_both_sides():
    assert pricing._leg_is_home({"team": "Arsenal"}, "Arsenal", "Man United") is True
    assert pricing._leg_is_home({"team": "Man United"}, "Arsenal", "Man United") is False
    assert pricing._leg_is_home({"team": None}, "Arsenal", "Man United") is None


def test_event_code_extraction():
    assert pricing.event_code("KXEPLGAME-25SEP07ARSMU-ARS") == "25SEP07ARSMU"
    assert pricing.event_code("NODASHES") is None


# ── fail closed ───────────────────────────────────────────────────────────────

def test_unpriceable_bars_are_absent_not_guessed():
    """A market we cannot price is a market we do not trade. Absence makes the
    interpreter's fail-closed rule do the right thing automatically."""
    probs, skips = pricing.build_model_probs(
        bars(row(home=None, away=None)),
        brains={"EPL": FakeBrain()})
    assert probs == {}
    assert skips["no_fixture"] == 1


def test_a_brain_that_raises_does_not_kill_the_run():
    probs, skips = pricing.build_model_probs(
        bars(row()), brains={"EPL": FakeBrain(pfair_raises=True)})
    assert probs == {}
    assert skips["pfair_failed"] == 1


def test_a_bad_prior_is_skipped_and_counted():
    probs, skips = pricing.build_model_probs(
        bars(row()), brains={"EPL": FakeBrain(prior_raises=True)})
    assert probs == {}
    assert skips["prior_failed"] == 1


def test_none_pfair_counts_as_an_unpriceable_leg_type():
    class NoPrice(FakeBrain):
        def pfair(self, *a, **kw):
            return None, {}

    probs, skips = pricing.build_model_probs(bars(row()), brains={"EPL": NoPrice()})
    assert probs == {}
    assert skips["unpriceable_leg_type"] == 1


def test_missing_signal_makes_the_strategy_stand_down():
    """End-to-end fail-closed: no model_prob means an edge condition is False,
    so the strategy places nothing rather than trading on a guess."""
    assert interpret.evaluate({"signal": "edge", "op": "gt", "value": 0.05},
                              {"price": 0.4}) is False


# ── the honesty requirement ───────────────────────────────────────────────────

def test_leg_title_is_not_mistaken_for_a_fixture():
    """Kalshi's `title` is the leg text ('Kobe wins'), never 'A vs B'. Reading a
    fixture out of it was the bug that made every real bar unpriceable."""
    probs, _ = pricing.build_model_probs(
        bars(row(title="Kobe wins", home="Port FC", away="Kobe")),
        brains={"EPL": FakeBrain(0.55)})
    assert len(probs) == 1


def test_lookahead_warning_names_the_actual_mechanism():
    """The warning has to say WHY, or it becomes boilerplate people skip."""
    w = pricing.LOOKAHEAD_WARNING.lower()
    assert "elo" in w and "stacker" in w and "trained" in w


# ── it actually drives the engine ─────────────────────────────────────────────

def test_model_probs_reach_the_engine_and_enable_an_edge_strategy():
    def eng_bar(ts):
        return {"ticker": "M", "series": "S", "ts": ts, "yes_bid": 39,
                "yes_ask": 41, "open": 40, "high": 40, "low": 40, "close": 40,
                "volume": 10_000, "open_interest": 1000,
                "close_time": 10 * 86400, "status": "active", "result": "yes"}

    spec = {
        "name": "edge", "side": "yes",
        "universe": {"min_volume": 1},
        "entry": {"all": [{"signal": "edge", "op": "gt", "value": 0.10}]},
        "sizing": {"method": "fixed", "dollars": 100.0, "max_bet_dollars": 100.0},
        "exit": {"any": [{"signal": "hold_to_settlement", "op": "eq", "value": True}]},
        "caps": {"daily_spend_dollars": 1e6, "per_market_dollars": 1e6,
                 "total_exposure_dollars": 1e6},
    }
    no_cost = engine.Costs(fee_rate=0.0, slippage_cents=0.0, max_volume_share=1.0)
    bar_list = [eng_bar(0), eng_bar(60), eng_bar(11 * 86400)]   # decide, fill, settle

    without, _ = engine.run(spec, bar_list, 1000.0, no_cost)
    assert without == []                                  # no model_prob -> no trade

    with_probs, _ = engine.run(spec, bar_list, 1000.0, no_cost,
                               model_probs={("M", 0): 0.70, ("M", 60): 0.70})
    assert len(with_probs) == 1
    assert with_probs[0]["settled"] is True
