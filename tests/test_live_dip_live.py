"""Real-order dip catcher: decisions against a fake Kalshi API, including the
error paths a reviewer flagged (failed cancels, failed sells, lost POSTs)."""
from wc.live import dip_live as dl

S = 1000
KO = 10_000 * S


class FakeAPI:
    def __init__(self):
        self.posts, self.deletes, self.n = [], [], 0
        self.cancel_reduces = 1.0
        self.cancel_raises = False
        self.post_raises = 0                 # raise on the next N posts
        self.status = {}                     # oid -> status for GET /portfolio/orders/{oid}
        self.resting = []                    # for GET /portfolio/orders?ticker=..

    def post(self, path, body):
        if self.post_raises:
            self.post_raises -= 1
            raise TimeoutError("read timed out")
        self.n += 1
        self.posts.append(body)
        return {"order_id": f"o{self.n}", "fill_count": "0.00"}

    def delete(self, path):
        self.deletes.append(path)
        if self.cancel_raises:
            raise RuntimeError("429 Too Many Requests")
        return {"reduced_by": str(self.cancel_reduces)}

    def get(self, path, params=None):
        if path.startswith("/portfolio/orders/"):
            return {"order": {"status": self.status.get(path.rsplit("/", 1)[1])}}
        if path == "/portfolio/orders":
            return {"orders": self.resting}
        raise AssertionError(path)


def _ex(n_legs=1, cap=3000, ref=50, bid=49, ask=51):
    legs = {}
    for i in range(n_legs):
        L = dl.LiveLeg(f"T{i}", KO)
        L.ref, L.bid, L.ask, L.seen_ms = ref, bid, ask, 0
        legs[L.ticker] = L
    events = []
    api = FakeAPI()
    ex = dl.Executor(api, legs, cap, True, lambda e, L, ts, **kw: events.append((e, kw)))
    return ex, api, legs, events


def _fill(oid, px):
    return {"order_id": oid, "yes_price_dollars": f"{px / 100:.4f}"}


# ── happy paths ──

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


def test_fill_rests_a_sell_five_above_and_round_trip_counts():
    ex, api, legs, ev = _ex()
    ex.tick(1 * S)
    ex.on_fill(_fill("o1", 48), 2 * S)
    s = api.posts[-1]
    assert (s["side"], s["price"], s["post_only"]) == ("ask", "0.5300", True)
    assert ex.committed() == 48                     # held at cost, no buy left
    ex.tick(3 * S)
    assert len(api.posts) == 2                      # no new buy while holding
    ex.on_fill(_fill("o2", 53), 4 * S)
    L = legs["T0"]
    assert L.held == 0 and L.trips == 1 and ex.committed() == 0
    assert ev[-1][0] == "sell_fill" and ev[-1][1]["pnl_c"] == 5


def test_unknown_fills_are_ignored():
    ex, api, legs, _ = _ex()
    ex.on_fill(_fill("someone-else", 10), 1 * S)
    assert legs["T0"].held == 0


def test_cutoff_cancels_resting_buy():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    legs["T0"].seen_ms = legs["T0"].cutoff
    ex.tick(legs["T0"].cutoff)
    assert api.deletes and legs["T0"].state == "flat"


def test_stale_feed_pulls_the_buy_and_stops_quoting():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    ex.tick(dl.STALE_MS + 2 * S)                    # nothing from the feed for 2 min
    assert api.deletes and legs["T0"].state == "flat"
    ex.tick(dl.STALE_MS + 4 * S)
    assert len(api.posts) == 1                      # no new buy on stale data


# ── review finding 1: a failed cancel must not look like a fill ──

def test_cancel_error_with_order_still_resting_keeps_tracking_it():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    legs["T0"].ref, legs["T0"].ask = 55, 56
    api.cancel_raises, api.status["o1"] = True, "resting"
    ex.tick(62 * S)
    L = legs["T0"]
    assert L.state == "bid" and L.buy_oid == "o1" and len(api.posts) == 1
    assert ex.committed() == 48                     # still counted against the cap
    api.cancel_raises = False
    ex.tick(62 * S + dl.BACKOFF_MS + 1)             # retried after the backoff
    assert L.buy_oid == "o2" and ex.committed() == 53


