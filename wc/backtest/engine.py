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

from collections import defaultdict, deque

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
                 max_volume_share=0.10, settlement_fee=0.0,
                 fill_delay_bars=1, max_fill_age_seconds=3600,
                 assume_fills=False, fill_rate=0.99):
        self.fee_rate = fee_rate
        self.slippage_cents = slippage_cents
        self.max_volume_share = max_volume_share
        self.settlement_fee = settlement_fee
        # LATENCY. A decision made on bar t fills on bar t+`fill_delay_bars`,
        # at THAT bar's price.
        #
        # Filling on the deciding bar is not merely imprecise, it is BIASED:
        # the price that triggered the strategy is exactly the price it gets,
        # and trigger prices — the dip below 10c, the blown-out spread — are
        # the least likely to still be there when an order arrives. The error
        # never averages out; every trade gets the same small gift, and across
        # thousands of trades that compounds into an edge that is not real.
        #
        # Set 0 to fill on the deciding bar. That is a DIAGNOSTIC, not a
        # result: comparing the two is how you find out whether a strategy is
        # living off its own trigger price.
        self.fill_delay_bars = int(fill_delay_bars)
        # An intent whose fill bar arrives this long after the decision is
        # dropped rather than filled. A market that went quiet for an hour is
        # not one you would still be sending that order into.
        self.max_fill_age_seconds = max_fill_age_seconds

        # ASSUME-FILLS MODE. Treats liquidity as available: the per-bar volume
        # cap is skipped and `fill_rate` of the requested size fills.
        #
        # This is a stated ASSUMPTION, not a measurement, and it is optimistic.
        # Prediction-market books are thin, and the markets a strategy most
        # wants are often the ones with least size behind the quote. Use it to
        # answer "is there an edge here at all" — a strategy that loses even
        # under assumed fills is dead, and that is worth knowing in one run.
        # Do not use it to size real money: turn it off, and see how much of
        # the edge survives the volume cap.
        self.assume_fills = assume_fills
        self.fill_rate = fill_rate

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
                 "volume", "open_interest", "close_time", "_result",
                 "_settlement_value", "_history")

    def __init__(self, row, history=None):
        # `history` is a list of trailing {price, open_interest} dicts — the
        # bars BEFORE this one. It never contains the current bar and never
        # contains a future one.
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
        keys = row.keys() if hasattr(row, "keys") else ()
        self._settlement_value = (row["settlement_value"]
                                  if "settlement_value" in keys else None)
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

    @property
    def final_result(self):
        """The recorded outcome, readable WITHOUT the close_time gate.

        Strictly for end-of-run finalisation, never for a trading decision.
        Kalshi's archive stops at a market's close_time, so the last bar is
        always fractionally BEFORE it and `settled` never fires — which would
        leave every resolved position marked out at its last price, discarding
        an outcome we actually know.

        This is not lookahead: the replay is over, no strategy is consulted
        again, and the position was opened long before. Reading it during the
        loop would be; that is why `result` keeps its gate and this is separate.
        """
        return self._result

    @property
    def final_settlement_value(self):
        """The recorded payout, ungated — the companion to `final_result`, and
        safe for the same reason: the replay is over and no strategy is
        consulted again."""
        return self._settlement_value

    @property
    def settlement_value(self):
        """Dollars per contract at settlement. 1.0 for YES, 0.0 for NO, and
        something in between for a VOID. Unreadable before close, like
        `result` — it carries the outcome just as directly."""
        if not self.settled:
            return None
        return self._settlement_value

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
            "volume": self.volume,
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
        out = {}

        prices = [h["price"] for h in self._history if h.get("price") is not None]
        if len(prices) >= 2:
            first = prices[0]
            out["price_change_pct"] = (((price - first) / first)
                                       if (first and price is not None) else None)
            out["volatility"] = statistics.pstdev(prices)

        # Open-interest change over the same trailing window. Previously
        # declared in the vocabulary but never computed, which meant any
        # strategy gating on it silently never fired — a spec that validated,
        # ran, placed nothing, and gave no reason why.
        ois = [h["open_interest"] for h in self._history
               if h.get("open_interest") is not None]
        if ois and ois[0]:
            out["oi_change_pct"] = (self.open_interest - ois[0]) / ois[0]

        return out


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

    def exit_ctx(self, price, ts, settled=False):
        """Exit-only signals. `hold_to_settlement` is True exactly when the bar
        has reached settlement, so a spec can express "never sell early, take
        the resolution" as a real terminal condition. It was previously
        hardcoded False, which made every strategy using it unfireable."""
        return {
            "unrealized_pnl_pct": (self.mark(price) / self.cost) if self.cost else 0.0,
            "days_held": (ts - self.entry_ts) / DAY,
            "hold_to_settlement": bool(settled),
        }


