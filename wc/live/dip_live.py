"""Dip catcher with REAL orders. Same rules as the paper bot (wc/live/dip_paper.py,
docs/findings/13_DIP_PAPER_PLAN.md); only the fills differ: they are Kalshi's,
read from /portfolio/fills, not simulated.

Safety:
  * nothing is sent without --live (otherwise it logs what it would do)
  * every order is post_only GTC, 1 contract: it rests, it never takes
  * hard cap: every buy order that may still rest or fill + cost of contracts
    held <= --max-dollars. An order id is forgotten only when Kalshi confirms it
    is cancelled or filled, so an ambiguous error can't hide exposure
  * one resting sell per contract held, retried until it rests
  * refuses to start if the account already has resting orders
  * kill file logs/dip_live.KILL, SIGTERM, end of games or a crash -> cancel
    every resting order on the account, then exit

    python -m wc.live.dip_live --date 2026-10-22 --max-dollars 30 --live
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import threading
import time
import traceback
import uuid
from collections import defaultdict

from wc.live import dip_paper as dp

LOGS = dp.LOGS
KILL = os.path.join(LOGS, "dip_live.KILL")
TICK_S = 2
PENDING_TIMEOUT_MS = 30_000
BACKOFF_MS = 30_000          # pause a leg after an order error
STALE_MS = 120_000           # no quote/trade for 2 min: stop quoting the leg
WRITE_GAP_S = 0.15           # <= ~7 order calls/s (basic tier allows ~10)


def target(ref, ask):
    """Same pricing as the paper bot: last trade - 2c, never at/above the ask."""
    if ref is None or not dp.MIN_REF <= ref <= dp.MAX_REF:
        return None
    px = ref - dp.OFFSET_C
    if ask is not None:
        px = min(px, ask - 1)
    return px if px >= 1 else None


class LiveLeg:
    """Buy side: flat -> bid (resting buy) -> flat, with 'pending' when a cancel
    found nothing left (the buy filled; its fill is on the way).
    Sell side: `held` contracts, each with one resting sell in `sell_oids`."""

    def __init__(self, ticker, kickoff_ms):
        self.ticker, self.ko = ticker, kickoff_ms
        self.cutoff = kickoff_ms + dp.NO_NEW_AFTER_KO_MIN * 60_000
        self.state = "flat"
        self.ref = self.bid = self.ask = None
        self.seen_ms = 0
        self.buy_oid = self.buy_px = self.buy_post = None
        self.pending_since = None
        self.held = 0
        self.held_cost = 0
        self.sell_oids = set()
        self.trips = 0
        self.backoff_until = 0

    def avg_cost(self):
        return self.held_cost // self.held if self.held else 0


class Executor:
    def __init__(self, api, legs, max_cents, live, log, write_gap_s=0.0):
        self.api, self.legs, self.max_cents = api, legs, max_cents
        self.live, self.log, self.write_gap_s = live, log, write_gap_s
        self.by_oid = {}             # order_id -> (ticker, "buy" | "sell", px)

    def committed(self):
        """Cents at risk: every buy order not yet confirmed done (orphans too)
        plus the cost of contracts held."""
        return (sum(px for _, kind, px in self.by_oid.values() if kind == "buy")
                + sum(L.held_cost for L in self.legs.values()))

    def _write(self):
        if self.write_gap_s:
            time.sleep(self.write_gap_s)

    # ── raw order calls ──
    def _place(self, L, side, px, now):
        cid = str(uuid.uuid4())
        body = {"ticker": L.ticker, "side": side, "count": "1.00",
                "price": f"{px / 100:.4f}", "time_in_force": "good_till_canceled",
                "post_only": True, "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": cid}
        kind = "buy" if side == "bid" else "sell"
        if not self.live:
            oid = "dry-" + cid[:8]
            self.log("place", L, now, side=side, px=px, oid=oid, dry=True)
            self.by_oid[oid] = (L.ticker, kind, px)
            return oid
        self._write()
        try:
            r = self.api.post("/portfolio/events/orders", body)
        except Exception as e:
            self.log("place_error", L, now, side=side, px=px, error=str(e)[:300])
            L.backoff_until = now + BACKOFF_MS
            oid = self._find_order(L, px, now)     # a timed-out POST may have landed
            if oid:
                self.by_oid[oid] = (L.ticker, kind, px)
                self.log("place_recovered", L, now, side=side, px=px, oid=oid)
            return oid
        o = r.get("order") or r
        oid = o.get("order_id")
        self.log("place", L, now, side=side, px=px, oid=oid, fill_count=o.get("fill_count"))
        if oid:
            self.by_oid[oid] = (L.ticker, kind, px)
        return oid

    def _find_order(self, L, px, now):
        """A resting order on this leg at this price that we don't track yet."""
        try:
            r = self.api.get("/portfolio/orders", {"ticker": L.ticker, "status": "resting"})
        except Exception as e:
            self.log("lookup_error", L, now, error=str(e)[:200])
            return None
        for o in r.get("orders") or []:
            oid = o.get("order_id")
            p = o.get("yes_price_dollars") or o.get("price_dollars") or o.get("price")
            if oid not in self.by_oid and p is not None and dp._c(p) == px:
                return oid
        return None

    def _status(self, oid):
        """Kalshi's status for an order: resting / canceled / executed / None."""
        try:
            r = self.api.get(f"/portfolio/orders/{oid}")
            return (r.get("order") or r).get("status")
        except Exception:
            return None

    def _cancel(self, L, oid, now):
        """'cancelled' (gone, unfilled), 'filled' (nothing left; the fill is
        coming) or 'unknown' (may still rest: keep tracking, try again)."""
        if not self.live or str(oid).startswith("dry-"):
            self.log("cancel", L, now, oid=oid, dry=True)
            self.by_oid.pop(oid, None)
            return "cancelled"
        self._write()
        try:
            r = self.api.delete(f"/portfolio/events/orders/{oid}")
            reduced = float(r.get("reduced_by") or 0)
        except Exception as e:
            self.log("cancel_error", L, now, oid=oid, error=str(e)[:200])
            st = self._status(oid)
            if st == "canceled":
                self.by_oid.pop(oid, None)
                return "cancelled"
            return "filled" if st == "executed" else "unknown"
        self.log("cancel", L, now, oid=oid, reduced_by=reduced)
        if reduced > 0:
            self.by_oid.pop(oid, None)
            return "cancelled"
        return "filled"

    def _drop_buy(self, L, now):
        """Cancel the resting buy. True only if it is confirmed gone unfilled."""
        res = self._cancel(L, L.buy_oid, now)
        if res == "cancelled":
            L.state, L.buy_oid, L.buy_px = "flat", None, None
            return True
        if res == "filled":
            L.state, L.pending_since = "pending", now
        else:
            L.backoff_until = now + BACKOFF_MS        # 'unknown': stay 'bid', retry
        return False

    def _post_sells(self, L, now):
        """One resting sell per contract held, at avg cost + 5c. If the bid has
        already reached that, a post-only sell would cross and be rejected, so
        rest one tick above the bid instead (still a maker order)."""
        while len(L.sell_oids) < L.held:
            px = min(99, L.avg_cost() + dp.TARGET_C)
            if L.bid is not None and L.bid >= px:
                px = min(99, L.bid + 1)
            oid = self._place(L, "ask", px, now)
            if not oid:
                return                               # retried next tick
            L.sell_oids.add(oid)

    # ── decisions (same rules as the paper Leg.tick) ──
    def tick(self, now):
        for L in self.legs.values():
            if now < L.backoff_until:
                continue
            if len(L.sell_oids) < L.held:
                self._post_sells(L, now)
            if L.state == "pending":
                if now - L.pending_since <= PENDING_TIMEOUT_MS:
                    continue
                st = self._status(L.buy_oid)
                if st == "canceled":
                    self.by_oid.pop(L.buy_oid, None)
                    self.log("pending_cancelled", L, now)
                    L.state, L.buy_oid, L.buy_px = "flat", None, None
                else:                                # executed / unknown: keep waiting
                    L.pending_since = now
                    self.log("pending_wait", L, now, status=st)
                continue
            if now >= L.cutoff or now - L.seen_ms > STALE_MS:
                if L.state == "bid":
                    self._drop_buy(L, now)
                continue
            if L.held:                               # one position at a time
                continue
            if L.state == "flat":
                px = target(L.ref, L.ask)
                if px is None or L.trips >= dp.MAX_TRIPS or self.committed() + px > self.max_cents:
                    continue
                oid = self._place(L, "bid", px, now)
                if oid:
                    L.state, L.buy_oid, L.buy_px, L.buy_post = "bid", oid, px, now
            elif L.state == "bid" and now - L.buy_post >= dp.REPEG_MS:
                px = target(L.ref, L.ask)
                if px == L.buy_px:
                    continue                         # unchanged price keeps its queue place
                if not self._drop_buy(L, now):
                    continue
                if px is not None and self.committed() + px <= self.max_cents:
                    oid = self._place(L, "bid", px, now)
                    if oid:
                        L.state, L.buy_oid, L.buy_px, L.buy_post = "bid", oid, px, now

    def on_fill(self, fill, now):
        oid = fill.get("order_id")
        if oid not in self.by_oid:
            return
        tk, kind, order_px = self.by_oid.pop(oid)
        L = self.legs[tk]
        px = dp._c(fill.get("yes_price_dollars")) or order_px
        if kind == "buy":
            L.held += 1
            L.held_cost += px
            if oid == L.buy_oid:
                L.state, L.buy_oid, L.buy_px = "flat", None, None
            else:
                self.log("orphan_buy_fill", L, now, px=px, oid=oid)
                if L.state == "bid":                 # one position at a time
                    self._drop_buy(L, now)
            self.log("buy_fill", L, now, px=px, oid=oid, is_taker=fill.get("is_taker"))
            self._post_sells(L, now)
        else:
            cost = L.avg_cost()
            L.held -= 1
            L.held_cost -= cost
            L.sell_oids.discard(oid)
            self.log("sell_fill", L, now, px=px, buy_px=cost, pnl_c=px - cost, oid=oid)
            if L.held == 0:
                L.trips += 1


