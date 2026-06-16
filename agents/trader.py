"""
trader.py — Agent 1 (Trader).

Runs one full cycle: scan candidate markets, ask the Brain for a fair
probability, size with TVM-adjusted Kelly, place the order, and launch the
background exit monitor. Logs every decision (BET or PASS) to a daily JSONL.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Make sibling packages importable when run from anywhere
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "group"))

from group import scanner, kelly, notify  # noqa: E402

EBK_EXIT = os.path.join(BASE_DIR, "ebk-exit")
KEEPER = os.path.join(BASE_DIR, "agents", "keeper.py")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# "Czechia vs Mexico Winner?" -> ("Czechia", "Mexico")
_VS_RE = re.compile(
    r"([A-Z][A-Za-z .'-]+?)\s+(?:vs\.?|v\.?)\s+([A-Z][A-Za-z .'-]+?)(?:\s+Winner)?\??$",
    re.IGNORECASE,
)


def _event_prefix(ticker):
    """KXWCGAME-26JUN24CZEMEX-CZE -> KXWCGAME-26JUN24CZEMEX (the game)."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _utcnow():
    return datetime.now(timezone.utc)


def _utcnow_iso():
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class Trader:
    def __init__(self, client, config, brain, dry_run=True):
        self.client = client
        self.config = config
        self.brain = brain
        self.dry_run = dry_run

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _days_to_close(self, close_time):
        """Days from now until market close. 0 if unparseable/past."""
        if not close_time:
            return 0.0
        try:
            ct = close_time.replace("Z", "+00:00")
            close = datetime.fromisoformat(ct)
            if close.tzinfo is None:
                close = close.replace(tzinfo=timezone.utc)
            delta = (close - _utcnow()).total_seconds() / 86400.0
            return max(0.0, delta)
        except Exception:
            return 0.0

    def _build_decision(self, market, result, p_market, bet_dollars, tau_days):
        return {
            "ts": _utcnow_iso(),
            "ticker": market["ticker"],
            "title": market["title"],
            "yes_sub_title": market.get("yes_sub_title", ""),
            "close_time": market.get("close_time", ""),
            "tau_days": round(tau_days, 3),
            "p_market": round(p_market, 4),
            "p_fair": round(result["p_fair"], 4),
            "p_fair_lo": round(result["p_fair_lo"], 4),
            "p_fair_llm": result["p_fair_llm"],
            "p_fair_model": result["p_fair_model"],
            "p_fair_data": result["p_fair_data"],
            "sigma": round(result["sigma"], 4),
            "edge": round(result["p_fair_lo"] - p_market, 4),
            "bet_dollars": round(bet_dollars, 2),
            "action": "BET" if bet_dollars > 0 else "PASS",
            "dry_run": self.dry_run,   # so the Trainer can exclude preview rows
            "reasoning": result.get("reasoning", ""),
            "order_id": None,
            "fill_price": None,
            "contracts": None,
            # filled in by the Trainer after resolution:
            "resolved": False,
            "outcome": None,
            "pnl": None,
        }

    def _execute_and_monitor(self, market, decision, p_market):
        contracts = kelly.to_contracts(decision["bet_dollars"], p_market)
        # Enter at the ask (marketable limit) so the order actually fills.
        ok, order_id, fill_price = self.client.buy(
            market["ticker"], contracts,
            yes_price_cents=market.get("yes_ask_cents"),
            dry_run=self.dry_run,
        )
        decision["contracts"] = contracts
        if ok:
            decision["order_id"] = order_id
            decision["fill_price"] = fill_price
            self._launch_exit_monitor(market, fill_price)
        else:
            decision["action"] = "BET_FAILED"
        return ok

    def _launch_exit_monitor(self, market, entry_price):
        """
        Launch the Keeper (Agent 4) as a detached background process to manage
        the in-play exit. Falls back to the legacy ebk-exit monitor if the game's
        teams can't be parsed from the title.
        """
        m = _VS_RE.search(market.get("title", ""))
        if not m:
            # Not a parseable game market — use the legacy price-trigger monitor.
            self._launch_legacy_monitor(market)
            return

        home, away = m.group(1).strip(), m.group(2).strip()
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f"keeper_{market['ticker']}.log")
        cmd = [
            sys.executable, KEEPER,
            "--ticker", market["ticker"],
            "--title", market["title"],
            "--yes-sub-title", market.get("yes_sub_title", ""),
            "--entry-price", str(entry_price),
            "--home", home, "--away", away,
        ]
        if market.get("yes_bid_cents") is not None:
            cmd += ["--yes-bid-cents", str(market["yes_bid_cents"])]
        if not self.dry_run:
            cmd.append("--execute")
        try:
            with open(log_path, "a") as logf:
                subprocess.Popen(cmd, stdout=logf, stderr=logf,
                                 start_new_session=True)
            print(f"  [TRADER] Keeper launched for {market['ticker']} "
                  f"(log: {log_path})", flush=True)
        except Exception as e:
            print(f"  [TRADER WARN] Keeper launch failed: {e}", flush=True)

    def _launch_legacy_monitor(self, market):
        cmd = [
            sys.executable, EBK_EXIT,
            market["ticker"], market.get("close_time", ""),
            "--sell-high",   str(self.config["sell_high"]),
            "--sell-all-hi", str(self.config["sell_all_hi"]),
            "--sell-panic",  str(self.config["sell_panic"]),
            "--execute",
        ]
        try:
            subprocess.run(cmd, timeout=30)
        except Exception as e:
            print(f"  [TRADER WARN] legacy monitor launch failed: {e}", flush=True)

    # ── Main cycle ─────────────────────────────────────────────────────────────

    def run_once(self):
        """One scan-evaluate-bet cycle. Returns list of decision dicts."""
        try:
            balance = self.client.get_balance()
        except Exception as e:
            print(f"  [TRADER WARN] get_balance failed: {e} — assuming $0", flush=True)
            balance = 0.0

        candidates = scanner.scan(self.client, self.config)

        # Portfolio caps: limit concurrent positions and new $ deployed per scan.
        def _held(p):
            # New Kalshi API uses position_fp; fall back to legacy fields.
            for k in ("position_fp", "position", "market_exposure_dollars"):
                v = p.get(k)
                if v not in (None, ""):
                    try:
                        return abs(float(v)) > 0
                    except (TypeError, ValueError):
                        pass
            return False
        open_count = 0
        event_legs = {}   # legs already held per game — prevents offsetting bets
        try:
            for p in self.client.get_positions():
                if _held(p):
                    open_count += 1
                    ev = _event_prefix(p.get("ticker", ""))
                    event_legs[ev] = event_legs.get(ev, 0) + 1
        except Exception:
            pass
        max_open = self.config.get("max_open_positions", 8)
        max_cycle = self.config.get("max_cycle_dollars", 40.0)
        max_legs = self.config.get("max_legs_per_event", 1)
        cycle_spent = 0.0

        print(f"  Scanned {len(candidates)} candidate markets. Balance ${balance:.2f} | "
              f"open {open_count}/{max_open}, cycle ${max_cycle:.0f}, "
              f"max {max_legs} leg(s)/game", flush=True)

        decisions = []
        for market in candidates:
            result = self.brain.evaluate(market)
            # Entry cost = the ask (what we actually pay to buy YES). Edge and
            # sizing are computed against it. No ask → can't enter; force a pass.
            ask = market.get("yes_ask_cents")
            p_market = (ask if ask is not None else market["yes_bid_cents"]) / 100.0
            tau_days = self._days_to_close(market.get("close_time", ""))

            if ask is None:
                detail = {"bet": 0.0, "raw": 0.0, "capped": False,
                          "edge": result["p_fair_lo"] - p_market}
            else:
                detail = kelly.size_detail(
                    result["p_fair_lo"], p_market, balance, self.config, tau_days
                )
            bet_dollars = detail["bet"]
            decision = self._build_decision(
                market, result, p_market, bet_dollars, tau_days
            )
            decision["raw_kelly_dollars"] = round(detail["raw"], 2)
            decision["capped"] = detail["capped"]

            # Portfolio-cap gate (applies in dry-run too, so the preview is honest).
            event = _event_prefix(market["ticker"])
            if bet_dollars > 0:
                if event_legs.get(event, 0) >= max_legs:
                    # Already have a leg on this game — a 2nd leg would offset it.
                    decision["action"] = "PASS_CAP"
                    decision["cap_reason"] = (
                        f"already hold {max_legs} leg(s) on this game "
                        f"(no offsetting bets)"
                    )
                    bet_dollars = decision["bet_dollars"] = 0.0
                elif open_count >= max_open:
                    decision["action"] = "PASS_CAP"
                    decision["cap_reason"] = f"max_open_positions {max_open} reached"
                    bet_dollars = decision["bet_dollars"] = 0.0
                elif cycle_spent + bet_dollars > max_cycle:
                    decision["action"] = "PASS_CAP"
                    decision["cap_reason"] = (
                        f"max_cycle_dollars ${max_cycle:.0f} reached "
                        f"(${cycle_spent:.2f} already this cycle)"
                    )
                    bet_dollars = decision["bet_dollars"] = 0.0

            if bet_dollars > 0:
                # Ping only on bets that actually go through, if Kelly wanted more.
                if detail["capped"]:
                    max_bet = self.config.get("max_bet_dollars", 20.0)
                    notify.ping(
                        "Bet capped",
                        f"{market['ticker']}: Kelly wanted ${detail['raw']:.2f} "
                        f"(>{max_bet:.0f} cap). Betting ${bet_dollars:.2f}. "
                        f"edge={detail['edge']:+.3f}",
                        payload={"ticker": market["ticker"], "raw": round(detail["raw"], 2),
                                 "cap": max_bet, "bet": round(bet_dollars, 2)},
                    )
                # Count toward caps only on a confirmed fill (dry-run counts for
                # an honest preview). A failed/unfilled buy doesn't consume budget.
                filled = True
                if not self.dry_run:
                    filled = self._execute_and_monitor(market, decision, p_market)
                if filled:
                    open_count += 1
                    cycle_spent += bet_dollars
                    event_legs[event] = event_legs.get(event, 0) + 1

            log_decision(decision)
            _print_decision(decision)
            decisions.append(decision)

        return decisions


# ── Logging ──────────────────────────────────────────────────────────────────


def _log_path():
    os.makedirs(LOG_DIR, exist_ok=True)
    day = _utcnow().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"group_{day}.jsonl")


def log_decision(decision):
    with open(_log_path(), "a") as f:
        f.write(json.dumps(decision) + "\n")


def _print_decision(d):
    tag = d["action"]
    print(
        f"  [{tag:11s}] {d['ticker']:<40s} "
        f"p_mkt={d['p_market']:.2f} p_fair={d['p_fair']:.2f} "
        f"edge={d['edge']:+.3f} bet=${d['bet_dollars']:.2f}",
        flush=True,
    )
    if d["reasoning"]:
        print(f"               {d['reasoning']}", flush=True)