class Book:
    """One agent's sub-book inside a portfolio manager.

    Its own capital and its own positions, so its P&L is its own. What it
    SHARES with its sibling agents is the PM's view (they read the same
    `model_probs`) and the PM's risk limits — which is exactly how a desk
    works: independent traders, one risk book.
    """

    __slots__ = ("name", "spec", "bankroll", "start_bankroll", "positions",
                 "pending_entry", "pending_exit", "committed", "spend_by_day",
                 "journal")

    def __init__(self, name, spec, bankroll, journal=None):
        self.name = name
        self.spec = spec
        self.bankroll = float(bankroll)
        self.start_bankroll = float(bankroll)
        self.positions = {}                   # ticker -> Position
        self.pending_entry = {}
        self.pending_exit = {}
        self.committed = 0.0
        self.spend_by_day = defaultdict(float)
        # What this PM has learned so far. Written when a bet settles, read
        # when the next one is sized — so the desk that has been wrong about a
        # league twenty times sizes down there, and the one that has never
        # traded it sizes normally. See wc/firm/journal.py.
        self.journal = journal

    def exposure(self):
        return sum(p.cost for p in self.positions.values()) + self.committed


def run(spec, bars, starting_bankroll=1000.0, costs=None,
        model_probs=None, history_window=20):
    """Replay ONE strategy. A thin wrapper over run_book, deliberately — a
    separate single-strategy loop would be a second implementation to drift."""
    return run_book([Book("_", spec, starting_bankroll)], bars, costs=costs,
                    model_probs=model_probs, history_window=history_window)


