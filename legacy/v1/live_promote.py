#!/usr/bin/env python3
"""
live_promote.py — the promotion patch-panel execution bus.

Routes a winning arena team's LIVE decisions to REAL Kalshi orders, one per
category "slot", governed by switchboard.json (the patch panel).

SAFE BY DEFAULT. A real order is placed ONLY when BOTH:
    1. switchboard.json  master.armed = true   (and master.kill = false), AND
    2. the process is run with  --execute
Otherwise it runs a DRY-RUN MIRROR: it computes and logs every order it WOULD
place (to logs/promote.jsonl) but places none. Phase 0 lives entirely here.

It reuses the paper arena's pricing/strategy/brains READ-ONLY (arena.py) and keeps
its own real-order placement, fuses, and reconciliation. The paper arena is never
touched. One cycle per invocation (cron/launchd), mirroring arena's cadence:

    python3 live_promote.py            # dry-run mirror (no orders)
    python3 live_promote.py --select   # just print the auto-selector table, exit
    python3 live_promote.py --execute  # real orders IFF switchboard master.armed
"""
import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import arena as A                                    # noqa: E402  (read-only reuse)
from group.env_portable import load_keys            # noqa: E402
from kalshi_client import KalshiClient               # noqa: E402
from group.strategy import build_strategy            # noqa: E402
from group.paper import PaperAccount                 # noqa: E402
from group import live_feed, promotion               # noqa: E402

SWITCHBOARD = os.path.join(BASE_DIR, "switchboard.json")
PSTATE = os.path.join(A.ARENA, "promote_state.json")
PLOG = os.path.join(BASE_DIR, "logs", "promote.jsonl")

DEFAULT_SB = {
    "master": {"armed": False, "kill": False, "daily_cap_dollars": 20.0},
    "slots": {},
}


# ── config / state / log ─────────────────────────────────────────────────────

def _load_sb():
    if not os.path.exists(SWITCHBOARD):
        return json.loads(json.dumps(DEFAULT_SB))
    sb = json.load(open(SWITCHBOARD))
    sb.setdefault("master", {}).setdefault("armed", False)
    sb["master"].setdefault("kill", False)
    sb["master"].setdefault("daily_cap_dollars", 20.0)
    sb.setdefault("slots", {})
    return sb


def _load_state():
    st = A._load(PSTATE, {})
    st.setdefault("date", "")
    st.setdefault("day_spent", 0.0)
    st.setdefault("slots", {})
    return st


def _save_state(st):
    A._save(PSTATE, st)


