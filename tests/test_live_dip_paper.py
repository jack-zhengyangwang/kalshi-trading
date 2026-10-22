"""Dip catcher paper bot: the per-leg state machine (docs/findings/13_DIP_PAPER_PLAN.md)."""
from wc.live import dip_paper as dp

S = 1000                       # ms per second
KO = 10_000 * S                # kickoff, ms


def _leg(ref=50, bid=49, ask=51):
    leg = dp.Leg("T", kickoff_ms=KO)
    leg.on_quote(bid, ask, 0)
    leg.on_trade(ref, 0)
    return leg


def _kinds(events):
    return [e["event"] for e in events]


def test_arms_a_buy_two_below_the_last_trade():
    leg = _leg(ref=50)
    ev = leg.tick(1 * S)
    assert _kinds(ev) == ["post_buy"] and leg.buy_px == 48


def test_buy_never_at_or_above_the_ask():
    leg = _leg(ref=50, bid=46, ask=47)          # last trade above a fallen ask
    leg.tick(1 * S)
    assert leg.buy_px == 46                      # capped at ask - 1


def test_no_arming_outside_5_to_95_or_after_cutoff():
    assert _leg(ref=4, bid=3, ask=5).tick(1 * S) == []
    assert _leg(ref=96, bid=95, ask=97).tick(1 * S) == []
    late = KO + dp.NO_NEW_AFTER_KO_MIN * 60 * S
    assert _leg().tick(late) == []


def test_trade_at_our_price_is_not_a_fill_but_below_is():
    leg = _leg(ref=50)
    leg.tick(1 * S)
    assert leg.on_trade(48, 2 * S) == []                 # queue ahead of us at 48
    ev = leg.on_trade(47, 3 * S)
    assert _kinds(ev) == ["buy_fill", "post_sell"]
    assert leg.state == "long" and leg.sell_px == 53


def test_trades_before_the_post_do_not_fill():
    leg = _leg(ref=50)
    leg.tick(5 * S)
    assert leg.on_trade(40, 4 * S) == []                 # printed before we posted


def test_ask_dropping_to_our_price_fills():
    leg = _leg(ref=50)
    leg.tick(1 * S)
    assert _kinds(leg.on_quote(47, 48, 2 * S))[0] == "buy_fill"


def test_repeg_follows_the_price_only_every_60s():
    leg = _leg(ref=50)
    leg.tick(1 * S)
    leg.on_quote(54, 56, 2 * S)                          # the book moves up
    leg.on_trade(55, 2 * S)                              # a print far above: no fill
    assert leg.tick(30 * S) == []                        # too soon to re-peg
    ev = leg.tick(61 * S)
    assert _kinds(ev) == ["post_buy"] and leg.buy_px == 53


def test_sell_fills_above_target_and_round_trip_counts():
    leg = _leg(ref=50)
    leg.tick(1 * S)
    leg.on_trade(47, 2 * S)                              # bought at 48
    assert leg.on_trade(53, 3 * S) == []                 # at the target: not yet
    ev = leg.on_trade(54, 4 * S)
    assert _kinds(ev) == ["sell_fill"]
    assert leg.state == "flat" and leg.trips == 1
    assert ev[0]["pnl_c"] == 5


def test_bid_rising_to_target_fills_the_sell():
    leg = _leg(ref=50)
    leg.tick(1 * S)
    leg.on_trade(47, 2 * S)
    assert _kinds(leg.on_quote(53, 54, 3 * S)) == ["sell_fill"]


def test_phase_tags_pre_game_and_in_play():
    leg = _leg(ref=50)
    leg.tick(KO - 10 * S)
    assert leg.on_trade(40, KO - 5 * S)[0]["phase"] == "pre"
    leg2 = _leg(ref=50)
    leg2.tick(KO + 1 * S)
    assert leg2.on_trade(40, KO + 2 * S)[0]["phase"] == "in"


def test_at_most_three_round_trips():
    leg = _leg(ref=50)
    t = 1
    for _ in range(3):
        leg.tick(t * S)
        leg.on_trade(leg.buy_px - 1, (t + 1) * S)
        leg.on_trade(leg.sell_px + 1, (t + 2) * S)
        leg.on_trade(50, (t + 3) * S)                    # back to 50 for the next arm
        t += 100
    assert leg.trips == 3 and leg.tick(t * S) == []


def test_cutoff_cancels_a_resting_buy_but_keeps_a_position():
    leg = _leg(ref=50)
    leg.tick(1 * S)
    late = KO + dp.NO_NEW_AFTER_KO_MIN * 60 * S
    assert _kinds(leg.tick(late)) == ["cancel_buy"] and leg.state == "flat"
    leg2 = _leg(ref=50)
    leg2.tick(1 * S)
    leg2.on_trade(47, 2 * S)
    assert leg2.tick(late) == [] and leg2.state == "long"   # sell keeps resting


def test_settle_open_position_and_summary():
    events = [
        {"event": "buy_fill", "ticker": "A", "px": 48, "phase": "pre", "ts": 1},
        {"event": "sell_fill", "ticker": "A", "px": 53, "pnl_c": 5, "phase": "pre", "ts": 2},
        {"event": "buy_fill", "ticker": "B", "px": 40, "phase": "in", "ts": 3},
        {"event": "buy_fill", "ticker": "C", "px": 30, "phase": "in", "ts": 4},
    ]
    rep = dp.summarize(events, {"A": "no", "B": "yes", "C": "no"})
    assert rep["buy_fills"] == 3 and rep["round_trips"] == 1
    assert rep["net_c"] == 5 + (100 - 40) + (0 - 30)
    assert rep["by_phase"]["in"]["net_c"] == 30
    assert rep["unsold"] == 2


def test_parse_messages():
    q = dp.parse({"type": "ticker", "msg": {"market_ticker": "T", "yes_bid_dollars": "0.6400",
                                            "yes_ask_dollars": "0.6500", "ts_ms": 7}})
    assert q == ("quote", "T", 64, 65, 7)
    t = dp.parse({"type": "trade", "msg": {"market_ticker": "T", "yes_price_dollars": "0.4400",
                                           "ts_ms": 9}})
    assert t == ("trade", "T", 44, 9)
    assert dp.parse({"type": "subscribed", "msg": {}}) is None