def run_book(books, bars, costs=None, model_probs=None, history_window=20,
             pm_caps=None):
    """Replay a portfolio manager's whole book over `bars`.

    `books` are the PM's agents, each with its own capital. `model_probs` is the
    PM's VIEW — one opinion, shared by its own agents and by nobody else's, which
    is the point of wc/firm/views.py.

    `pm_caps` are firm-level limits that bind ACROSS agents, on top of each
    agent's own spec caps. Two agents that each pass their own daily cap must
    still not breach the PM's.

    Bars are replayed chronologically across all markets AND all agents in one
    pass. Running each agent separately would mean PM-level caps could never
    bind, because the engine would never see two agents holding at once — the
    same reasoning that makes the market interleave load-bearing.

    Returns (trades, rejections). Every trade carries `agent`.
    """
    costs = costs or Costs()
    delay = costs.fill_delay_bars
    pm_caps = pm_caps or {}

    trades = []
    rejections = defaultdict(int)
    history = defaultdict(list)
    vol_24h = defaultdict(deque)
    last_view = {}
    pm_spend_by_day = defaultdict(float)

    def pm_exposure():
        return sum(b.exposure() for b in books)

    def pm_committed():
        """Capital the desk has decided to spend but not yet filled. Without
        this, three agents can each pass the PM's daily cap on the same bar and
        then all fill — the identical flaw the per-agent caps already guard."""
        return sum(b.committed for b in books)

    for row in bars:
        view = BarView(row, history=list(history[row["ticker"]]))
        mid = view.mid()
        if mid is None:
            rejections["no_price"] += 1
            continue

        history[view.ticker].append(
            {"price": mid, "open_interest": view.open_interest})
        if len(history[view.ticker]) > history_window:
            history[view.ticker].pop(0)

        window = vol_24h[view.ticker]
        window.append((view.ts, view.volume))
        cutoff = view.ts - 86400
        while window and window[0][0] < cutoff:
            window.popleft()
        vol_day = sum(v for _, v in window)

        last_view[view.ticker] = view
        mp = (model_probs or {}).get((view.ticker, view.ts))
        day = int(view.ts // 86400)

        for book in books:
            spec = book.spec
            side = spec["side"]
            caps = spec["caps"]
            max_positions = spec["sizing"].get("max_concurrent_positions")
            price = mid if side == "yes" else 1.0 - mid
            # Side-relative, like `price`: a NO agent's view is P(no), so its
            # edge is P(no) - price_no and its recorded model_prob matches
            # `outcome` (1 = OUR side won) in the journal's Brier.
            book_mp = mp if (mp is None or side == "yes") else 1.0 - mp

            ctx = view.to_ctx(side, model_prob=book_mp)
            ctx["volume_24h"] = vol_day

            def close_out(pos, rec):
                trades.append(rec)
                rec["agent"] = book.name
                book.bankroll += rec["net_pnl"] + pos.cost
                # The desk learns HERE, in the middle of the walk — not at the
                # end. A lesson recorded after the run would never have changed
                # a single decision, which is not learning, it is bookkeeping.
                if book.journal is not None and rec.get("settled"):
                    book.journal.record(rec["exit_ts"], rec.get("series"),
                                        rec.get("model_prob"), rec.get("outcome"),
                                        rec["net_pnl"])
                del book.positions[view.ticker]

            pos = book.positions.get(view.ticker)
            if pos and view.settled:
                close_out(pos, _settle(pos, view, costs))
                book.pending_exit.pop(view.ticker, None)
                continue

            if view.ticker in book.pending_exit:
                intent = book.pending_exit[view.ticker]
                intent["bars_left"] -= 1
                if intent["bars_left"] > 0:
                    continue
                del book.pending_exit[view.ticker]
                if _too_stale(intent, view, costs):
                    rejections["exit_expired_before_fill"] += 1
                    continue
                close_out(pos, _close(pos, view, price, costs))
                continue

            if view.ticker in book.pending_entry:
                intent = book.pending_entry[view.ticker]
                intent["bars_left"] -= 1
                if intent["bars_left"] > 0:
                    continue
                del book.pending_entry[view.ticker]
                book.committed -= intent["stake"]
                if _too_stale(intent, view, costs):
                    rejections["entry_expired_before_fill"] += 1
                    continue
                filled = _open_position(intent, view, side, costs,
                                        book.bankroll, rejections)
                if filled:
                    book.bankroll -= (filled.cost + filled.entry_fee)
                    book.spend_by_day[day] += filled.cost
                    pm_spend_by_day[day] += filled.cost
                    book.positions[view.ticker] = filled
                continue

            if pos:
                if interpret.should_exit(spec, ctx,
                                         pos.exit_ctx(price, view.ts, settled=False)):
                    if delay <= 0:
                        close_out(pos, _close(pos, view, price, costs))
                    else:
                        book.pending_exit[view.ticker] = {
                            "decided_ts": view.ts, "bars_left": delay}
                continue

            if not interpret.passes_universe(spec, ctx):
                rejections["universe_filter"] += 1
                continue
            if not interpret.evaluate(spec["entry"], ctx):
                rejections["entry_conditions"] += 1
                continue

            if max_positions and \
                    len(book.positions) + len(book.pending_entry) >= max_positions:
                rejections["max_concurrent_positions"] += 1
                continue

            stake = interpret.size(spec, ctx, book.bankroll)
            if stake <= 0:
                rejections["zero_size"] += 1
                continue
            if book.journal is not None:
                stake *= book.journal.size_multiplier(view.series)
                if stake <= 0:
                    rejections["journal_stood_down"] += 1
                    continue
            stake = min(stake, caps["per_market_dollars"])

            if book.spend_by_day[day] + book.committed + stake > caps["daily_spend_dollars"]:
                rejections["daily_cap"] += 1
                continue
            if book.exposure() + stake > caps["total_exposure_dollars"]:
                rejections["total_exposure_cap"] += 1
                continue

            # Firm-level limits, on top of the agent's own. Two agents that each
            # pass their own cap must still not breach the PM's.
            if "daily_spend_dollars" in pm_caps and \
                    pm_spend_by_day[day] + pm_committed() + stake \
                    > pm_caps["daily_spend_dollars"]:
                rejections["pm_daily_cap"] += 1
                continue
            if "total_exposure_dollars" in pm_caps and \
                    pm_exposure() + stake > pm_caps["total_exposure_dollars"]:
                rejections["pm_exposure_cap"] += 1
                continue

            intent = {"stake": stake, "decided_ts": view.ts, "model_prob": book_mp,
                      "series": view.series, "bars_left": delay}
            if delay <= 0:
                filled = _open_position(intent, view, side, costs,
                                        book.bankroll, rejections)
                if filled:
                    book.bankroll -= (filled.cost + filled.entry_fee)
                    book.spend_by_day[day] += filled.cost
                    pm_spend_by_day[day] += filled.cost
                    book.positions[view.ticker] = filled
            else:
                book.pending_entry[view.ticker] = intent
                book.committed += stake

    # Positions still open when the data ends must not vanish — that would
    # silently drop their P&L and flatter the result.
    #
    # A market whose close_time has passed and whose outcome we recorded is
    # SETTLED, not liquidated: paying it out at its last quoted price would
    # throw away the one fact that makes P&L, Brier, and calibration
    # computable. Only a genuinely unfinished market is marked to market.
    for book in books:
        for ticker, pos in book.positions.items():
            last = last_view.get(ticker)
            if last is None:
                continue

            finished = (last.close_time is not None and last.ts >= last.close_time)
            outcome = last.final_result
            if outcome in ("yes", "no", "void") and (finished or _past_close(last)):
                rec = _settle_final(pos, last, costs, outcome)
                rec["agent"] = book.name
                trades.append(rec)
                continue

            price = last.mid()
            if price is None:
                continue
            px = price if pos.side == "yes" else 1.0 - price
            gross = (px - pos.entry_price) * pos.contracts
            rec = _record(pos, last.ts, px, gross, 0.0, None, False)
            rec["liquidated_at_end"] = True
            rec["agent"] = book.name
            trades.append(rec)
            rejections["open_at_end"] += 1

    trades.sort(key=lambda t: t["exit_ts"])
    return trades, dict(rejections)


def _past_close(view):
    """The archive stops AT close_time, so the final bar sits just inside it.
    A market whose last bar is within one day of its close has finished for
    settlement purposes; one whose data ends weeks early genuinely has not."""
    if view.close_time is None:
        return False
    return (view.close_time - view.ts) <= DAY


def _settle_final(pos, view, costs, outcome):
    """Settle a still-open position at end of run, using the recorded outcome."""
    if outcome == "void":
        value = view.final_settlement_value
        value = pos.entry_price if value is None else value
        payout = value if pos.side == "yes" else (1.0 - value)
        rec = _record(pos, view.ts, payout,
                      (payout - pos.entry_price) * pos.contracts,
                      costs.settlement_fee, None, True)
        rec["voided"] = True
        return rec
    won = (outcome == "yes") if pos.side == "yes" else (outcome == "no")
    payout = 1.0 if won else 0.0
    return _record(pos, view.ts, payout,
                   (payout - pos.entry_price) * pos.contracts,
                   costs.settlement_fee, 1 if won else 0, True)


def _too_stale(intent, view, costs):
    """True when the fill bar arrived so long after the decision that the order
    would no longer have been sent. Guards the case where a market goes quiet
    and the "next bar" is hours away — filling there would be worse than not
    modelling latency at all."""
    limit = costs.max_fill_age_seconds
    return limit is not None and (view.ts - intent["decided_ts"]) > limit


def _open_position(intent, view, side, costs, bankroll, rejections):
    """Turn an intent into a Position at THIS bar's price, or return None with a
    counted reason. The decision was made on an earlier bar; the price is
    always the fill bar's."""
    fill = _fill_price(view, side, costs, buying=True)
    if fill is None or fill <= 0 or fill >= 1:
        rejections["no_fill_price"] += 1
        return None

    contracts = int(intent["stake"] / fill)
    if contracts < 1:
        rejections["below_one_contract"] += 1
        return None

    if costs.assume_fills:
        # Edge cases dismissed on purpose: no volume cap, and `fill_rate` of
        # the order fills regardless of what was resting at the quote.
        contracts = int(contracts * costs.fill_rate)
        if contracts < 1:
            rejections["below_one_contract"] += 1
            return None
    else:
        cap_by_volume = int((view.volume or 0) * costs.max_volume_share)
        if cap_by_volume < 1:
            rejections["insufficient_volume"] += 1
            return None
        contracts = min(contracts, cap_by_volume)

    cost = contracts * fill
    fee = costs.trade_fee(fill, contracts)
    if cost + fee > bankroll:
        rejections["insufficient_bankroll"] += 1
        return None

    return Position(view.ticker, view.series, side, contracts, fill, view.ts,
                    cost, fee, view.days_to_resolution(), intent["model_prob"])


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
    """Settlement. `view.result` is only readable here because the bar has
    passed close_time.

    Three outcomes, not two. A VOID — a cancelled or postponed game — pays the
    fair value Kalshi settled at rather than 0 or 1, so most of the stake comes
    back. Scoring it as a loss would understate every strategy by roughly the
    void rate, which on soccer is about 13% of settled markets.
    """
    if view.result == "void":
        value = view.settlement_value
        if value is None:                      # recorded void with no payout
            value = pos.entry_price            # assume flat rather than invent
        payout = value if pos.side == "yes" else (1.0 - value)
        gross = (payout - pos.entry_price) * pos.contracts
        rec = _record(pos, view.ts, payout, gross, costs.settlement_fee, None, True)
        # outcome is None on purpose: a void has no binary result, so it must
        # not enter Brier, log loss, or the calibration plot. A forecast is not
        # wrong because the match was called off.
        rec["voided"] = True
        return rec

    won = (view.result == "yes") if pos.side == "yes" else (view.result == "no")
    outcome = 1 if won else 0
    payout = 1.0 if won else 0.0
    gross = (payout - pos.entry_price) * pos.contracts
    return _record(pos, view.ts, payout, gross, costs.settlement_fee, outcome, True)
