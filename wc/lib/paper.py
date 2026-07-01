"""
paper.py — a simulated trading account for the paper tournament.

No real orders ever. Fills happen at the real market bid/ask (passed in by the
caller), positions settle at $1/$0 on resolution. Tracks cash, realized and
unrealized PnL so teams can be ranked. Serializable to/from JSON for persistence.
"""


class PaperAccount:
    def __init__(self, name, starting_cash=200.0):
        self.name = name
        self.starting_cash = starting_cash
        self.cash = starting_cash
        # ticker -> {contracts, entry, title, yes_sub_title, home, away}
        self.positions = {}
        self.realized_pnl = 0.0
        self.trades = 0          # number of fills
        self.settled = 0         # number of resolved positions

    # ── trading (simulated) ──────────────────────────────────────────────────

    def buy(self, ticker, contracts, price, meta=None):
        """Buy `contracts` YES @ `price` (dollars). Averages into any existing position."""
        contracts = int(contracts)
        if contracts <= 0 or not (0 < price < 1):
            return False
        cost = contracts * price
        if cost > self.cash + 1e-9:
            # size down to available cash rather than going negative
            contracts = int(self.cash / price)
            if contracts <= 0:
                return False
            cost = contracts * price
        self.cash -= cost
        p = self.positions.get(ticker)
        if p:
            total = p["contracts"] + contracts
            p["entry"] = (p["entry"] * p["contracts"] + price * contracts) / total
            p["contracts"] = total
        else:
            p = {"contracts": contracts, "entry": price}
            p.update(meta or {})
            self.positions[ticker] = p
        self.trades += 1
        return True

    def sell(self, ticker, contracts, price):
        """Sell `contracts` of an open position @ `price` (dollars). Realizes PnL."""
        p = self.positions.get(ticker)
        if not p:
            return False
        contracts = min(int(contracts), p["contracts"])
        if contracts <= 0 or not (0 <= price < 1):
            return False
        self.cash += contracts * price
        self.realized_pnl += contracts * (price - p["entry"])
        p["contracts"] -= contracts
        self.trades += 1
        if p["contracts"] <= 0:
            del self.positions[ticker]
        return True

    def settle(self, ticker, outcome):
        """Resolve a held position: outcome 1 (YES won) pays $1/contract, else $0."""
        p = self.positions.pop(ticker, None)
        if not p:
            return 0.0
        payoff = 1.0 if outcome == 1 else 0.0
        self.cash += p["contracts"] * payoff
        pnl = p["contracts"] * (payoff - p["entry"])
        self.realized_pnl += pnl
        self.settled += 1
        return pnl

    # ── valuation ─────────────────────────────────────────────────────────────

    def unrealized_pnl(self, mark_prices):
        """mark_prices: {ticker: current_bid_dollars}. Value open positions at the bid."""
        total = 0.0
        for tk, p in self.positions.items():
            mark = mark_prices.get(tk, p["entry"])
            total += p["contracts"] * (mark - p["entry"])
        return total

    def equity(self, mark_prices):
        """Cash + mark-to-market value of open positions."""
        held = sum(p["contracts"] * mark_prices.get(tk, p["entry"])
                   for tk, p in self.positions.items())
        return self.cash + held

    def total_pnl(self, mark_prices=None):
        return self.realized_pnl + (self.unrealized_pnl(mark_prices or {}))

    # ── persistence ─────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            "name": self.name,
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "positions": self.positions,
            "realized_pnl": self.realized_pnl,
            "trades": self.trades,
            "settled": self.settled,
        }

    @classmethod
    def from_dict(cls, d):
        a = cls(d["name"], d.get("starting_cash", 200.0))
        a.cash = d.get("cash", a.starting_cash)
        a.positions = d.get("positions", {})
        a.realized_pnl = d.get("realized_pnl", 0.0)
        a.trades = d.get("trades", 0)
        a.settled = d.get("settled", 0)
        return a
