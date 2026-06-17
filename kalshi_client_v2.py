"""
kalshi_client_v2.py — hardened READ-side Kalshi client for Arena v2.

Parallel-track module: leaves the live arena's `kalshi_client.py` untouched.
Subclasses KalshiClient (reusing its RSA/Bearer auth + order methods) and adds the
three things full-surface coverage needs to stay under Kalshi's rate limit:

  1. TOKEN-BUCKET THROTTLE — caps request rate (default ~4 req/s) so a cycle that
     touches dozens of legs can't burst past Kalshi's per-second window. The
     original arena's 429s came from the ~9 req/s burst, not the cadence.
  2. 429 / 5xx RETRY with exponential backoff + jitter, honoring Retry-After.
     The original silently dropped throttled calls to None (invisible throttling).
  3. BATCHED SERIES FETCH — `list_markets_by_series()` pulls EVERY leg of a series
     (spread/total/team-total/BTTS/corners/...) in a few paginated /markets calls
     (page size 200), each market already carrying yes_bid/yes_ask/volume. This
     collapses a per-ticker get_market/get_orderbook storm into a handful of calls.

Plus an optional WebSocket subscriber (`KalshiWS`) for high-frequency live pricing
(orderbook_delta / ticker) so in-play scalpers don't poll REST at all.

Docs verified 2026-06-16:
  GET /markets supports series_ticker, event_ticker, tickers (CSV), status,
    limit (max 200), cursor.   https://docs.kalshi.com/api-reference/market/get-markets
  WS wss://api.elections.kalshi.com/trade-api/ws/v2, RSA headers on handshake,
    channels orderbook_delta/ticker/trade/fill.  https://docs.kalshi.com/websockets/websocket-connection
"""
import json
import os
import random
import threading
import time

import requests

from kalshi_client import KalshiClient, API_PREFIX, LIVE_HOST

WS_PATH = "/trade-api/ws/v2"
WS_URL_LIVE = "wss://api.elections.kalshi.com" + WS_PATH


class TokenBucket:
    """Simple thread-safe token bucket. `rate` tokens/sec, burst capacity `rate`."""

    def __init__(self, rate=4.0):
        self.rate = float(rate)
        self.capacity = float(rate)
        self.tokens = float(rate)
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def take(self, n=1):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens < n:
                wait = (n - self.tokens) / self.rate
                time.sleep(wait)
                self.tokens = 0.0
                self.t = time.monotonic()
            else:
                self.tokens -= n


