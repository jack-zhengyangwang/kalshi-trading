#!/usr/bin/env python3
"""
inplay_exit.py — Automated in-play exit for Polymarket or Kalshi positions.

Monitors price every 60s and auto-sells based on configurable triggers.
No score API needed — the market price IS the signal.

Usage — Polymarket:
    python3 inplay_exit.py \
        --exchange polymarket \
        --wallet 0xYOUR_ADDRESS \
        --match-start 2026-06-12T00:00:00Z \
        [--execute]

    Env: WALLET_KEY=0xYOUR_PRIVATE_KEY

Usage — Kalshi:
    python3 inplay_exit.py \
        --exchange kalshi \
        --market KXFTBALL-25FIFAKR-WIN \
        --match-start 2026-06-12T00:00:00Z \
        [--execute]

    Env: KALSHI_API_KEY=your_api_key

    Find your Kalshi ticker first:
        python3 ../kalshi_client.py "Korea World Cup"

Arguments:
    --exchange      polymarket | kalshi (default: polymarket)
    --wallet        Polymarket wallet address (polymarket only)
    --market        Kalshi ticker OR Polymarket slug override
    --match-start   ISO 8601 UTC kick-off time (default: 2026-06-12T00:00:00Z)
    --sell-high     Sell 65% when price rises above this (default: 0.72)
    --sell-all-hi   Sell 100% when price rises above this (default: 0.90)
    --sell-panic    Sell 100% when price drops below this (default: 0.18)
    --min-elapsed   Don't trigger before N minutes into match (default: 30)
    --poll-secs     Polling interval in seconds (default: 60)
    --execute       Actually submit orders (omit for dry-run)
"""

import os, sys, ssl, time, argparse
from datetime import datetime, timezone

# ── SOCKS5 proxy — only needed for Polymarket, applied selectively ─────────
SOCKS_PROXY = "socks5h://127.0.0.1:1080"

def _apply_socks5_patch():
    try:
        import httpx
        _orig = httpx.Client.__init__
        def _px(self, *a, **kw):
            if 'proxy' not in kw and 'proxies' not in kw and 'mounts' not in kw:
                kw['proxy'] = SOCKS_PROXY
            _orig(self, *a, **kw)
        httpx.Client.__init__ = _px
        _orig_a = httpx.AsyncClient.__init__
        def _apx(self, *a, **kw):
            if 'proxy' not in kw and 'proxies' not in kw and 'mounts' not in kw:
                kw['proxy'] = SOCKS_PROXY
            _orig_a(self, *a, **kw)
        httpx.AsyncClient.__init__ = _apx
    except ImportError:
        pass

    import requests as _r
    _orig_req = _r.Session.request
    def _preq(self, method, url, **kw):
        if "polymarket.com" in url or "clob" in url.lower():
            kw.setdefault("proxies", {"http": SOCKS_PROXY, "https": SOCKS_PROXY})
        return _orig_req(self, method, url, **kw)
    _r.Session.request = _preq

import requests

CLOB_HOST  = "https://clob.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID   = 137

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode    = ssl.CERT_NONE


# ══════════════════════════════════════════════════════════════════════════
# EXCHANGE ADAPTERS
# Each adapter exposes: get_price(), get_position(), sell()
# All prices in [0, 1]; position size in units (shares or contracts).
# ══════════════════════════════════════════════════════════════════════════

