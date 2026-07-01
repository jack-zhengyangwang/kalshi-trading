"""
tradetape.py — historical trade-tape data + fill model for the Arena v3 replay.

Kalshi exposes no historical orderbook DEPTH, but it DOES expose the full executed
trade tape (`/markets/trades`: every fill with price, size, time, taker side). That
is the right data for "could my order have been met": if a team wants N contracts at
minute t and only M actually traded near that price/time, the order fills M and the
rest FAILS — exactly the realism the candlestick replay lacked.

Pure data + math: fetch the tape, bucket it per game-minute into {price, volume},
and answer fill questions. No strategy here.
"""


def fetch_trades(client, ticker, max_pages=60, min_ts=None, max_ts=None):
    """Executed trades for a ticker (paginated, newest-first). Each: created_time,
    yes_price_dollars, count_fp (size), taker_side. min_ts/max_ts (unix seconds)
    bound the window so we don't pull a month of pre-market chatter per leg."""
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"ticker": ticker, "limit": 1000}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        if max_ts is not None:
            params["max_ts"] = int(max_ts)
        if cursor:
            params["cursor"] = cursor
        try:
            d = client._get("/markets/trades", params=params, auth=False)
        except Exception:
            break
        out.extend(d.get("trades", []))
        cursor = d.get("cursor")
        if not cursor or not d.get("trades"):
            break
    return out


def _ts(s):
    import datetime as dt
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def minute_book(trades, ko):
    """{minute_offset: {"price": last_yes_price_dollars, "vol": contracts}} relative
    to kickoff `ko` (epoch). Pre-game trades land in negative offsets."""
    book = {}
    for t in sorted(trades, key=lambda x: x.get("created_time", "")):
        ts = _ts(t.get("created_time", ""))
        px = t.get("yes_price_dollars")
        n = t.get("count_fp")
        if ts is None or px in (None, "") or n in (None, ""):
            continue
        m = int((ts - ko) / 60)
        b = book.setdefault(m, {"price": None, "vol": 0.0})
        b["price"] = float(px)        # last trade price in this minute
        b["vol"] += float(n)
    return book


def price_at(book, minute):
    """Last trade price at or before `minute`, or None."""
    ks = [k for k in book if k <= minute and book[k]["price"] is not None]
    return book[max(ks)]["price"] if ks else None


def fillable(book, minute, window=10):
    """Contracts traded in [minute-window, minute] — the realistic fill ceiling
    for an order placed at `minute` (you can't take more than the tape shows)."""
    return sum(book[k]["vol"] for k in book if minute - window <= k <= minute)


def fill(order_n, book, minute, window=10):
    """(filled_contracts, status). status: 'met' | 'partial' | 'unmet'.
    Caps the fill at the volume actually available on the tape near `minute`."""
    avail = fillable(book, minute, window)
    if avail <= 0:
        return 0, "unmet"
    if avail >= order_n:
        return int(order_n), "met"
    return int(avail), "partial"