class KalshiClientV2(KalshiClient):
    """Read-hardened client. Order methods are inherited unchanged (orders are not
    rate-bound the way market-data sweeps are; keep them identical to the proven path)."""

    def __init__(self, *a, req_per_sec=4.0, max_retries=5, **kw):
        super().__init__(*a, **kw)
        self._bucket = TokenBucket(req_per_sec)
        self._max_retries = max_retries

    # Override only the GET path: throttle + retry/backoff. POST (orders) untouched.
    def _get(self, path, params=None, auth=True):
        headers = self._auth_headers("GET", path) if auth else {}
        url = f"{self.base_url}{path}"
        delay = 0.5
        last = None
        for attempt in range(self._max_retries):
            self._bucket.take()
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=10)
            except requests.RequestException as e:
                last = e
                time.sleep(delay + random.uniform(0, delay))
                delay = min(delay * 2, 8.0)
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                ra = r.headers.get("Retry-After")
                sleep_s = float(ra) if ra and ra.isdigit() else delay + random.uniform(0, delay)
                last = requests.HTTPError(f"{r.status_code} on {path}")
                time.sleep(sleep_s)
                delay = min(delay * 2, 8.0)
                # auth headers carry a timestamp — refresh them before the retry
                headers = self._auth_headers("GET", path) if auth else {}
                continue
            r.raise_for_status()
            return r.json()
        raise last or requests.HTTPError(f"GET {path} failed after {self._max_retries} tries")

    # ── batched market-data ──────────────────────────────────────────────────

    def list_markets_by_series(self, series_ticker, status="open", page=200):
        """All markets for a series (every leg), paginated. Each market dict
        already carries yes_bid/yes_ask (+ _dollars) and volume — no per-ticker
        follow-up calls needed."""
        out, cursor = [], None
        while True:
            params = {"series_ticker": series_ticker, "status": status, "limit": page}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params, auth=False)
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or not data.get("markets"):
                break
        return out

    def list_markets_by_tickers(self, tickers, page=200):
        """Batch-fetch specific tickers in one call (CSV `tickers` param)."""
        out = []
        for i in range(0, len(tickers), page):
            chunk = ",".join(tickers[i:i + page])
            data = self._get("/markets", params={"tickers": chunk, "limit": page}, auth=False)
            out.extend(data.get("markets", []))
        return out

    @staticmethod
    def quote_cents(market):
        """(bid_cents, ask_cents) from a market dict, dollar fields preferred."""
        def cents(d_field, c_field):
            v = market.get(d_field)
            if v not in (None, ""):
                return int(round(float(v) * 100))
            v = market.get(c_field)
            return int(v) if v not in (None, "") else None
        return cents("yes_bid_dollars", "yes_bid"), cents("yes_ask_dollars", "yes_ask")

    @staticmethod
    def liquidity(market):
        """{volume, oi, ask_size} for a market dict. The batch payload's `volume`
        is null; the real fields are volume_fp / open_interest_fp / yes_ask_size_fp."""
        def num(f):
            v = market.get(f)
            try:
                return float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                return 0.0
        return {"volume": num("volume_fp"), "oi": num("open_interest_fp"),
                "ask_size": num("yes_ask_size_fp")}

    def ws_auth_headers(self):
        """RSA headers for the WebSocket handshake (msg = ts + GET + WS_PATH)."""
        if not self._rsa_key:
            return {}
        from kalshi_client import _rsa_sign
        ts = str(int(time.time() * 1000))
        sig = _rsa_sign(self._rsa_key, ts + "GET" + WS_PATH)
        return {"KALSHI-ACCESS-KEY": self._key_id,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts}


