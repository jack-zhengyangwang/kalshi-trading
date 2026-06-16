"""
kalshi_client.py — Thin wrapper around the Kalshi REST API v2.

Supports two auth methods:
  1. RSA private key (recommended — newer Kalshi API)
     Env: KALSHI_KEY_ID + KALSHI_PRIVATE_KEY_FILE (path to PEM file)
  2. Bearer token (older API keys shown as plain strings)
     Env: KALSHI_API_KEY

To find your Key ID: kalshi.com → Settings → API → your key's name/ID field.

Kalshi contract model:
    - Each contract pays $1.00 if YES, $0.00 if NO at resolution
    - Prices are integers in cents: 38 = $0.38 per contract
    - Position size = integer number of contracts (no fractional)
    - No proxy needed (US-regulated, no geo-block)
"""

import os, json, time, base64, requests
from datetime import datetime, timezone

LIVE_HOST  = "https://api.elections.kalshi.com"
DEMO_HOST  = "https://demo-api.kalshi.co"
API_PREFIX = "/trade-api/v2"
LIVE_BASE  = LIVE_HOST + API_PREFIX
DEMO_BASE  = DEMO_HOST + API_PREFIX

KEY_FILE_DEFAULT  = os.path.join(os.path.dirname(__file__), "kalshi_private_key.pem")
KOREA_TONIGHT     = "KXWCGAME-26JUN11KORCZE-KOR"   # Korea Republic wins tonight


