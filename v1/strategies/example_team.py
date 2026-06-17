"""
example_team.py — a TEMPLATE for adding your own agent team.

An "agent team" is a Strategy: it shares the Brain's fair-value estimate and only
decides HOW to act on it — when to enter, how big, and when to exit. Copy this
class into group/strategy.py, register it (see ADDING_AN_AGENT.md), point a team
at it in tournament_config.json, and the arena will run it against every game.

The base class (group/strategy.py::Strategy) gives you:
    self.p                       # your team's params dict (from tournament_config.json)
    self._kelly_dollars(prob, ask, balance, tau_days, min_edge=None)
                                 # fractional-Kelly + TVM sizing; returns $ to bet (0 = skip)
    self.exit_decision(pos, ctx) # shared re-armed exit (overpriced / take-profit / stop)

The arena calls your team once per market per cycle with `ctx`:
    ctx = {
      "market":     {...},       # the Kalshi market (ticker, yes_sub_title, bid/ask cents)
      "pregame":    {...}|None,  # Brain fair value BEFORE kickoff (has p_fair, p_fair_lo, sigma)
      "live":       {...}|None,  # Brain fair value DURING the game (has live_p_fair, sigma)
      "ask":        float|None,  # current YES ask in dollars (what you pay to buy)
      "bid":        float|None,  # current YES bid in dollars (what you get to sell)
      "balance":    float,       # cash available to size against
      "tau_days":   float,       # days to resolution (for the time-value discount)
      "game_state": {...},       # {status: pre|in, minute, home_score, away_score, ...}
    }

Return a dollar amount to bet (0 = pass). The arena converts $ → integer contracts
at the ask, places the (paper) order, and later asks you to exit via exit_decision.
"""
from group.strategy import Strategy, _prob


class ValueContrarian(Strategy):
    """EXAMPLE: bet only when the model STRONGLY disagrees with the market, then hold.

    Idea: the edge is biggest where our fair value is far from the market price. So
    enter pre-game only when |model - market| clears a `disagree` threshold, size by
    Kelly on the conservative lower bound, and hold to resolution (stop-loss only).
    A deliberately *selective* team — the opposite of spraying small bets.
    """

    def entry_dollars(self, ctx):
        r = ctx.get("pregame")
        ask = ctx["ask"]
        if r is None or ask is None:               # only acts pre-game, needs a price
            return 0.0

        p_model = r.get("p_fair")                  # blended fair value
        p_market = r.get("p_fair_data", ask)       # the market's own view (mid)
        if p_model is None:
            return 0.0

        # Selectivity gate: skip unless model and market disagree by `disagree`.
        if abs(p_model - p_market) < self.p.get("disagree", 0.10):
            return 0.0

        # Bet the conservative lower bound (haircut for uncertainty), Kelly-sized.
        prob = _prob(r, self.p.get("entry_prob", "lo"))
        return self._kelly_dollars(prob, ask, ctx["balance"], ctx["tau_days"])

    # exit_decision is inherited → uses your params' exit_* thresholds.
    # Override it here if you want custom exit logic.


# Suggested params block to drop into tournament_config.json (see ADDING_AN_AGENT.md):
#   {
#     "name": "E_value_contrarian",
#     "kind": "value_contrarian",
#     "params": {
#       "kelly_fraction": 0.20, "min_edge": 0.03, "entry_prob": "lo",
#       "disagree": 0.10, "max_bet_dollars": 10.0, "min_bet_dollars": 1.0,
#       "exit_stop_fair": 0.10
#     }
#   }
