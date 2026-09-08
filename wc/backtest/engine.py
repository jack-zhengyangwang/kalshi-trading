"""The backtest engine: replay a strategy spec over historical candlesticks.

Market-agnostic. Keys off ticker, close time, and price — never off sport or
series semantics — so the same engine tests soccer, weather, and Fed markets.

Two structural guarantees, both enforced by construction rather than discipline:

  1. NO LOOKAHEAD. The interpreter is handed a BarView, never the bar list. The
     outcome is unreadable until the bar passes close_time. A strategy cannot
     cheat because it is never given the data to cheat with.

  2. CHRONOLOGICAL ACROSS ALL MARKETS. Bars are interleaved in time, not
     replayed market-by-market. Iterating one market to completion would mean
     never holding two positions at once, so portfolio caps could never bind —
     a subtle bug that produces plausible-looking wrong numbers.

See docs/backtester/02_ENGINE.md.
"""
from __future__ import annotations

from collections import defaultdict

from wc.backtest import interpret

DAY = 86400.0


# ── cost model ────────────────────────────────────────────────────────────────

class Costs:
    """Explicit, configurable, and reported separately from P&L.

    Kalshi's trading fee is non-linear in price and peaks near 50c. A flat-rate
    approximation is how a losing strategy comes to look profitable, so the
    published formula is modelled directly.
    """

    def __init__(self, fee_rate=0.07, slippage_cents=1.0,
                 max_volume_share=0.10, settlement_fee=0.0):
        self.fee_rate = fee_rate
        self.slippage_cents = slippage_cents
        self.max_volume_share = max_volume_share
        self.settlement_fee = settlement_fee

    def trade_fee(self, price, contracts):
        """Kalshi fee: round_up(fee_rate * C * P * (1 - P)), P as a probability.
        Peaks at P=0.5 and vanishes at the extremes."""
        import math
        return math.ceil(self.fee_rate * contracts * price * (1.0 - price) * 100.0) / 100.0


class BarView:
    """What a strategy is allowed to see at one instant.

    Deliberately not the bar list, and deliberately without `result` until the
    market has closed. This class IS the no-lookahead guarantee.
    """

    __slots__ = ("ticker", "series", "ts", "yes_bid", "yes_ask", "close",
                 "volume", "open_interest", "close_time", "_result", "_history")

    def __init__(self, row, history=None):
        self.ticker = row["ticker"]
        self.series = row["series"]
        self.ts = row["ts"]
        self.yes_bid = row["yes_bid"]
        self.yes_ask = row["yes_ask"]
        self.close = row["close"]
        self.volume = row["volume"] or 0
        self.open_interest = row["open_interest"] or 0
        self.close_time = row["close_time"]
        self._result = row["result"]
        self._history = history or []

    @property
    def settled(self):
        return self.close_time is not None and self.ts >= self.close_time

    @property
    def result(self):
        """Unreadable until the market has actually closed."""
        if not self.settled:
            return None
        return self._result

    def days_to_resolution(self):
        if self.close_time is None:
            return None
        return max(0.0, (self.close_time - self.ts) / DAY)

    def mid(self):
        if self.yes_bid is None or self.yes_ask is None:
            return (self.close / 100.0) if self.close is not None else None
        return ((self.yes_bid + self.yes_ask) / 2.0) / 100.0

    def to_ctx(self, side, model_prob=None):
        """The dict the interpreter evaluates conditions against."""
        mid = self.mid()
        price = mid if side == "yes" else (1.0 - mid if mid is not None else None)
        spread = (self.yes_ask - self.yes_bid) \
            if (self.yes_bid is not None and self.yes_ask is not None) else None

        ctx = {
            "ticker": self.ticker,
            "series": self.series,
            "price": price,               # what WE pay, given side
            "yes_price": mid,             # the market's own price, side-independent
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "spread": spread,
            "days_to_resolution": self.days_to_resolution(),
            "volume_24h": self.volume,
            "open_interest": self.open_interest,
            "model_prob": model_prob,
            "edge": (model_prob - price) if (model_prob is not None and price is not None) else None,
        }
        if self._history:
            ctx.update(self._trailing_features(price))
        return ctx

    def _trailing_features(self, price):
        """Computed on the trailing window only — never on future bars."""
        import statistics
        prices = [p for p in self._history if p is not None]
        if len(prices) < 2:
            return {}
        first = prices[0]
        return {
            "price_change_pct": ((price - first) / first) if (first and price is not None) else None,
            "volatility": statistics.pstdev(prices) if len(prices) > 1 else 0.0,
        }


