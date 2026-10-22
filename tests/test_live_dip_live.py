"""Real-order dip catcher: decisions against a fake Kalshi API."""
from wc.live import dip_live as dl

S = 1000
KO = 10_000 * S


class FakeAPI:
    def __init__(self):
        self.posts, self.deletes, self.n = [], [], 0
        self.cancel_reduces = 1.0

    def post(self, path, body):
        self.n += 1
        self.posts.append(body)
        return {"order_id": f"o{self.n}", "fill_count": "0.00"}

    def delete(self, path):
        self.deletes.append(path)
        return {"reduced_by": str(self.cancel_reduces)}


def _ex(n_legs=1, cap=3000, ref=50, bid=49, ask=51):
    legs = {}
    for i in range(n_legs):
        L = dl.LiveLeg(f"T{i}", KO)
        L.ref, L.bid, L.ask = ref, bid, ask
        legs[L.ticker] = L
    events = []
    api = FakeAPI()
    ex = dl.Executor(api, legs, cap, True, lambda e, L, ts, **kw: events.append((e, kw)))
    return ex, api, legs, events


def test_posts_post_only_gtc_one_contract_two_below():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    b = api.posts[0]
    assert (b["side"], b["price"], b["count"]) == ("bid", "0.4800", "1.00")
    assert b["post_only"] is True and b["time_in_force"] == "good_till_canceled"
    assert legs["T0"].state == "bid"


def test_cap_limits_how_many_legs_get_orders():
    ex, api, legs, _ = _ex(n_legs=5, cap=100)       # 48c each: only 2 fit in $1
    ex.tick(1 * S)
    assert len(api.posts) == 2 and ex.committed() == 96


def test_repeg_cancels_then_replaces():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    legs["T0"].ref, legs["T0"].ask = 55, 56
    ex.tick(30 * S)
    assert len(api.posts) == 1                      # too soon
    ex.tick(62 * S)
    assert api.deletes == ["/portfolio/events/orders/o1"]
    assert api.posts[-1]["price"] == "0.5300" and legs["T0"].buy_oid == "o2"


def test_cancel_that_finds_nothing_waits_for_the_fill():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    legs["T0"].ref, legs["T0"].ask = 55, 56
    api.cancel_reduces = 0.0                        # it had already filled
    ex.tick(62 * S)
    assert legs["T0"].state == "pending" and len(api.posts) == 1
    ex.on_fill({"order_id": "o1", "yes_price_dollars": "0.4800"}, 63 * S)
    assert legs["T0"].state == "long" and api.posts[-1]["side"] == "ask"


def test_fill_rests_a_sell_five_above_and_round_trip_counts():
    ex, api, legs, ev = _ex()
    ex.tick(1 * S)
    ex.on_fill({"order_id": "o1", "yes_price_dollars": "0.4800"}, 2 * S)
    s = api.posts[-1]
    assert (s["side"], s["price"], s["post_only"]) == ("ask", "0.5300", True)
    assert ex.committed() == 48                     # held at cost
    ex.on_fill({"order_id": "o2", "yes_price_dollars": "0.5300"}, 3 * S)
    assert legs["T0"].state == "flat" and legs["T0"].trips == 1
    assert ("sell_fill", ) == (ev[-1][0], ) and ev[-1][1]["pnl_c"] == 5


def test_unknown_fills_are_ignored():
    ex, api, legs, _ = _ex()
    ex.on_fill({"order_id": "someone-else", "yes_price_dollars": "0.10"}, 1 * S)
    assert legs["T0"].state == "flat"


def test_cutoff_cancels_resting_buy():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    ex.tick(legs["T0"].cutoff)
    assert api.deletes and legs["T0"].state == "flat"