class PolymarketAdapter:
    """Polymarket CLOB via py_clob_client_v2. Needs SOCKS5 + WALLET_KEY."""

    DEFAULT_SLUG = "fifwc-kr-cze-2026-06-11-kr"

    def __init__(self, wallet_address, private_key, slug=None):
        self.wallet      = wallet_address
        self.private_key = private_key
        self.slug        = slug or self.DEFAULT_SLUG
        self.token_id    = None
        self.tick_size   = 0.01

    def setup(self):
        print(f"  Fetching token ID for slug '{self.slug}'...", flush=True)
        resp = requests.get(f"{GAMMA_HOST}/markets?slug={self.slug}&limit=1",
                            verify=False, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            raise RuntimeError(f"No Polymarket market for slug: {self.slug}")
        m = markets[0]
        for token in m.get("tokens", []):
            if token.get("outcome", "").lower() in ("yes", "korea", "true"):
                self.token_id  = token["token_id"]
                self.tick_size = float(m.get("orderPriceMinTickSize", "0.01"))
                break
        if not self.token_id:
            tokens = m.get("tokens", [])
            if tokens:
                self.token_id  = tokens[0]["token_id"]
                self.tick_size = float(m.get("orderPriceMinTickSize", "0.01"))
        if not self.token_id:
            raise RuntimeError("Could not find YES token in Polymarket response")
        print(f"  Token: {self.token_id[:20]}...  tick={self.tick_size}", flush=True)

    def get_price(self):
        try:
            resp = requests.get(f"{CLOB_HOST}/book?token_id={self.token_id}",
                                verify=False, timeout=8)
            resp.raise_for_status()
            bids = resp.json().get("bids", [])
            return float(bids[0]["price"]) if bids else None
        except Exception as e:
            print(f"  [PM WARN] price fetch: {e}", flush=True)
            return None

    def get_position(self):
        try:
            resp = requests.get(f"{DATA_API}/positions?user={self.wallet}&sizeThreshold=0.01",
                                verify=False, timeout=10)
            resp.raise_for_status()
            for pos in resp.json():
                tid = pos.get("asset") or pos.get("tokenId") or ""
                if tid == self.token_id:
                    return float(pos.get("size", 0) or 0)
        except Exception as e:
            print(f"  [PM WARN] position fetch: {e}", flush=True)
        return None

    def sell(self, amount, dry_run):
        """amount = shares (float). Returns True on success."""
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import OrderType, OrderArgs
        from py_clob_client_v2.order_builder.constants import SELL

        best_bid = self.get_price()
        if not best_bid or best_bid < 0.01:
            print("  [PM ERROR] No bid. Cannot sell.", flush=True)
            return False

        tick  = self.tick_size
        price = max(0.01, round(best_bid / tick) * tick)
        price = round(price, 4)

        print(f"  PM: sell {amount:.2f} shares @ {price:.4f}  (proceeds ≈${amount*price:.2f})", flush=True)
        if dry_run:
            print("  [DRY RUN] would submit FAK sell.", flush=True)
            return True

        try:
            client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=self.private_key)
            creds  = client.create_or_derive_api_key()
            client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=self.private_key,
                                creds=creds, signature_type=0, funder=self.wallet)
            signed = client.create_order(OrderArgs(token_id=self.token_id,
                                                   price=price, size=round(amount, 4), side=SELL))
            resp   = client.post_order(signed, OrderType.FAK)
            status = (resp.get("status") or "?").upper()
            print(f"  PM order → {status}", flush=True)
            if status in ("DELAYED", "LIVE"):
                time.sleep(2)
                try:
                    oid = resp.get("orderID") or resp.get("orderId") or resp.get("id")
                    os2 = client.get_order(str(oid))
                    status = (os2.get("status") or status).upper()
                except Exception:
                    pass
            return status in ("MATCHED", "FILLED")
        except Exception as e:
            print(f"  [PM ERROR] {e}", flush=True)
            return False

    @property
    def unit_label(self):
        return "shares"


class KalshiAdapter:
    """Kalshi REST API v2. Supports RSA (KALSHI_KEY_ID) or Bearer (KALSHI_API_KEY). No proxy needed."""

    def __init__(self, ticker, api_key=None, key_id=None):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from kalshi_client import KalshiClient
        self.ticker = ticker
        self.client = KalshiClient(api_key=api_key, key_id=key_id)

    def setup(self):
        print(f"  Verifying Kalshi market '{self.ticker}'...", flush=True)
        m = self.client.get_market(self.ticker)
        title = m.get("title", "?")
        print(f"  Market: {title}", flush=True)

    def get_price(self):
        return self.client.get_best_bid_price(self.ticker)

    def get_position(self):
        count = self.client.get_position_count(self.ticker)
        return float(count) if count else None

    def sell(self, amount, dry_run):
        """amount = contracts (int). Returns True on success."""
        count     = max(1, int(round(amount)))
        best_cents = self.client.get_best_bid_cents(self.ticker)
        if not best_cents:
            print("  [KALSHI ERROR] No bid. Cannot sell.", flush=True)
            return False

        ok, order_id, fill_price = self.client.sell_limit(
            self.ticker, count, best_cents, dry_run=dry_run)
        return ok

    @property
    def unit_label(self):
        return "contracts"


