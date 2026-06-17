"""
keeper.py — Agent 4 (Keeper): in-play exit manager.

While a game is live, the Keeper re-estimates the fair value of our position
every poll using the Brain's `evaluate_live` (analytic in-play WP + live market
price + throttled LLM read of the score), and decides whether to sell:

  • OVERPRICED  — market bid > live fair + sell_margin  → scale out (market is
                  paying more than the position is worth; strongest +EV signal)
  • TAKE_PROFIT — realized profit vs entry ≥ take_profit_pct → scale out
  • STOP        — live fair < stop_fair → exit (our outcome is dead per the LIVE
                  model; replaces the dumb price-floor panic sell)
  • HOLD        — otherwise; residual rides to resolution

Every action is logged to logs/exits.jsonl with a full snapshot so the Trainer
can later compute the counterfactual (exit vs. hold-to-maturity).

Reuses KalshiAdapter (price/position/sell) from scripts/inplay_exit.py.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "group"))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from group import live_feed, notify  # noqa: E402
from group.exit_rules import decide_exit, disarm  # noqa: E402

LOG_DIR = os.path.join(BASE_DIR, "logs")
EXITS_PATH = os.path.join(LOG_DIR, "exits.jsonl")


def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Keeper:
    def __init__(self, adapter, brain, market, entry_price, home, away,
                 config, dry_run=True):
        self.adapter = adapter
        self.brain = brain
        self.market = market          # dict: ticker, title, yes_sub_title, ...
        self.entry_price = entry_price
        self.home = home
        self.away = away
        self.config = config
        self.dry_run = dry_run
        self.remaining = None
        self._prev_score = None
        self._prev_llm = None
        self._halftime_done = False
        self._flags = {"_op_armed": True}   # re-arm state for the shared exit rules

    # ── decision ──────────────────────────────────────────────────────────────

    def _decide(self, bid, live_fair):
        """Re-armed exit via the shared group/exit_rules (the same logic the paper
        strategies use). Flags are disarmed by the caller only on a real sell."""
        return decide_exit(live_fair, bid, self.entry_price, self._flags, self.config)

    # ── halftime re-assessment (recommend-only) ─────────────────────────────────

    def _halftime_update(self, live, bid, gs):
        """At halftime, ping + log an add/hold/trim recommendation. No auto-buy."""
        lo = max(0.01, live["live_p_fair"] - live.get("sigma", 0.15))
        min_edge = self.config.get("min_edge", 0.03)

        ask_c = None
        client = getattr(self.adapter, "client", None)
        if client is not None:
            try:
                ask_c = client.get_best_ask_cents(self.market["ticker"])
            except Exception:
                pass
        ask = (ask_c if ask_c else round(bid * 100) + 1) / 100.0
        add_edge = lo - ask

        if add_edge >= min_edge:
            rec = (f"ADD looks justified — live edge {add_edge:+.3f} vs ask {ask:.2f}. "
                   f"Tell me to buy more if you want it.")
        elif live["live_p_fair"] < bid - 0.05:
            rec = (f"TRIM / consider exit — live fair {live['live_p_fair']:.2f} is below "
                   f"the {bid:.2f} bid; exit triggers remain armed.")
        else:
            rec = f"HOLD — no add edge (live edge {add_edge:+.3f}); exit triggers armed."

        notify.ping(
            "Halftime update",
            f"{self.market['ticker']} {gs['home_score']}-{gs['away_score']} "
            f"{gs['minute']}': fair {live['live_p_fair']:.2f}, bid {bid:.2f}. {rec}",
        )
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(EXITS_PATH, "a") as f:
            f.write(json.dumps({
                "ts": _utcnow_iso(), "ticker": self.market["ticker"],
                "action": "HALFTIME_REC", "live_fair": round(live["live_p_fair"], 4),
                "bid": bid, "ask": ask, "minute": gs["minute"],
                "score": f"{gs['home_score']}-{gs['away_score']}", "recommendation": rec,
            }) + "\n")

    # ── logging ────────────────────────────────────────────────────────────────

    def _log_exit(self, action, fraction, bid, live, gs):
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {
            "ts": _utcnow_iso(),
            "ticker": self.market["ticker"],
            "yes_sub_title": self.market.get("yes_sub_title", ""),
            "entry_price": self.entry_price,
            "action": action,
            "fraction_sold": fraction,
            "bid": bid,
            "live_fair": round(live["live_p_fair"], 4),
            "p_inplay": live["p_inplay"],
            "p_market": round(live["p_market"], 4),
            "p_llm": live["p_llm"],
            "minute": gs["minute"],
            "score": f"{gs['home_score']}-{gs['away_score']}",
            "remaining_after": self.remaining,
        }
        with open(EXITS_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # ── main loop ────────────────────────────────────────────────────────────

    def run(self):
        self.remaining = self.adapter.get_position()
        if not self.remaining or self.remaining < 0.5:
            print(f"  [KEEPER] no position on {self.market['ticker']}; nothing to manage.", flush=True)
            return
        print(f"  [KEEPER] managing {self.remaining:.0f} contracts of "
              f"{self.market['ticker']} (entry {self.entry_price}).", flush=True)

        poll = self.config.get("live_poll_secs", 60)
        while self.remaining and self.remaining >= 0.5:
            gs = live_feed.get_game_state(self.home, self.away)
            if gs is None:
                print("  [KEEPER] no live state yet; waiting...", flush=True)
                time.sleep(poll)
                continue
            if gs["status"] == "post":
                print("  [KEEPER] match final — holding residual to resolution.", flush=True)
                break
            if gs["status"] != "in":
                time.sleep(poll)
                continue

            bid = self.adapter.get_price()
            if bid is None:
                time.sleep(poll)
                continue
            # Refresh the live market price so evaluate_live's market component
            # reflects the current book, not the stale entry-time quote.
            self.market["yes_bid_cents"] = round(bid * 100)
            self.market["yes_ask_cents"] = None

            live = self.brain.evaluate_live(
                self.market, gs, prev_score=self._prev_score, prev_llm=self._prev_llm
            )
            self._prev_score = live["score_key"]
            self._prev_llm = live["llm_for_cache"]

            # Halftime re-assessment (once): recommend add/hold/trim — never auto-buys.
            if not self._halftime_done and 45 <= gs["minute"] <= 52:
                self._halftime_update(live, bid, gs)
                self._halftime_done = True

            action, fraction = self._decide(bid, live["live_p_fair"])
            print(f"  [KEEPER] {gs['home_score']}-{gs['away_score']} {gs['minute']}' | "
                  f"bid={bid:.3f} fair={live['live_p_fair']:.3f} -> {action}", flush=True)

            if action != "HOLD" and fraction > 0:
                # M1: sell whole contracts and track the integer actually sold,
                # so `remaining` doesn't drift from the real position.
                want = max(1, int(round(self.remaining * fraction)))
                want = min(want, int(round(self.remaining)))
                ok = self.adapter.sell(want, self.dry_run)
                if ok or self.dry_run:
                    self.remaining -= want
                    # Disarm only on a confirmed sell (failed sells retry next poll).
                    disarm(action, self._flags)
                    self._log_exit(action, want, bid, live, gs)
                    if action == "STOP" or self.remaining < 0.5:
                        notify.ping("Position exited",
                                    f"{self.market['ticker']} {action} @ {bid:.2f} "
                                    f"(fair {live['live_p_fair']:.2f})")
                        break
                    notify.ping("Partial exit",
                                f"{self.market['ticker']} {action}: sold {want} "
                                f"contracts @ {bid:.2f}, {self.remaining:.0f} left")

            time.sleep(poll)

        print(f"  [KEEPER] done. {self.remaining:.0f} contracts held to resolution.", flush=True)


# ── CLI entry point (the Trader launches this in the background) ───────────────

def main():
    import argparse
    from group.brain import Brain

    p = argparse.ArgumentParser(description="Keeper — in-play exit manager")
    p.add_argument("--ticker", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--yes-sub-title", default="")
    p.add_argument("--entry-price", type=float, required=True)
    p.add_argument("--home", required=True)
    p.add_argument("--away", required=True)
    p.add_argument("--yes-bid-cents", type=int, default=None)
    p.add_argument("--yes-ask-cents", type=int, default=None)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()

    config_path = os.path.join(BASE_DIR, "group", "config.json")
    with open(config_path) as f:
        config = json.load(f)
    state_path = os.path.join(BASE_DIR, "trainer_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
            for k in ("brain_weights", "exit_take_profit_pct", "exit_sell_margin",
                      "exit_stop_fair", "exit_fraction"):
                if k in state and state[k] is not None:
                    config[k] = state[k]
        except Exception:
            pass

    from inplay_exit import KalshiAdapter
    adapter = KalshiAdapter(args.ticker, key_id=os.environ.get("KALSHI_KEY_ID"),
                            api_key=os.environ.get("KALSHI_API_KEY"))
    adapter.setup()

    market = {
        "ticker": args.ticker,
        "title": args.title,
        "yes_sub_title": args.yes_sub_title,
        "yes_bid_cents": args.yes_bid_cents,
        "yes_ask_cents": args.yes_ask_cents,
    }
    brain = Brain(config)
    keeper = Keeper(adapter, brain, market, args.entry_price, args.home, args.away,
                    config, dry_run=not args.execute)
    keeper.run()


if __name__ == "__main__":
    main()