def _log(rec):
    os.makedirs(os.path.dirname(PLOG), exist_ok=True)
    rec = {"ts": int(time.time()), **rec}
    with open(PLOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ── plug resolution ──────────────────────────────────────────────────────────

def _resolve_plugs(sb, base_cfg, acfg):
    """{cat: {name, slot, state, brain, strategy}} for every active slot whose
    plugged team resolves. manual → pin; auto → gated selector."""
    sel = promotion.select_per_category()
    plugged = {}
    for cat, slot in sb["slots"].items():
        mode = slot.get("mode", "off")
        if mode == "off" or cat not in acfg["categories"]:
            continue
        name = slot.get("pin") if mode == "manual" else sel.get(cat)
        if not name:
            continue
        st = A._load(A._team_path(name), None)
        if not st:
            _log({"event": "PLUG_MISS", "cat": cat, "team": name})
            continue
        plugged[cat] = {
            "name": name, "slot": slot, "state": st,
            "brain": A._build_brain(base_cfg, cat, acfg["categories"][cat],
                                    A._load(A._brain_path(cat), {})),
            "strategy": build_strategy(st["kind"], name, st["params"]),
        }
    return plugged, sel


# ── one cycle ────────────────────────────────────────────────────────────────

def run_cycle(execute):
    load_keys()
    sb = _load_sb()
    master = sb["master"]
    base_cfg = json.load(open(A.GCONFIG))
    acfg = json.load(open(A.ACONFIG))
    client = KalshiClient()

    armed = bool(master.get("armed")) and not master.get("kill")
    live = bool(execute and armed)              # the ONLY thing that arms real orders
    mode = "LIVE" if live else "MIRROR"

    # Kelly scales the bet by available balance, so we must feed the strategy a
    # real number: the live wallet balance when armed, else a configured notional
    # (mirror decisions stay realistic). The per-slot max_bet/fuses still cap the
    # actual order regardless of wallet size.
    if live:
        try:
            wallet = client.get_balance()
        except Exception:
            wallet = master.get("notional_dollars", 200.0)
    else:
        wallet = master.get("notional_dollars", 200.0)

    if master.get("kill"):
        print("  [PROMOTE] master.kill is set — placing nothing.", flush=True)
        _log({"event": "KILL", "mode": "KILL"})
        return

    plugged, sel = _resolve_plugs(sb, base_cfg, acfg)
    print(f"=== PROMOTE [{mode}] selector picks: "
          + ", ".join(f"{c}:{n or '—'}" for c, n in sel.items()), flush=True)
    if not plugged:
        print("  [PROMOTE] no slot has an eligible/pinned team — nothing to do.", flush=True)
        return

    # state: mirror accounts (metadata + disarmed-mode gate) + daily fuse
    state = _load_state()
    today = time.strftime("%Y%m%d")
    if state["date"] != today:
        state = {"date": today, "day_spent": 0.0, "slots": state.get("slots", {})}
    day_spent = state["day_spent"]
    day_cap = master.get("daily_cap_dollars", 1e12)

    accounts = {}
    for cat, P in plugged.items():
        sd = state["slots"].get(cat, {})
        acct = (PaperAccount.from_dict(sd["account"]) if sd.get("account")
                else PaperAccount(f"slot_{cat}_{P['name']}", 0.0))
        # The mirror account is a POSITION LEDGER (enter-once guard + entry price /
        # re-arm flags for exits), NOT a wallet — real risk control is the fuses +
        # real positions. Fund it so PaperAccount.buy never rejects on cash.
        acct.cash = 1e9
        accounts[cat] = acct

    sb_board = live_feed.scoreboard_events("fifa.world")
    events_open = A._open_events(client, acfg)
    cycle_spent = {cat: 0.0 for cat in plugged}
    acted = []

    for prefix, ev in events_open.items():
        if not ev.get("home"):
            continue
        gs = live_feed.state_from_events(sb_board, ev["home"], ev["away"])
        if not gs:
            continue                              # not near-term on ESPN's board
        matchup = (ev["home"], ev["away"])
        mtotal = A._live_mtotal(client, acfg, ev)
        live_phase = gs["status"] == "in"

        for cat, P in plugged.items():
            if cat not in ev["cats"]:
                continue
            slot, brain, strat, acct = P["slot"], P["brain"], P["strategy"], accounts[cat]
            cat_cfg = acfg["categories"][cat]
            for leg in ev["cats"][cat]["legs"]:
                bid, ask = A._live_book(client, leg["ticker"])
                if bid is None and ask is None:
                    continue
                mkt = A._mkt(leg, ev["home"], ev["away"], bid, ask)
                _, r, comp = A.price(cat_cfg, brain, mkt, matchup, mtotal,
                                     gs if live_phase else None)
                if r is None:
                    continue
                tk = leg["ticker"]
                mpos = acct.positions.get(tk)
                # held: REAL position when live; mirror position when disarmed.
                held = (client.get_position_count(tk) if live
                        else (mpos["contracts"] if mpos else 0))

                if live_phase:
                    ctx = {"market": mkt, "pregame": None, "live": r, "ask": ask, "bid": bid,
                           "balance": wallet, "tau_days": 0, "game_state": gs}
                else:
                    ctx = {"market": mkt, "pregame": r, "live": None, "ask": ask, "bid": bid,
                           "balance": wallet, "tau_days": 1.0,
                           "game_state": {"status": "pre", "minute": 0, "home_score": 0,
                                          "away_score": 0, "home_team": ev["home"],
                                          "away_team": ev["away"]}}

                # ── EXIT (we already hold) ──
                if mpos:
                    action, frac = strat.exit_decision(mpos, ctx)
                    if action != "HOLD" and frac > 0 and bid:
                        have = int(held or mpos["contracts"])
                        want = max(1, min(int(round(have * frac)), have))
                        ok, oid, fill = client.sell_limit(tk, want, round(bid * 100), dry_run=not live)
                        if ok:
                            acct.sell(tk, want, fill or bid)
                            if action == "OVERPRICED":
                                mpos["_op_armed"] = False
                            elif action == "TAKE_PROFIT":
                                mpos["_tp_done"] = True
                            rec = {"event": "SELL", "mode": mode, "cat": cat, "team": P["name"],
                                   "ticker": tk, "sub": leg["sub"], "action": action,
                                   "contracts": want, "price": round(bid, 4),
                                   "order_id": oid, "placed": bool(live)}
                            _log(rec)
                            acted.append(f"{cat} {action} {leg['sub']} {want}@{bid:.2f}")
                    continue

                # ── ENTRY (flat) ──
                if (held or 0) > 0:
                    continue                      # real position w/o local metadata → leave it
                d = strat.entry_dollars(ctx)
                if d <= 0 or not ask:
                    continue
                if len(acct.positions) >= slot.get("max_open", 3):
                    _log({"event": "SKIP", "reason": "max_open", "cat": cat, "ticker": tk})
                    continue
                cyc_left = slot.get("fuse_per_cycle", 0.0) - cycle_spent[cat]
                day_left = day_cap - day_spent
                budget = min(d, slot.get("max_bet", 0.0), cyc_left, day_left)
                if budget < ask:                  # fuse won't fund even 1 contract
                    _log({"event": "SKIP", "reason": "fuse", "cat": cat, "ticker": tk,
                          "want_dollars": round(d, 2), "budget": round(budget, 2)})
                    continue
                n = max(1, int(budget / ask))
                ok, oid, fill = client.buy(tk, n, round(ask * 100), dry_run=not live)
                if ok:
                    fillp = fill or ask
                    acct.buy(tk, n, fillp, {"sub": leg["sub"]})
                    cost = n * fillp
                    cycle_spent[cat] += cost
                    day_spent += cost
                    rec = {"event": "BUY", "mode": mode, "cat": cat, "team": P["name"],
                           "ticker": tk, "sub": leg["sub"], "contracts": n,
                           "price": round(fillp, 4), "dollars": round(cost, 2),
                           "p_fair": round(r.get("p_fair", r.get("live_p_fair", 0.0)), 4),
                           "order_id": oid, "placed": bool(live)}
                    _log(rec)
                    acted.append(f"{cat} BUY {leg['sub']} {n}@{fillp:.2f} (${cost:.2f})")

    # persist mirror state + daily fuse
    state["day_spent"] = round(day_spent, 4)
    for cat in plugged:
        state["slots"].setdefault(cat, {})
        state["slots"][cat]["account"] = accounts[cat].to_dict()
        state["slots"][cat]["team"] = plugged[cat]["name"]
    _save_state(state)

    if acted:
        print(f"  [PROMOTE {mode}] " + "; ".join(acted), flush=True)
        print(f"  spent this cycle: " + ", ".join(f"{c}=${cycle_spent[c]:.2f}" for c in plugged)
              + f" | day ${day_spent:.2f}/{day_cap:.0f}", flush=True)
    else:
        print(f"  [PROMOTE {mode}] no action this cycle.", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Promotion patch-panel execution bus")
    ap.add_argument("--execute", action="store_true",
                    help="arm real orders (ONLY fires if switchboard master.armed=true)")
    ap.add_argument("--select", action="store_true", help="print selector table and exit")
    args = ap.parse_args()
    if args.select:
        load_keys()
        sel = promotion.select_per_category()
        for cat, rows in promotion.category_table().items():
            print(f"[{cat}] → {sel[cat] or '— none eligible'}")
            for r in rows:
                print(f"   {'✓' if r['eligible'] else '·'} {r['name']:<18} "
                      f"${r['pnl']:+7.2f} n={r['n_closed']:<3} brier={r['brier']} score={r['score']}")
        return
    run_cycle(args.execute)


if __name__ == "__main__":
    main()