# ══════════════════════════════════════════════════════════════════════════
# SHARED MONITORING LOOP
# ══════════════════════════════════════════════════════════════════════════

def elapsed_minutes(match_start_utc):
    return (datetime.now(timezone.utc) - match_start_utc).total_seconds() / 60

def fmt_time():
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def run_monitor(exchange, match_start, args, dry_run):
    partial_sold = False
    all_sold     = False
    remaining    = exchange.get_position()

    if remaining is None or remaining < 0.5:
        print(f"  [WARN] No position found ({remaining} {exchange.unit_label}).", flush=True)
        print("  Buy a position first, then re-run.", flush=True)
        if not dry_run:
            sys.exit(1)
        remaining = remaining or 0.0
    print(f"  Position: {remaining:.1f} {exchange.unit_label}\n", flush=True)

    print(f"Watching market (polling every {args.poll_secs}s)...\n", flush=True)

    while True:
        elapsed = elapsed_minutes(match_start)
        price   = exchange.get_price()
        t       = fmt_time()

        if price is None:
            print(f"  [{t}] min {elapsed:.0f} — price unavailable, retrying...", flush=True)
            time.sleep(30)
            continue

        est_value = remaining * price
        print(f"  [{t}] min {elapsed:.0f} | price={price:.4f} | "
              f"holding={remaining:.1f} {exchange.unit_label} (≈${est_value:.2f})", flush=True)

        if elapsed > 115:
            print(f"\n  Match over. Hold {remaining:.1f} {exchange.unit_label} to resolution.", flush=True)
            break

        if elapsed < args.min_elapsed:
            time.sleep(args.poll_secs)
            continue

        # Re-arm partial trigger after halftime
        if 47 <= elapsed < 50 and partial_sold and not all_sold:
            print(f"  [{t}] Halftime — re-arming partial sell trigger.", flush=True)
            partial_sold = False

        # ── TRIGGER 1: FULL SELL HI ────────────────────────────────────────
        if price > args.sell_all_hi and not all_sold:
            sell_amt = remaining
            print(f"\n  [{t}] TRIGGER: FULL SELL HI  price={price:.4f} > {args.sell_all_hi}", flush=True)
            print(f"  Selling all {sell_amt:.1f} {exchange.unit_label}.\n", flush=True)
            ok = exchange.sell(sell_amt, dry_run)
            if ok or dry_run:
                all_sold = True
                remaining = 0
            break

        # ── TRIGGER 2: PARTIAL SELL (goal, market over-reacts) ─────────────
        if price > args.sell_high and not partial_sold and not all_sold:
            sell_amt = remaining * 0.65
            print(f"\n  [{t}] TRIGGER: PARTIAL SELL  price={price:.4f} > {args.sell_high}", flush=True)
            print(f"  Selling 65% = {sell_amt:.1f} {exchange.unit_label}.\n", flush=True)
            ok = exchange.sell(sell_amt, dry_run)
            if ok or dry_run:
                partial_sold = True
                remaining   -= sell_amt
                print(f"  Remaining: {remaining:.1f} {exchange.unit_label} riding to resolution.\n", flush=True)

        # ── TRIGGER 3: PANIC SELL ──────────────────────────────────────────
        elif price < args.sell_panic and not all_sold:
            sell_amt = remaining
            print(f"\n  [{t}] TRIGGER: PANIC SELL  price={price:.4f} < {args.sell_panic}", flush=True)
            print(f"  Selling all {sell_amt:.1f} {exchange.unit_label} — cutting loss.\n", flush=True)
            ok = exchange.sell(sell_amt, dry_run)
            if ok or dry_run:
                all_sold = True
                remaining = 0
            break

        if all_sold:
            break

        time.sleep(args.poll_secs)

    print("\n" + "=" * 60, flush=True)
    print(f"  Session complete. Remaining: {remaining:.1f} {exchange.unit_label}", flush=True)
    if remaining > 0.1:
        print("  These will resolve at $1.00 if Korea wins.", flush=True)
    print("=" * 60, flush=True)


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--exchange",     default="polymarket", choices=["polymarket", "kalshi"])
    p.add_argument("--wallet",       default=None,  help="Polymarket wallet address")
    p.add_argument("--market",       default=None,  help="Kalshi ticker or Polymarket slug override")
    p.add_argument("--match-start",  default="2026-06-12T00:00:00Z")
    p.add_argument("--sell-high",    type=float, default=0.72)
    p.add_argument("--sell-all-hi",  type=float, default=0.90)
    p.add_argument("--sell-panic",   type=float, default=0.18)
    p.add_argument("--min-elapsed",  type=int,   default=30)
    p.add_argument("--poll-secs",    type=int,   default=60)
    p.add_argument("--execute",      action="store_true")
    return p.parse_args()


