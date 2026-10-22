"""Dip catcher with REAL orders. Same rules as the paper bot (wc/live/dip_paper.py,
docs/findings/13_DIP_PAPER_PLAN.md); only the fills differ: they are Kalshi's,
read from /portfolio/fills, not simulated.

Safety:
  * nothing is sent without --live (otherwise it logs what it would do)
  * every order is post_only GTC, 1 contract: it rests, it never takes
  * hard cap: resting buys + positions held <= --max-dollars (cost basis)
  * kill file logs/dip_live.KILL -> cancel every resting order and exit
  * on exit (end of games, kill, crash) every resting order is cancelled

    python -m wc.live.dip_live --date 2026-10-22 --max-dollars 30 --live
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
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


def target(ref, ask):
    """Same pricing as the paper bot: last trade - 2c, never at/above the ask."""
    if ref is None or not dp.MIN_REF <= ref <= dp.MAX_REF:
        return None
    px = ref - dp.OFFSET_C
    if ask is not None:
        px = min(px, ask - 1)
    return px if px >= 1 else None


class LiveLeg:
    """flat -> bid (resting buy) -> long (resting sell) -> flat.
    'pending' = our buy cancel found nothing to cancel (it filled or vanished);
    we wait for the fill to show up in /portfolio/fills."""

    def __init__(self, ticker, kickoff_ms):
        self.ticker, self.ko = ticker, kickoff_ms
        self.cutoff = kickoff_ms + dp.NO_NEW_AFTER_KO_MIN * 60_000
        self.state = "flat"
        self.ref = self.bid = self.ask = None
        self.buy_oid = self.buy_px = self.buy_post = None
        self.sell_oid = self.sell_px = None
        self.trips = 0
        self.pending_since = None


class Executor:
    def __init__(self, api, legs, max_cents, live, log):
        self.api, self.legs, self.max_cents = api, legs, max_cents
        self.live, self.log = live, log
        self.by_oid = {}                 # order_id -> (ticker, "buy" | "sell")

    def committed(self):
        """Cents tied up: resting buys + cost of positions held."""
        return sum(L.buy_px or 0 for L in self.legs.values()
                   if L.state in ("bid", "pending", "long"))

    def _place(self, L, side, px, now):
        body = {"ticker": L.ticker, "side": side, "count": "1.00",
                "price": f"{px / 100:.4f}", "time_in_force": "good_till_canceled",
                "post_only": True, "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": str(uuid.uuid4())}
        if not self.live:
            oid = "dry-" + body["client_order_id"][:8]
            self.log("place", L, now, side=side, px=px, oid=oid, dry=True)
            self.by_oid[oid] = (L.ticker, "buy" if side == "bid" else "sell")
            return oid
        try:
            r = self.api.post("/portfolio/events/orders", body)
        except Exception as e:
            self.log("place_error", L, now, side=side, px=px, error=str(e)[:300])
            return None
        o = r.get("order") or r
        oid = o.get("order_id")
        self.log("place", L, now, side=side, px=px, oid=oid, fill_count=o.get("fill_count"))
        if oid:
            self.by_oid[oid] = (L.ticker, "buy" if side == "bid" else "sell")
        return oid

    def _cancel(self, L, oid, now):
        """True if something was cancelled; False if nothing was left (filled)."""
        if not self.live or str(oid).startswith("dry-"):
            self.log("cancel", L, now, oid=oid, dry=True)
            self.by_oid.pop(oid, None)
            return True
        try:
            r = self.api.delete(f"/portfolio/events/orders/{oid}")
        except Exception as e:
            self.log("cancel_error", L, now, oid=oid, error=str(e)[:200])
            return False                      # the fills poll decides what happened
        reduced = float(r.get("reduced_by") or 0)
        self.log("cancel", L, now, oid=oid, reduced_by=reduced)
        if reduced > 0:
            self.by_oid.pop(oid, None)
        return reduced > 0

    def _drop_buy(self, L, now):
        """Cancel the resting buy. True if the leg is now flat."""
        if self._cancel(L, L.buy_oid, now):
            L.state, L.buy_oid, L.buy_px = "flat", None, None
            return True
        L.state, L.pending_since = "pending", now      # it probably filled
        return False

    def tick(self, now):
        """Same decisions as the paper Leg.tick, acted out with real orders."""
        for L in self.legs.values():
            if L.state == "pending" and now - L.pending_since > PENDING_TIMEOUT_MS:
                self.log("pending_timeout", L, now)
                L.state, L.buy_oid, L.buy_px = "flat", None, None
            if now >= L.cutoff:
                if L.state == "bid":
                    self._drop_buy(L, now)
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
                    continue                       # unchanged price keeps its queue place
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
        tk, kind = self.by_oid.pop(oid)
        L = self.legs[tk]
        px = dp._c(fill.get("yes_price_dollars"))
        if kind == "buy":
            L.state, L.buy_px, L.buy_oid = "long", px, None
            self.log("buy_fill", L, now, px=px, oid=oid, is_taker=fill.get("is_taker"))
            L.sell_px = min(99, px + dp.TARGET_C)
            L.sell_oid = self._place(L, "ask", L.sell_px, now)
        else:
            self.log("sell_fill", L, now, px=px, buy_px=L.buy_px,
                     pnl_c=(px or 0) - (L.buy_px or 0), oid=oid)
            L.state, L.trips = "flat", L.trips + 1
            L.buy_px = L.sell_oid = L.sell_px = None


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


def run(date, max_dollars, live):
    from wc.kalshi.client_ext import KalshiClientV2, KalshiWS
    client = KalshiClientV2()
    api = API(client)
    kickoffs, init = dp.discover(client, date)
    now0 = int(time.time() * 1000)
    legs = {tk: LiveLeg(tk, ko) for tk, ko in kickoffs.items()
            if ko + dp.NO_NEW_AFTER_KO_MIN * 60_000 > now0}
    if not legs:
        raise SystemExit("no legs still open for new buys")
    for tk, (bid, ask, last) in init.items():
        if tk in legs:
            legs[tk].bid, legs[tk].ask, legs[tk].ref = bid, ask, last
    end_ms = max(L.ko for L in legs.values()) + dp.KICKOFF_BEFORE_OCC_MS + 30 * 60_000

    os.makedirs(LOGS, exist_ok=True)
    f = open(os.path.join(LOGS, f"dip_live_{date}.jsonl"), "a")
    lock = threading.Lock()
    stats = defaultdict(int)

    def log(event, L, ts, **kw):
        e = {"event": event, "ticker": L.ticker if L else None, "ts": ts,
             "phase": ("pre" if ts < L.ko else "in") if L else None,
             "ref": L.ref if L else None, "bid": L.bid if L else None,
             "ask": L.ask if L else None, "live": live, **kw}
        f.write(json.dumps(e) + "\n")
        f.flush()
        stats[event] += 1

    ex = Executor(api, legs, int(max_dollars * 100), live, log)

    class Feed(KalshiWS):
        def _on_message(self, _ws, raw):
            try:
                p = dp.parse(json.loads(raw))
            except Exception:
                return
            if not p or p[1] not in legs:
                return
            with lock:
                L = legs[p[1]]
                if p[0] == "quote":
                    L.bid, L.ask = p[2], p[3]
                else:
                    L.ref = p[2]

    stop = threading.Event()

    def feed_loop():
        while not stop.is_set():
            ws = Feed(client)
            ws.run(list(legs), ("ticker", "trade"))
            if not stop.is_set():
                with lock:
                    log("ws_gap", None, int(time.time() * 1000), error=str(ws.error)[:200])
                time.sleep(5)

    threading.Thread(target=feed_loop, daemon=True).start()
    print(f"[dip-live] {date}: {len(legs)} legs, cap ${max_dollars:.2f}, "
          f"{'LIVE ORDERS' if live else 'DRY RUN'}, until "
          f"{dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc):%H:%M} UTC", flush=True)
    time.sleep(3)                                    # let the feed refresh quotes first
    fills_since = int(time.time()) - 5
    seen = set()
    last_status = 0
    try:
        while (now := int(time.time() * 1000)) < end_ms:
            if os.path.exists(KILL):
                print("[dip-live] KILL file found", flush=True)
                break
            if live:
                try:
                    fl = api.get("/portfolio/fills", {"min_ts": fills_since, "limit": 200})
                    for fill in sorted(fl.get("fills") or [], key=lambda x: x.get("ts") or 0):
                        if fill.get("fill_id") in seen:
                            continue
                        seen.add(fill.get("fill_id"))
                        with lock:
                            ex.on_fill(fill, now)
                except Exception as e:
                    with lock:
                        log("fills_error", None, now, error=str(e)[:200])
            with lock:
                ex.tick(now)
            if now - last_status >= 300_000:
                print(f"[dip-live] {dt.datetime.now(dt.timezone.utc):%H:%M} committed="
                      f"{ex.committed()}c " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())),
                      flush=True)
                last_status = now
            time.sleep(TICK_S)
    except Exception:
        traceback.print_exc()
    finally:
        stop.set()
        if live:
            try:
                api.delete("/portfolio/events/orders")       # cancel every resting order
                print("[dip-live] cancelled all resting orders", flush=True)
            except Exception as e:
                print(f"[dip-live] CANCEL-ALL FAILED: {e}", flush=True)
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
