"""A PM's book: several agents, independent capital, one risk limit.

This is the desk model. The tests that matter are the ones about agents being
genuinely separate books while the PM's caps still bind across them — the thing
that would silently break if each agent were replayed on its own.
"""
import pytest

from wc.backtest import engine
from tests.test_backtest_engine import bar, spec, with_fill_bar, NO_COST


def book(name, bankroll=1000.0, **over):
    return engine.Book(name, spec(**over), bankroll)


ALWAYS = {"all": [{"signal": "price", "op": "lt", "value": 0.99}]}


# ── independence ──────────────────────────────────────────────────────────────

def test_each_agent_keeps_its_own_position_in_the_same_market():
    """Two traders on one desk may both hold Arsenal. Keying positions by
    ticker alone would have one agent's exit close the other's trade."""
    books = [book("a"), book("b")]
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run_book(books, bars, costs=NO_COST)
    assert {t["agent"] for t in trades} == {"a", "b"}
    assert len(trades) == 2


def test_agents_spend_their_own_capital():
    rich, poor = book("rich", 10_000.0), book("poor", 12.0)
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run_book([rich, poor], bars, costs=NO_COST)
    by = {t["agent"]: t for t in trades}
    assert by["rich"]["contracts"] > by["poor"]["contracts"]


def test_one_agents_loss_does_not_touch_another(tmp_path):
    winner = book("winner")
    loser = book("loser")
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run_book([winner, loser], bars, costs=NO_COST)
    assert winner.bankroll == loser.bankroll, "same spec, same capital, same result"


def test_every_trade_is_attributed_to_an_agent():
    """A desk whose profit comes from one agent while the others bleed is a
    different thing from one that is broadly right."""
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run_book([book("a"), book("b")], bars, costs=NO_COST)
    assert all(t.get("agent") for t in trades)


# ── the PM's risk limit binds across agents ───────────────────────────────────

def test_pm_daily_cap_binds_across_agents():
    """Two agents that each pass their own cap must still not breach the PM's.
    This is why the book is replayed in ONE pass — separate runs could never
    see two agents spending at once."""
    books = []
    for n in ("a", "b", "c"):
        b = book(n, 10_000.0)
        b.spec["sizing"] = {"method": "fixed", "dollars": 40.0,
                            "max_bet_dollars": 40.0}
        b.spec["entry"] = ALWAYS
        books.append(b)
    bars = with_fill_bar([bar(ts=0, volume=100_000),
                          bar(ts=86400, result="yes", close_time=86400)])
    _, rej = engine.run_book(books, bars, costs=NO_COST,
                             pm_caps={"daily_spend_dollars": 60.0})
    assert rej.get("pm_daily_cap", 0) > 0


def test_pm_exposure_cap_binds_across_agents():
    books = []
    for n in ("a", "b", "c"):
        b = book(n, 10_000.0)
        b.spec["sizing"] = {"method": "fixed", "dollars": 40.0,
                            "max_bet_dollars": 40.0}
        b.spec["entry"] = ALWAYS
        books.append(b)
    bars = with_fill_bar([bar(ticker=f"M{i}", ts=0, close_time=90 * 86400)
                          for i in range(6)])
    _, rej = engine.run_book(books, bars, costs=NO_COST,
                             pm_caps={"total_exposure_dollars": 50.0})
    assert rej.get("pm_exposure_cap", 0) > 0


def test_without_pm_caps_only_the_agents_own_limits_apply():
    books = [book("a", 10_000.0)]
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    _, rej = engine.run_book(books, bars, costs=NO_COST)
    assert "pm_daily_cap" not in rej and "pm_exposure_cap" not in rej


# ── the view is shared WITHIN a PM, and only within it ────────────────────────

def test_all_agents_of_one_pm_read_the_same_view():
    """A desk has one house opinion. Its traders differ in how they act on it,
    not in what they believe — that is what makes them a desk rather than a
    firm."""
    seen = []
    b1, b2 = book("a"), book("b")
    for b in (b1, b2):
        b.spec["entry"] = {"all": [{"signal": "edge", "op": "gt", "value": 0.10}]}
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    probs = {("M1", 0): 0.90, ("M1", 60): 0.90}
    trades, _ = engine.run_book([b1, b2], bars, costs=NO_COST, model_probs=probs)
    assert len(trades) == 2
    assert all(t["model_prob"] == 0.90 for t in trades)


# ── single-strategy run is the same code path ─────────────────────────────────

def test_run_is_a_wrapper_over_run_book():
    """A separate single-strategy loop would be a second implementation to
    drift. These must agree exactly."""
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    a, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)
    b, _ = engine.run_book([engine.Book("_", spec(), 1000.0)], bars, costs=NO_COST)
    assert [t["net_pnl"] for t in a] == [t["net_pnl"] for t in b]


# ── settlement at end of run ──────────────────────────────────────────────────

def test_a_resolved_market_settles_rather_than_marking_to_market():
    """Kalshi's archive stops AT close_time, so the last bar is always just
    inside it and the normal settle branch never fires. Paying out at the last
    quote would discard the outcome — and with it P&L, Brier and calibration."""
    bars = with_fill_bar([bar(ts=0, close_time=86400),
                          bar(ts=86000, close_time=86400, result="yes")])
    trades, _ = engine.run_book([book("a")], bars, costs=NO_COST)
    t = trades[0]
    assert t["settled"] is True
    assert t.get("liquidated_at_end") is not True
    assert t["exit_price"] == pytest.approx(1.0)
    assert t["outcome"] == 1


def test_an_unfinished_market_is_still_marked_to_market():
    """Only a genuinely resolved market settles. Data ending mid-life must not
    invent an outcome."""
    bars = with_fill_bar([bar(ts=0, close_time=900 * 86400),
                          bar(ts=86400, close_time=900 * 86400)])
    trades, rej = engine.run_book([book("a")], bars, costs=NO_COST)
    assert trades[0]["liquidated_at_end"] is True
    assert rej.get("open_at_end") == 1


def test_a_resolved_void_settles_at_its_fair_price():
    b = bar(ts=86000, close_time=86400)
    b["result"] = "void"
    b["settlement_value"] = 0.40
    trades, _ = engine.run_book([book("a")],
                                with_fill_bar([bar(ts=0, close_time=86400), b]),
                                costs=NO_COST)
    assert trades[0]["voided"] is True
    assert trades[0]["exit_price"] == pytest.approx(0.40)
    assert trades[0]["outcome"] is None