def _load_rsa_key(pem_path):
    """Load RSA private key from PEM file. Returns a cryptography key object."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    with open(pem_path, "rb") as f:
        data = f.read()
    return serialization.load_pem_private_key(data, password=None, backend=default_backend())


def _rsa_sign(private_key, message: str) -> str:
    """Sign message with RSA-PSS-SHA256, return base64-encoded signature.
    Kalshi API v2 requires PSS padding (not PKCS1v15)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.padding import PSS, MGF1
    sig = private_key.sign(
        message.encode("utf-8"),
        PSS(mgf=MGF1(hashes.SHA256()), salt_length=PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode("utf-8")


class KalshiClient:
    def __init__(self, key_id=None, private_key_file=None, api_key=None, demo=False):
        """
        Priority: RSA key pair > Bearer token.

        RSA (recommended):
            key_id           — from KALSHI_KEY_ID env or passed directly
            private_key_file — path to PEM file (defaults to MyPersonalAgent.txt)

        Bearer (fallback):
            api_key          — from KALSHI_API_KEY env or passed directly
        """
        self.base_url    = DEMO_BASE if demo else LIVE_BASE
        self.session     = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._rsa_key    = None
        self._key_id     = None

        # RSA path
        key_id = key_id or os.environ.get("KALSHI_KEY_ID")
        pem    = private_key_file or os.environ.get("KALSHI_PRIVATE_KEY_FILE") or KEY_FILE_DEFAULT

        if key_id and os.path.exists(pem):
            try:
                self._rsa_key = _load_rsa_key(pem)
                self._key_id  = key_id
                print(f"  [KALSHI] RSA auth — key_id={key_id}", flush=True)
            except Exception as e:
                print(f"  [KALSHI WARN] RSA load failed: {e}. Falling back to Bearer.", flush=True)

        # Bearer fallback
        if not self._rsa_key:
            token = api_key or os.environ.get("KALSHI_API_KEY")
            if token:
                self.session.headers.update({"Authorization": f"Bearer {token}"})
                print("  [KALSHI] Bearer token auth.", flush=True)
            else:
                print("  [KALSHI WARN] No auth credentials found.", flush=True)

    def _auth_headers(self, method: str, path: str) -> dict:
        """Generate RSA auth headers.
        path = suffix only, no query params (e.g. '/portfolio/balance').
        Signature message = timestamp + METHOD + API_PREFIX + path (no query string)."""
        if not self._rsa_key:
            return {}
        ts       = str(int(time.time() * 1000))
        # Strip query string from path — must NOT be included in the signature
        clean_path = path.split("?")[0]
        msg      = ts + method.upper() + API_PREFIX + clean_path
        sig      = _rsa_sign(self._rsa_key, msg)
        return {
            "KALSHI-ACCESS-KEY":       self._key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _get(self, path, params=None, auth=True):
        headers = self._auth_headers("GET", path) if auth else {}
        r = self.session.get(f"{self.base_url}{path}", params=params,
                             headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        headers = self._auth_headers("POST", path)
        r = self.session.post(f"{self.base_url}{path}", json=body,
                              headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Market data ────────────────────────────────────────────────────────

    def search_markets(self, query, status="open", limit=10):
        # Public endpoint — no auth needed
        data = self._get("/markets", params={"status": status, "limit": limit,
                                              "text_search": query}, auth=False)
        return data.get("markets", [])

    def get_market(self, ticker):
        return self._get(f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker, depth=10):
        # Kalshi migrated to dollar-denominated books: `orderbook_fp` with
        # `yes_dollars`/`no_dollars` = [[price_str_dollars, size_str], ...].
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        return data.get("orderbook_fp") or data.get("orderbook") or {}

    def get_best_bid_cents(self, ticker):
        """Best YES bid in cents (what you'd receive selling YES now), or None."""
        # 1. Orderbook top-of-book (authoritative). yes_dollars is ascending,
        #    so the best bid is the highest price.
        try:
            yes = self.get_orderbook(ticker).get("yes_dollars")
            if yes:
                best = max(float(p) for p, _ in yes)
                if best > 0:
                    return int(round(best * 100))
        except Exception as e:
            print(f"  [KALSHI WARN] orderbook: {e}", flush=True)
        # 2. Fall back to the market summary's yes_bid_dollars.
        try:
            yb = self.get_market(ticker).get("yes_bid_dollars")
            if yb not in (None, "") and float(yb) > 0:
                return int(round(float(yb) * 100))
        except Exception as e:
            print(f"  [KALSHI WARN] summary bid: {e}", flush=True)
        return None

    def get_best_ask_cents(self, ticker):
        """Best YES ask in cents (what you'd pay buying YES now), or None."""
        try:
            ya = self.get_market(ticker).get("yes_ask_dollars")
            if ya not in (None, "") and float(ya) > 0:
                return int(round(float(ya) * 100))
        except Exception as e:
            print(f"  [KALSHI WARN] summary ask: {e}", flush=True)
        # Fallback: derive from the orderbook's NO side (best NO bid → YES ask).
        try:
            no = self.get_orderbook(ticker).get("no_dollars")
            if no:
                best_no_bid = max(float(p) for p, _ in no)
                yes_ask = 1.0 - best_no_bid
                if 0 < yes_ask < 1:
                    return int(round(yes_ask * 100))
        except Exception as e:
            print(f"  [KALSHI WARN] orderbook ask: {e}", flush=True)
        return None

    def get_best_bid_price(self, ticker):
        cents = self.get_best_bid_cents(ticker)
        return cents / 100.0 if cents is not None else None

    # ── Portfolio ──────────────────────────────────────────────────────────

    def get_positions(self, ticker=None):
        data = self._get("/portfolio/positions")
        positions = data.get("market_positions", [])
        if ticker:
            return [p for p in positions if p.get("ticker") == ticker]
        return positions

    def get_position_count(self, ticker):
        """Returns fractional contract count as float (Kalshi uses position_fp field)."""
        for p in self.get_positions(ticker):
            return float(p.get("position_fp", 0) or 0)
        return 0.0

    def get_balance(self):
        """Returns available cash balance in dollars."""
        data = self._get("/portfolio/balance")
        # Verified: `balance` is in cents (e.g. 16848 -> $168.48). Fall back to a
        # dollar field if the cents field is ever absent.
        if data.get("balance") is not None:
            return data["balance"] / 100.0
        for k in ("balance_dollars", "available_balance_dollars"):
            if data.get(k) not in (None, ""):
                return float(data[k])
        return 0.0

    # ── Orders ─────────────────────────────────────────────────────────────

    def buy(self, ticker, count, yes_price_cents=None, dry_run=True):
        """
        Buy `count` YES contracts. Defaults to best ask if yes_price_cents not given.
        Returns (success, order_id, fill_price_dollars).
        """
        # Best YES ask in cents (what we'd pay), from the dollar-denominated API.
        price = yes_price_cents or self.get_best_ask_cents(ticker)
        if not price:
            # H4: never default to an arbitrary price — abort rather than overpay.
            print("  [KALSHI ERROR] no ask price available; aborting buy.", flush=True)
            return False, "no-price", 0.0
        # Order body sends `yes_price` in cents — verified accepted by the live
        # order API (1¢ test order: HTTP 201, echoes yes_price_dollars).

        print(f"  Kalshi: buy {count} YES contracts @ {price}¢  "
              f"(cost ≈${count * price / 100:.2f})", flush=True)

        if dry_run:
            print("  [DRY RUN] would submit limit buy.", flush=True)
            return True, "dry-run", price / 100.0

        import uuid
        body = {
            "ticker":          ticker,
            "client_order_id": str(uuid.uuid4()),
            "action":          "buy",
            "side":            "yes",
            "type":            "limit",
            "count":           count,
            "yes_price":       price,
        }
        try:
            resp      = self._post("/portfolio/orders", body)
            order     = resp.get("order", resp)
            order_id  = order.get("order_id", "?")
            status    = order.get("status", "?").upper()
            filled    = float(order.get("fill_count_fp", 0) or 0)
            fill_price = (float(order["yes_price_dollars"])
                          if order.get("yes_price_dollars") not in (None, "")
                          else price / 100.0)
            print(f"  Kalshi buy order {order_id} → {status} (filled {filled:g})", flush=True)
            # H2: a RESTING order is NOT a fill. Count it filled only if executed
            # or actually matched; otherwise cancel the resting order.
            if status in ("EXECUTED", "FILLED") or filled > 0:
                return True, order_id, fill_price
            if status == "RESTING":
                print("  [KALSHI] order rested unfilled — cancelling.", flush=True)
                self.cancel_order(order_id)
            return False, order_id, 0.0
        except Exception as e:
            print(f"  [KALSHI ERROR] buy failed: {e}", flush=True)
            return False, "error", 0.0

    def cancel_order(self, order_id):
        """Cancel a resting order by ID."""
        try:
            resp = self._post(f"/portfolio/orders/{order_id}/decrease",
                              {"reduce_by": 999999})
            return resp
        except Exception as e:
            print(f"  [KALSHI ERROR] cancel failed: {e}", flush=True)
            return None

    def sell_limit(self, ticker, count, yes_price_cents, dry_run=True):
        best = self.get_best_bid_cents(ticker)
        actual_price = min(yes_price_cents, best) if best else yes_price_cents

        print(f"  Kalshi: sell {count} contracts @ {actual_price}¢  "
              f"(proceeds ≈${count * actual_price / 100:.2f})", flush=True)

        if dry_run:
            print("  [DRY RUN] would submit limit sell.", flush=True)
            return True, "dry-run", actual_price / 100.0

        import uuid
        body = {
            "ticker":          ticker,
            "client_order_id": str(uuid.uuid4()),
            "action":          "sell",
            "side":            "yes",
            "type":            "limit",
            "count":           count,
            "yes_price":       actual_price,
        }
        try:
            resp      = self._post("/portfolio/orders", body)
            order     = resp.get("order", resp)
            order_id  = order.get("order_id", "?")
            status    = order.get("status", "?").upper()
            filled    = float(order.get("fill_count_fp", 0) or 0)
            fill_price = (float(order["yes_price_dollars"])
                          if order.get("yes_price_dollars") not in (None, "")
                          else actual_price / 100.0)
            print(f"  Kalshi sell order {order_id} → {status} (filled {filled:g})", flush=True)
            # H2: only a real fill counts. A resting sell didn't execute — cancel
            # it so the Keeper retries next poll with a fresh bid.
            if status in ("EXECUTED", "FILLED") or filled > 0:
                return True, order_id, fill_price
            if status == "RESTING":
                self.cancel_order(order_id)
            return False, order_id, 0.0
        except Exception as e:
            print(f"  [KALSHI ERROR] {e}", flush=True)
            return False, "error", 0.0

    def sell_market(self, ticker, count, dry_run=True):
        best = self.get_best_bid_cents(ticker)
        if not best:
            print("  [KALSHI ERROR] No bid available.", flush=True)
            return False, "no-bid", 0.0

        print(f"  Kalshi: market sell {count} contracts  "
              f"(best bid {best}¢, proceeds ≈${count * best / 100:.2f})", flush=True)

        if dry_run:
            print("  [DRY RUN] would submit market sell.", flush=True)
            return True, "dry-run", best / 100.0

        import uuid
        body = {
            "ticker":          ticker,
            "client_order_id": str(uuid.uuid4()),
            "action":          "sell",
            "side":            "yes",
            "type":            "market",
            "count":           count,
        }
        try:
            resp      = self._post("/portfolio/orders", body)
            order     = resp.get("order", resp)
            order_id  = order.get("order_id", "?")
            status    = order.get("status", "?").upper()
            filled    = float(order.get("fill_count_fp", 0) or 0)
            fill_price = (float(order["yes_price_dollars"])
                          if order.get("yes_price_dollars") not in (None, "")
                          else best / 100.0)
            print(f"  Kalshi market-sell order {order_id} → {status} (filled {filled:g})", flush=True)
            return (status in ("EXECUTED", "FILLED") or filled > 0), order_id, fill_price
        except Exception as e:
            print(f"  [KALSHI ERROR] {e}", flush=True)
            return False, "error", 0.0


# ── CLI: search + auth test ────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "Korea World Cup"

    # Check for cryptography package
    try:
        import cryptography
    except ImportError:
        print("Missing dependency — run: pip3 install cryptography")
        sys.exit(1)

    print(f"Auth test + searching Kalshi for: '{query}'\n")
    client = KalshiClient()

    # Test auth
    try:
        bal = client.get_balance()
        print(f"  Balance: ${bal:.2f}\n")
    except Exception as e:
        print(f"  Auth check failed: {e}")
        print("  Set KALSHI_KEY_ID env var with your key ID from kalshi.com → Settings → API\n")

    markets = client.search_markets(query, limit=10)
    if not markets:
        print("No markets found.")
    for m in markets:
        ticker  = m.get("ticker", "?")
        title   = m.get("title", "?")
        yes_bid = m.get("yes_bid", "?")
        yes_ask = m.get("yes_ask", "?")
        vol     = m.get("volume", 0)
        print(f"  {ticker:<44} bid={yes_bid}¢ ask={yes_ask}¢ vol={vol}")
        print(f"    {title}")
        print()