class API:
    """Signed REST over the existing client, plus DELETE."""

    def __init__(self, client):
        self.c = client

    def get(self, path, params=None):
        return self.c._get(path, params=params)

    def post(self, path, body):
        return self.c._post(path, body)

    def delete(self, path):
        headers = self.c._auth_headers("DELETE", path)
        r = self.c.session.delete(f"{self.c.base_url}{path}", headers=headers, timeout=10)
        r.raise_for_status()
        return r.json() if r.content else {}


def poll_fills(api, since_s, seen):
    """Every fill since `since_s` not seen before, oldest first, following the
    cursor so no page is missed. Returns (fills, newest ts seen)."""
    out, cursor, newest = [], None, since_s
    for _ in range(20):
        params = {"min_ts": since_s, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = api.get("/portfolio/fills", params)
        for f in r.get("fills") or []:
            newest = max(newest, int(f.get("ts") or 0))
            if f.get("fill_id") not in seen:
                seen.add(f.get("fill_id"))
                out.append(f)
        cursor = r.get("cursor")
        if not cursor or not r.get("fills"):
            break
    return sorted(out, key=lambda f: f.get("ts") or 0), newest


def cancel_all(api, tries=3):
    for i in range(tries):
        try:
            api.delete("/portfolio/events/orders")
            left = api.get("/portfolio/orders", {"status": "resting"}).get("orders") or []
            if not left:
                return True
        except Exception as e:
            print(f"[dip-live] cancel-all attempt {i + 1} failed: {e}", flush=True)
        time.sleep(2)
    return False


def run(date, max_dollars, live):
    from wc.kalshi.client_ext import KalshiClientV2, KalshiWS
    client = KalshiClientV2()
    api = API(client)
    if os.path.exists(KILL):
        raise SystemExit(f"{KILL} exists: remove it to start")
    if live:
        resting = api.get("/portfolio/orders", {"status": "resting"}).get("orders") or []
        if resting:
            raise SystemExit(f"{len(resting)} resting orders on the account: another "
                             "instance running? Cancel them first.")
    kickoffs, init = dp.discover(client, date)
    now0 = int(time.time() * 1000)
    legs = {tk: LiveLeg(tk, ko) for tk, ko in kickoffs.items()
            if ko + dp.NO_NEW_AFTER_KO_MIN * 60_000 > now0}
    if not legs:
        raise SystemExit("no legs still open for new buys")
    if live:
        pos = api.get("/portfolio/positions", {"limit": 1000}).get("market_positions") or []
        held = [p["ticker"] for p in pos
                if p.get("ticker") in legs and float(p.get("position_fp") or 0)]
        if held:
            raise SystemExit(f"already holding {held}: refusing to start blind")
    for tk, (bid, ask, last) in init.items():
        if tk in legs:
            L = legs[tk]
            L.bid, L.ask, L.ref, L.seen_ms = bid, ask, last, now0
    end_ms = max(L.ko for L in legs.values()) + dp.KICKOFF_BEFORE_OCC_MS + 30 * 60_000

    os.makedirs(LOGS, exist_ok=True)
    f = open(os.path.join(LOGS, f"dip_live_{date}.jsonl"), "a")
    stats = defaultdict(int)

    def log(event, L, ts, **kw):
        e = {"event": event, "ticker": L.ticker if L else None, "ts": ts,
             "phase": ("pre" if ts < L.ko else "in") if L else None,
             "ref": L.ref if L else None, "bid": L.bid if L else None,
             "ask": L.ask if L else None, "live": live, **kw}
        f.write(json.dumps(e) + "\n")
        f.flush()
        stats[event] += 1

    ex = Executor(api, legs, int(max_dollars * 100), live, log, WRITE_GAP_S)

    class Feed(KalshiWS):
        # No lock: the feed only overwrites a leg's latest numbers, so order
        # calls in the main loop can never stall it.
        def _on_message(self, _ws, raw):
            try:
                p = dp.parse(json.loads(raw))
            except Exception:
                return
            if not p or p[1] not in legs:
                return
            L = legs[p[1]]
            if p[0] == "quote":
                if p[2] is not None:
                    L.bid = p[2]
                if p[3] is not None:
                    L.ask = p[3]
            elif p[2] is not None:
                L.ref = p[2]
            L.seen_ms = int(time.time() * 1000)

    stop = threading.Event()

    def feed_loop():
        while not stop.is_set():
            ws = Feed(client)
            ws.run(list(legs), ("ticker", "trade"))
            if not stop.is_set():
                log("ws_gap", None, int(time.time() * 1000), error=str(ws.error)[:200])
                time.sleep(5)

    def on_term(signum, _frame):
        raise SystemExit(f"signal {signum}")

    signal.signal(signal.SIGTERM, on_term)       # SIGHUP stays ignored (nohup): a
                                                 # dropped ssh session must not stop it

    threading.Thread(target=feed_loop, daemon=True).start()
    print(f"[dip-live] {date}: {len(legs)} legs, cap ${max_dollars:.2f}, "
          f"{'LIVE ORDERS' if live else 'DRY RUN'}, until "
          f"{dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc):%H:%M} UTC", flush=True)
    time.sleep(3)                                    # let the feed refresh quotes first
    since_s, seen, last_status = int(time.time()) - 60, set(), 0
    try:
        while (now := int(time.time() * 1000)) < end_ms:
            if os.path.exists(KILL):
                print("[dip-live] KILL file found", flush=True)
                break
            if live:
                try:
                    fills, newest = poll_fills(api, since_s, seen)
                    since_s = max(since_s, newest - 60)      # overlap; `seen` dedupes
                    for fill in fills:
                        ex.on_fill(fill, now)
                except Exception as e:
                    log("fills_error", None, now, error=str(e)[:200])
            ex.tick(now)
            if now - last_status >= 300_000:
                print(f"[dip-live] {dt.datetime.now(dt.timezone.utc):%H:%M} committed="
                      f"{ex.committed()}c " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())),
                      flush=True)
                last_status = now
            time.sleep(TICK_S)
    except BaseException:
        traceback.print_exc()
    finally:
        stop.set()
        if live:
            ok = cancel_all(api)
            print(f"[dip-live] cancel-all {'OK' if ok else 'FAILED: CHECK THE ACCOUNT'}",
                  flush=True)
        log("exit", None, int(time.time() * 1000), committed=ex.committed())
        f.close()
        print("[dip-live] done", dict(stats), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", required=True)
    ap.add_argument("--max-dollars", type=float, required=True)
    ap.add_argument("--live", action="store_true", help="send real orders")
    a = ap.parse_args()
    run(a.date, a.max_dollars, a.live)


if __name__ == "__main__":
    main()