def main():
    args    = parse_args()
    dry_run = not args.execute

    match_start = datetime.fromisoformat(args.match_start.replace("Z", "+00:00"))

    print("=" * 60, flush=True)
    print(f"  EBK IN-PLAY EXIT — Korea vs Czechia", flush=True)
    print(f"  Exchange:    {args.exchange.upper()}", flush=True)
    print(f"  Mode:        {'DRY RUN' if dry_run else '*** LIVE EXECUTION ***'}", flush=True)
    print(f"  Match start: {match_start.strftime('%Y-%m-%d %H:%M UTC')}", flush=True)
    print(f"  Triggers:    >{args.sell_high} → partial sell | >{args.sell_all_hi} → full sell | <{args.sell_panic} → panic", flush=True)
    print("=" * 60, flush=True)

    if args.exchange == "polymarket":
        _apply_socks5_patch()
        wallet = args.wallet
        if not wallet:
            print("ERROR: --wallet required for Polymarket exchange.")
            sys.exit(1)
        key = os.environ.get("WALLET_KEY")
        if args.execute and not key:
            print("ERROR: --execute requires WALLET_KEY env var.")
            sys.exit(1)
        exchange = PolymarketAdapter(wallet, key, slug=args.market)
        print(f"  Wallet: {wallet[:10]}...{wallet[-4:]}", flush=True)

    elif args.exchange == "kalshi":
        ticker = args.market
        if not ticker:
            print("ERROR: --market <TICKER> required for Kalshi.")
            print("  Find it with: python3 ../kalshi_client.py 'Korea World Cup'")
            sys.exit(1)
        api_key = os.environ.get("KALSHI_API_KEY")
        key_id  = os.environ.get("KALSHI_KEY_ID")
        if args.execute and not api_key and not key_id:
            print("ERROR: --execute requires KALSHI_KEY_ID (RSA) or KALSHI_API_KEY (Bearer) env var.")
            sys.exit(1)
        exchange = KalshiAdapter(ticker, api_key=api_key, key_id=key_id)
        print(f"  Ticker: {ticker}", flush=True)

    print(flush=True)
    exchange.setup()
    print(flush=True)

    # Wait until 5 minutes before kick-off so logs aren't spammy
    now = datetime.now(timezone.utc)
    wait_until = match_start.replace(tzinfo=timezone.utc) if match_start.tzinfo is None else match_start
    pre_start_seconds = (wait_until - now).total_seconds() - 300
    if pre_start_seconds > 60:
        wake_at = now.timestamp() + pre_start_seconds
        print(f"  Match starts {match_start.strftime('%H:%M UTC')}. "
              f"Sleeping {pre_start_seconds/60:.0f} min, waking 5 min before kick-off...", flush=True)
        while True:
            remaining_sleep = wake_at - datetime.now(timezone.utc).timestamp()
            if remaining_sleep <= 0:
                break
            time.sleep(min(300, remaining_sleep))
            mins_left = (wake_at - datetime.now(timezone.utc).timestamp()) / 60
            if mins_left > 1:
                print(f"  [{fmt_time()}] {mins_left:.0f} min until bot activates.", flush=True)
        print(f"  [{fmt_time()}] Approaching kick-off — starting active monitoring.\n", flush=True)

    run_monitor(exchange, match_start, args, dry_run)


if __name__ == "__main__":
    main()