class KalshiWS:
    """Minimal WebSocket subscriber for live pricing. Maintains an in-memory book of
    best bid/ask per ticker from ticker + orderbook_delta updates, so in-play
    scalpers read it without any REST polling.

    Requires `websocket-client` (pip install websocket-client). Import is lazy so
    the rest of the v2 client works without it.
    """

    def __init__(self, client: KalshiClientV2, url=WS_URL_LIVE):
        self.client = client
        self.url = url
        self.quotes = {}          # ticker -> {"bid": cents, "ask": cents, "ts": t}
        self._book = {}           # ticker -> {"yes": {price:size}, "no": {price:size}}
        self._ws = None
        self._id = 0
        self._lock = threading.Lock()
        self.raw = []             # captured messages when self.capture=True
        self.capture = False
        self.error = None

    def _send(self, cmd, params):
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "cmd": cmd, "params": params}))

    def subscribe(self, tickers, channels=("ticker",)):
        self._send("subscribe", {"channels": list(channels),
                                 "market_tickers": list(tickers)})

    def _on_message(self, _ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if self.capture and len(self.raw) < 20:
            self.raw.append(msg)
        t = msg.get("type")
        d = msg.get("msg", {})
        tk = d.get("market_ticker")
        if not tk:
            return
        with self._lock:
            if t == "ticker":
                q = self.quotes.setdefault(tk, {})
                if d.get("yes_bid") is not None:
                    q["bid"] = int(d["yes_bid"])
                if d.get("yes_ask") is not None:
                    q["ask"] = int(d["yes_ask"])
                q["ts"] = time.time()
            elif t == "orderbook_snapshot":
                # Dollar-denominated: yes_dollars_fp / no_dollars_fp = [[price$, size], ...]
                book = {"yes": {}, "no": {}}
                for side in ("yes", "no"):
                    for price, size in (d.get(f"{side}_dollars_fp") or d.get(side) or []):
                        book[side][int(round(float(price) * 100))] = float(size)
                self._book[tk] = book
                self._refresh_quote(tk)
            elif t == "orderbook_delta":
                book = self._book.setdefault(tk, {"yes": {}, "no": {}})
                side = d.get("side")
                pricef = d.get("price_dollars", d.get("price"))
                delta = d.get("delta")
                if side in book and pricef is not None:
                    price_c = int(round(float(pricef) * 100)) if "." in str(pricef) \
                        else int(pricef)
                    lvl = book[side].get(price_c, 0.0) + float(delta or 0)
                    if lvl > 0:
                        book[side][price_c] = lvl
                    else:
                        book[side].pop(price_c, None)
                    self._refresh_quote(tk)

    def _refresh_quote(self, tk):
        """Derive top-of-book YES bid/ask (cents) from the maintained book.
        YES bid = highest yes price; YES ask = 100 - highest no bid."""
        book = self._book.get(tk, {})
        yes, no = book.get("yes", {}), book.get("no", {})
        q = self.quotes.setdefault(tk, {})
        if yes:
            q["bid"] = max(yes)
        if no:
            q["ask"] = 100 - max(no)
        q["ts"] = time.time()

    def run(self, tickers, channels=("ticker",), on_ready=None):
        """Blocking run loop (call in a thread). Reconnects are the caller's job."""
        try:
            import websocket  # websocket-client
        except ImportError as e:
            raise RuntimeError("websocket-client not installed: "
                               "pip install websocket-client") from e
        hdr = [f"{k}: {v}" for k, v in self.client.ws_auth_headers().items()]

        def _open(ws):
            self.subscribe(tickers, channels)
            if on_ready:
                on_ready()

        def _on_error(_ws, err):
            self.error = err

        self._ws = websocket.WebSocketApp(
            self.url, header=hdr,
            on_open=_open, on_message=self._on_message, on_error=_on_error)
        # macOS Python often lacks a system CA chain; reuse certifi's (bundled
        # with requests) so the TLS handshake verifies.
        sslopt = None
        try:
            import certifi
            sslopt = {"ca_certs": certifi.where()}
        except ImportError:
            pass
        self._ws.run_forever(ping_interval=10, ping_timeout=5, sslopt=sslopt)

    def stop(self):
        if self._ws:
            self._ws.close()

    def best(self, ticker):
        with self._lock:
            return dict(self.quotes.get(ticker, {}))


# ── CLI: offline throttle self-test + optional live checks ───────────────────

def _selftest():
    """Offline: verify the token bucket actually paces requests."""
    tb = TokenBucket(rate=5.0)
    t0 = time.monotonic()
    for _ in range(15):
        tb.take()
    dt = time.monotonic() - t0
    # 15 tokens at 5/s with a 5-burst: ~ (15-5)/5 = ~2.0s
    print(f"TokenBucket(5/s): 15 takes in {dt:.2f}s (expect ~2.0s)  "
          f"{'OK' if 1.6 < dt < 2.6 else 'CHECK'}")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    if "--live" in sys.argv:
        series = next((a for a in sys.argv[1:] if not a.startswith("-")), "KXWCSPREAD")
        c = KalshiClientV2()
        mk = c.list_markets_by_series(series)
        print(f"{series}: {len(mk)} open markets")
        for m in mk[:8]:
            b, a = c.quote_cents(m)
            print(f"  {m.get('ticker'):<46} bid={b} ask={a} vol={m.get('volume')}")
        sys.exit(0)
    if "--wstest" in sys.argv:
        series = next((a for a in sys.argv[1:] if not a.startswith("-")), "KXWCGAME")
        c = KalshiClientV2()
        tickers = [m["ticker"] for m in c.list_markets_by_series(series)[:3]]
        print(f"WS test: subscribing to {len(tickers)} {series} tickers "
              f"(orderbook_delta + ticker)...")
        ws = KalshiWS(c)
        ws.capture = True
        th = threading.Thread(
            target=ws.run,
            kwargs={"tickers": tickers, "channels": ("orderbook_delta", "ticker")},
            daemon=True)
        th.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 8 and len(ws.raw) < 6 and ws.error is None:
            time.sleep(0.25)
        ws.stop()
        if ws.error:
            print(f"  WS ERROR: {ws.error}")
        print(f"  received {len(ws.raw)} messages:")
        for m in ws.raw[:6]:
            print(f"    type={m.get('type')}  ticker={m.get('msg', {}).get('market_ticker')}")
        print(f"  quotes derived: {ws.quotes}")
        sys.exit(0 if ws.raw and not ws.error else 1)
    print("usage: python3 kalshi_client_v2.py [--selftest | --live [SERIES] | --wstest [SERIES]]")