class Position:
    __slots__ = ("ticker", "series", "side", "contracts", "entry_price",
                 "entry_ts", "cost", "entry_fee", "entry_dtr", "model_prob")

    def __init__(self, ticker, series, side, contracts, entry_price, entry_ts,
                 cost, entry_fee, entry_dtr, model_prob):
        self.ticker, self.series, self.side = ticker, series, side
        self.contracts, self.entry_price = contracts, entry_price
        self.entry_ts, self.cost = entry_ts, cost
        self.entry_fee, self.entry_dtr = entry_fee, entry_dtr
        self.model_prob = model_prob

    def mark(self, price):
        """Unrealised P&L at the current price."""
        return (price - self.entry_price) * self.contracts

    def exit_ctx(self, price, ts):
        return {
            "unrealized_pnl_pct": (self.mark(price) / self.cost) if self.cost else 0.0,
            "days_held": (ts - self.entry_ts) / DAY,
            "hold_to_settlement": False,
        }


def run(spec, bars, starting_bankroll=1000.0, costs=None,
        model_probs=None, history_window=20):
    """Replay `spec` over `bars` (chronological, all markets interleaved).

    `model_probs` optionally maps (ticker, ts) -> p_fair from our own brains,
    which is what lets an existing v4 strategy be expressed as a spec and
    backtested against the same engine as everything else.

    Returns (trades, rejections).
    """
    costs = costs or Costs()
    side = spec["side"]
    caps = spec["caps"]
    max_positions = spec["sizing"].get("max_concurrent_positions")

    bankroll = float(starting_bankroll)
    open_positions = {}                       # ticker -> Position
    trades = []
    rejections = defaultdict(int)
    spend_by_day = defaultdict(float)
    history = defaultdict(list)               # ticker -> trailing prices
    last_view = {}                            # ticker -> most recent BarView

    for row in bars:
        view = BarView(row, history=list(history[row["ticker"]]))
        mid = view.mid()
        if mid is None:
            rejections["no_price"] += 1
            continue

        price = mid if side == "yes" else 1.0 - mid
        history[view.ticker].append(price)
        if len(history[view.ticker]) > history_window:
            history[view.ticker].pop(0)

        last_view[view.ticker] = view
        mp = (model_probs or {}).get((view.ticker, view.ts))
        ctx = view.to_ctx(side, model_prob=mp)

        # ── settle anything past close ───────────────────────────────────────
        pos = open_positions.get(view.ticker)
        if pos and view.settled:
            trades.append(_settle(pos, view, costs))
            bankroll += trades[-1]["net_pnl"] + pos.cost
            del open_positions[view.ticker]
            continue

        # ── exits, before entries: frees capital and a position slot ─────────
        if pos:
            if interpret.should_exit(spec, ctx, pos.exit_ctx(price, view.ts)):
                trades.append(_close(pos, view, price, costs))
                bankroll += trades[-1]["net_pnl"] + pos.cost
                del open_positions[view.ticker]
            continue                          # never re-enter the same bar

        # ── entries ──────────────────────────────────────────────────────────
        # universe and signal rejections are counted separately: "no trades"
        # must be explainable, not silent.
        if not interpret.passes_universe(spec, ctx):
            rejections["universe_filter"] += 1
            continue
        if not interpret.evaluate(spec["entry"], ctx):
            rejections["entry_conditions"] += 1
            continue

        if max_positions and len(open_positions) >= max_positions:
            rejections["max_concurrent_positions"] += 1
            continue

        stake = interpret.size(spec, ctx, bankroll)
        if stake <= 0:
            rejections["zero_size"] += 1
            continue

        stake = min(stake, caps["per_market_dollars"])

        day = int(view.ts // 86400)
        if spend_by_day[day] + stake > caps["daily_spend_dollars"]:
            rejections["daily_cap"] += 1
            continue

        exposure = sum(p.cost for p in open_positions.values())
        if exposure + stake > caps["total_exposure_dollars"]:
            rejections["total_exposure_cap"] += 1
            continue

        # take, never make: buy at the ask plus slippage
        fill = _fill_price(view, side, costs, buying=True)
        if fill is None or fill <= 0 or fill >= 1:
            rejections["no_fill_price"] += 1
            continue

        contracts = int(stake / fill)
        if contracts < 1:
            rejections["below_one_contract"] += 1
            continue

        cap_by_volume = int((view.volume or 0) * costs.max_volume_share)
        if cap_by_volume < 1:
            rejections["insufficient_volume"] += 1
            continue
        contracts = min(contracts, cap_by_volume)

        cost = contracts * fill
        fee = costs.trade_fee(fill, contracts)
        if cost + fee > bankroll:
            rejections["insufficient_bankroll"] += 1
            continue

        bankroll -= (cost + fee)
        spend_by_day[day] += cost
        open_positions[view.ticker] = Position(
            view.ticker, view.series, side, contracts, fill, view.ts,
            cost, fee, view.days_to_resolution(), mp)

    # Positions still open when the data ends must not vanish — that would
    # silently drop their P&L and flatter the result. Mark them out at the last
    # price seen and flag them, so the report can separate them from real exits.
    for ticker, pos in open_positions.items():
        last = last_view.get(ticker)
        if last is None:
            continue
        price = last.mid()
        if price is None:
            continue
        px = price if pos.side == "yes" else 1.0 - price
        gross = (px - pos.entry_price) * pos.contracts
        rec = _record(pos, last.ts, px, gross, 0.0, None, False)
        rec["liquidated_at_end"] = True
        trades.append(rec)
        rejections["open_at_end"] += 1

    return trades, dict(rejections)


def _fill_price(view, side, costs, buying):
    """Fill at the side-appropriate touch plus slippage — never at `close`.
    Filling at close assumes size existed at the midpoint, which is the most
    common way a backtest flatters itself."""
    slip = costs.slippage_cents / 100.0
    if side == "yes":
        raw = view.yes_ask if buying else view.yes_bid
    else:
        # NO price is the complement of the opposing YES touch
        raw = (100 - view.yes_bid) if buying else (100 - view.yes_ask)
    if raw is None:
        return None
    p = raw / 100.0
    return p + slip if buying else p - slip


def _record(pos, exit_ts, exit_price, gross, exit_fee, outcome, settled):
    fees = pos.entry_fee + exit_fee
    return {
        "ticker": pos.ticker, "series": pos.series, "side": pos.side,
        "contracts": pos.contracts,
        "entry_ts": pos.entry_ts, "entry_price": pos.entry_price,
        "exit_ts": exit_ts, "exit_price": exit_price,
        "cost": round(pos.cost, 6),
        "gross_pnl": round(gross, 6),
        "fees": round(fees, 6),
        "net_pnl": round(gross - fees, 6),
        "return_pct": round((gross - fees) / pos.cost, 6) if pos.cost else 0.0,
        "entry_days_to_resolution": pos.entry_dtr,
        "model_prob": pos.model_prob,
        "outcome": outcome,
        "settled": settled,
    }


def _close(pos, view, price, costs):
    """Exit before settlement, at the bid minus slippage."""
    fill = _fill_price(view, pos.side, costs, buying=False)
    fill = price if fill is None else fill
    gross = (fill - pos.entry_price) * pos.contracts
    return _record(pos, view.ts, fill, gross,
                   costs.trade_fee(fill, pos.contracts), None, False)


def _settle(pos, view, costs):
    """Settlement: contracts pay 1 or 0. `view.result` is only readable here
    because the bar has passed close_time."""
    won = (view.result == "yes") if pos.side == "yes" else (view.result == "no")
    outcome = 1 if won else 0
    payout = 1.0 if won else 0.0
    gross = (payout - pos.entry_price) * pos.contracts
    return _record(pos, view.ts, payout, gross, costs.settlement_fee, outcome, True)
