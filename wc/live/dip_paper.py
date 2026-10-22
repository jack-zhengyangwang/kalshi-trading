"""Dip catcher, PAPER, on live Kalshi data. Rules: docs/findings/13_DIP_PAPER_PLAN.md.

Rest a YES buy 2c under the last trade, re-pegged every 60s; when a real trade
prints through it (a dip faster than the re-peg), rest a sell 5c above the fill;
an unsold position is held to settlement. Nothing here sends an order: fills are
simulated against Kalshi's live `ticker` and `trade` WebSocket channels.

    python -m wc.live.dip_paper run --date 2026-10-22      # until the last game ends
    python -m wc.live.dip_paper report --date 2026-10-22   # after settlement

Prices are integer cents; times are epoch milliseconds.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import threading
import time
from collections import defaultdict

from wc import paths

OFFSET_C = 2                  # buy this far under the last trade
TARGET_C = 5                  # sell this far over the fill
REPEG_MS = 60_000             # follow the price at most once a minute
MIN_REF, MAX_REF = 5, 95      # only legs priced 5-95c
MAX_TRIPS = 3                 # completed round trips per leg
NO_NEW_AFTER_KO_MIN = 90      # no new buys after kickoff + 90 min (wall clock)
KICKOFF_BEFORE_OCC_MS = 3 * 3600 * 1000   # occurrence_datetime = kickoff + 3h
SERIES = ("KXUELGAME", "KXUECLGAME")
LOGS = os.path.join(paths.ROOT, "logs")
RESULTS = os.path.join(paths.ROOT, "docs", "findings", "results")


class Leg:
    """One market's paper orders. States: flat -> bid (resting buy) -> long
    (resting sell) -> flat. Every method returns the events it caused."""

    def __init__(self, ticker, kickoff_ms):
        self.ticker = ticker
        self.ko = kickoff_ms
        self.cutoff = kickoff_ms + NO_NEW_AFTER_KO_MIN * 60_000
        self.state = "flat"
        self.ref = self.bid = self.ask = None
        self.buy_px = self.buy_post = None
        self.sell_px = self.sell_post = None
        self.trips = 0

    def _ev(self, event, ts, **kw):
        return {"event": event, "ticker": self.ticker, "ts": ts,
                "phase": "pre" if ts < self.ko else "in",
                "ref": self.ref, "bid": self.bid, "ask": self.ask, **kw}

    def _target(self):
        if self.ref is None or not MIN_REF <= self.ref <= MAX_REF:
            return None
        px = self.ref - OFFSET_C
        if self.ask is not None:
            px = min(px, self.ask - 1)          # never at or above the ask
        return px if px >= 1 else None

    def _bought(self, ts):
        out = [self._ev("buy_fill", ts, px=self.buy_px)]
        self.state = "long"
        self.sell_px, self.sell_post = min(99, self.buy_px + TARGET_C), ts
        out.append(self._ev("post_sell", ts, px=self.sell_px))
        return out

    def _sold(self, ts):
        ev = self._ev("sell_fill", ts, px=self.sell_px, buy_px=self.buy_px,
                      pnl_c=self.sell_px - self.buy_px)
        self.state, self.trips = "flat", self.trips + 1
        self.buy_px = self.buy_post = self.sell_px = self.sell_post = None
        return [ev]

    def on_quote(self, bid, ask, ts):
        self.bid, self.ask = bid, ask
        if self.state == "bid" and ask is not None and ts > self.buy_post and ask <= self.buy_px:
            return self._bought(ts)
        if self.state == "long" and bid is not None and ts > self.sell_post and bid >= self.sell_px:
            return self._sold(ts)
        return []

    def on_trade(self, px, ts):
        out = []
        if self.state == "bid" and ts > self.buy_post and px < self.buy_px:
            out = self._bought(ts)
        elif self.state == "long" and ts > self.sell_post and px > self.sell_px:
            out = self._sold(ts)
        self.ref = px
        return out

    def tick(self, ts):
        if ts >= self.cutoff:
            if self.state == "bid":
                self.state, self.buy_px, self.buy_post = "flat", None, None
                return [self._ev("cancel_buy", ts)]
            return []
        if self.state == "flat":
            px = self._target()
            if px is None or self.trips >= MAX_TRIPS:
                return []
            self.state, self.buy_px, self.buy_post = "bid", px, ts
            return [self._ev("post_buy", ts, px=px)]
        if self.state == "bid" and ts - self.buy_post >= REPEG_MS:
            px = self._target()
            if px is None:
                self.state, self.buy_px, self.buy_post = "flat", None, None
                return [self._ev("cancel_buy", ts)]
            if px != self.buy_px:               # unchanged price keeps its queue place
                self.buy_px, self.buy_post = px, ts
                return [self._ev("post_buy", ts, px=px)]
        return []


# ── messages ──────────────────────────────────────────────────────────────────

def _c(raw):
    return None if raw in (None, "") else int(round(float(raw) * 100))


def parse(msg):
    """WebSocket message -> ("quote", ticker, bid, ask, ts_ms) |
    ("trade", ticker, yes_c, ts_ms) | None."""
    d, t = msg.get("msg") or {}, msg.get("type")
    tk = d.get("market_ticker")
    if not tk:
        return None
    ts = d.get("ts_ms") or (d.get("ts") or 0) * 1000
    if t == "ticker":
        return ("quote", tk, _c(d.get("yes_bid_dollars")), _c(d.get("yes_ask_dollars")), ts)
    if t == "trade":
        return ("trade", tk, _c(d.get("yes_price_dollars")), ts)
    return None


# ── scoring ───────────────────────────────────────────────────────────────────

def _bucket():
    return {"buy_fills": 0, "round_trips": 0, "unsold": 0, "unsettled": 0, "net_c": 0}


def summarize(events, results):
    """Pair fills per ticker in order. A sell closes the open buy at its price;
    a buy still open at the end settles at 100 (result yes) or 0 (no)."""
    total, by_phase, by_series = _bucket(), defaultdict(_bucket), defaultdict(_bucket)
    open_buy, trips = {}, []
    for e in sorted(events, key=lambda e: e["ts"]):
        if e["event"] == "buy_fill":
            open_buy[e["ticker"]] = e
        elif e["event"] == "sell_fill" and e["ticker"] in open_buy:
            b = open_buy.pop(e["ticker"])
            trips.append((b, e["px"] - b["px"], "round_trip"))
    for tk, b in open_buy.items():
        res = results.get(tk)
        if res in ("yes", "no"):
            trips.append((b, (100 if res == "yes" else 0) - b["px"], "unsold"))
        else:
            trips.append((b, 0, "unsettled"))
    for b, pnl, kind in trips:
        for agg in (total, by_phase[b["phase"]], by_series[b["ticker"].split("-")[0]]):
            agg["buy_fills"] += 1
            agg["round_trips" if kind == "round_trip" else kind] += 1
            agg["net_c"] += pnl
    total["by_phase"], total["by_series"] = dict(by_phase), dict(by_series)
    total["trips"] = [{"ticker": b["ticker"], "phase": b["phase"], "buy_px": b["px"],
                       "pnl_c": pnl, "kind": kind} for b, pnl, kind in trips]
    return total


# ── live runner ───────────────────────────────────────────────────────────────

def discover(client, date):
    """{ticker: kickoff_ms} for every leg of SERIES games on `date` (by kickoff)."""
    legs, init = {}, {}
    for s in SERIES:
        ms = client._get("/markets", params={"series_ticker": s, "status": "open",
                                              "limit": 1000}).get("markets") or []
        for m in ms:
            occ = m.get("occurrence_datetime")
            if not occ:
                continue
            occ_ms = int(dt.datetime.fromisoformat(occ.replace("Z", "+00:00")).timestamp() * 1000)
            ko = occ_ms - KICKOFF_BEFORE_OCC_MS
            if dt.datetime.fromtimestamp(ko / 1000, dt.timezone.utc).date().isoformat() == date:
                legs[m["ticker"]] = ko
                init[m["ticker"]] = (_c(m.get("yes_bid_dollars")), _c(m.get("yes_ask_dollars")),
                                     _c(m.get("last_price_dollars")))
    return legs, init


def run(date):
    from wc.kalshi.client_ext import KalshiClientV2, KalshiWS
    client = KalshiClientV2()
    kickoffs, init = discover(client, date)
    if not kickoffs:
        raise SystemExit(f"no {SERIES} legs kicking off on {date}")
    legs = {tk: Leg(tk, ko) for tk, ko in kickoffs.items()}
    for tk, (bid, ask, last) in init.items():
        legs[tk].on_quote(bid, ask, 0)
        if last:
            legs[tk].on_trade(last, 0)
    end_ms = max(kickoffs.values()) + KICKOFF_BEFORE_OCC_MS + 30 * 60_000

    os.makedirs(LOGS, exist_ok=True)
    events_f = open(os.path.join(LOGS, f"dip_events_{date}.jsonl"), "a")
    raw_f = gzip.open(os.path.join(LOGS, f"dip_raw_{date}.jsonl.gz"), "at")
    lock = threading.Lock()
    stats = defaultdict(int)

    def emit(evs):
        for e in evs:
            events_f.write(json.dumps(e) + "\n")
            stats[e["event"]] += 1
        if evs:
            events_f.flush()

    class Feed(KalshiWS):
        def _on_message(self, _ws, raw):
            try:
                msg = json.loads(raw)
            except Exception:
                return
            p = parse(msg)
            if not p or p[1] not in legs:
                return
            with lock:
                raw_f.write(raw + "\n")
                stats["msgs"] += 1
                leg = legs[p[1]]
                emit(leg.on_quote(p[2], p[3], p[4]) if p[0] == "quote"
                     else leg.on_trade(p[2], p[3]))

    stop = threading.Event()

    def feed_loop():
        while not stop.is_set():
            ws = Feed(client)
            ws.run(list(legs), ("ticker", "trade"))
            if not stop.is_set():                # dropped: note the gap and reconnect
                with lock:
                    emit([{"event": "ws_gap", "ticker": None, "ts": int(time.time() * 1000),
                           "error": str(ws.error)[:200]}])
                time.sleep(5)

    threading.Thread(target=feed_loop, daemon=True).start()
    print(f"[dip] {date}: {len(legs)} legs, {len(set(kickoffs.values()))} kickoff times, "
          f"until {dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc):%H:%M} UTC",
          flush=True)
    last_status = 0
    while (now := int(time.time() * 1000)) < end_ms:
        with lock:
            for leg in legs.values():
                emit(leg.tick(now))
            raw_f.flush()
        if now - last_status >= 300_000:
            print(f"[dip] {dt.datetime.now(dt.timezone.utc):%H:%M} " +
                  " ".join(f"{k}={v}" for k, v in sorted(stats.items())), flush=True)
            last_status = now
        time.sleep(5)
    stop.set()
    events_f.close()
    raw_f.close()
    print("[dip] done", dict(stats), flush=True)


def report(date):
    from wc.kalshi.client_ext import KalshiClientV2
    client = KalshiClientV2()
    with open(os.path.join(LOGS, f"dip_events_{date}.jsonl")) as f:
        events = [json.loads(line) for line in f]
    tickers = {e["ticker"] for e in events if e["event"] == "buy_fill"}
    results = {tk: (client._get(f"/markets/{tk}").get("market") or {}).get("result")
               for tk in tickers}
    rep = summarize(events, results)
    rep["date"], rep["ws_gaps"] = date, sum(e["event"] == "ws_gap" for e in events)
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"dip_paper_{date}.json")
    with open(path, "w") as f:
        json.dump(rep, f, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "trips"}, indent=1))
    print("wrote", os.path.relpath(path, paths.ROOT))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["run", "report"])
    ap.add_argument("--date", required=True, help="kickoff date, UTC, YYYY-MM-DD")
    a = ap.parse_args()
    (run if a.mode == "run" else report)(a.date)


if __name__ == "__main__":
    main()