def test_cancel_error_on_a_filled_order_waits_for_the_fill():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    legs["T0"].ref, legs["T0"].ask = 55, 56
    api.cancel_raises, api.status["o1"] = True, "executed"
    ex.tick(62 * S)
    assert legs["T0"].state == "pending" and len(api.posts) == 1
    ex.on_fill(_fill("o1", 48), 63 * S)
    assert legs["T0"].held == 1 and api.posts[-1]["side"] == "ask"


def test_pending_never_forgets_an_unconfirmed_order():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    legs["T0"].ref, legs["T0"].ask = 55, 56
    api.cancel_reduces = 0.0                        # nothing left to cancel
    ex.tick(62 * S)
    t = 62 * S + dl.PENDING_TIMEOUT_MS + 1
    api.status["o1"] = None                         # status lookup fails
    ex.tick(t)
    assert legs["T0"].state == "pending" and "o1" in ex.by_oid
    api.status["o1"] = "canceled"
    ex.tick(t + dl.PENDING_TIMEOUT_MS + 1)
    assert legs["T0"].state == "flat" and "o1" not in ex.by_oid


def test_orphan_fill_counts_against_cap_and_gets_a_sell():
    ex, api, legs, ev = _ex()
    ex.tick(1 * S)                                  # o1 at 48
    ex.by_oid["old"] = ("T0", "buy", 46)            # an older buy we lost track of
    assert ex.committed() == 48 + 46
    ex.on_fill(_fill("old", 46), 2 * S)
    L = legs["T0"]
    assert ("orphan_buy_fill" in [e for e, _ in ev]) and L.held == 1
    assert api.deletes == ["/portfolio/events/orders/o1"]   # one position at a time
    assert api.posts[-1]["side"] == "ask" and api.posts[-1]["price"] == "0.5100"


# ── review finding 2: the sell must be retried until it rests ──

def test_failed_sell_is_retried():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    api.post_raises = 1
    ex.on_fill(_fill("o1", 48), 2 * S)
    L = legs["T0"]
    assert L.held == 1 and not L.sell_oids
    ex.tick(2 * S + dl.BACKOFF_MS + 1)
    assert len(L.sell_oids) == 1 and api.posts[-1]["side"] == "ask"


def test_sell_rests_above_the_bid_when_the_target_would_cross():
    ex, api, legs, _ = _ex()
    ex.tick(1 * S)
    legs["T0"].bid = 55                             # already bounced past 53
    ex.on_fill(_fill("o1", 48), 2 * S)
    assert api.posts[-1]["side"] == "ask" and api.posts[-1]["price"] == "0.5600"


# ── review finding 3: a timed-out POST may have created the order ──

def test_lost_post_is_recovered_from_resting_orders():
    ex, api, legs, ev = _ex()
    api.post_raises = 1
    api.resting = [{"order_id": "ghost", "yes_price_dollars": "0.4800"}]
    ex.tick(1 * S)
    assert legs["T0"].buy_oid == "ghost" and ex.by_oid["ghost"][1] == "buy"
    assert "place_recovered" in [e for e, _ in ev]


# ── fills polling ──

def test_poll_fills_follows_cursor_and_dedupes():
    pages = [{"fills": [{"fill_id": "a", "ts": 5}, {"fill_id": "b", "ts": 6}], "cursor": "c1"},
             {"fills": [{"fill_id": "c", "ts": 7}], "cursor": None}]

    class P:
        def get(self, path, params=None):
            return pages[1] if params.get("cursor") else pages[0]
    seen = {"a"}
    fills, newest = dl.poll_fills(P(), 0, seen)
    assert [f["fill_id"] for f in fills] == ["b", "c"] and newest == 7
