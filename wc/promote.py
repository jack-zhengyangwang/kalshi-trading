#!/usr/bin/env python3
"""
live_promote_v3.py — Arena v3 promotion execution bus (GAME_LINES, PRE-GAME).

Routes the v3 game_lines PRE-GAME ensemble (the edge validated by the trade-tape
replay: +$648 pre-game, 49% win) to REAL Kalshi orders, governed by
switchboard_v3.json. Deliberately separate from live_promote_v2.py so the (now
paused) winner runner is never touched.

╔═══════════════════════════════════════════════════════════════════════════╗
║ SAFE BY DEFAULT. A real order is placed ONLY when ALL THREE hold:          ║
║   1. switchboard_v3.json  master.armed = true                             ║
║   2. master.kill = false                                                   ║
║   3. this process is run with  --execute                                   ║
║ Otherwise: DRY-RUN MIRROR — logs every order it WOULD place + a paper      ║
║ ledger (logs/promote_v3.jsonl), places nothing.                            ║
╚═══════════════════════════════════════════════════════════════════════════╝

Differences vs v2: reads arena_v3_state (strategy_v3 teams), ranks by PRE-GAME
fitness, sizes with strategy_v3 (price band + unit cap + favorite-tilt +
market_focus), prices PRE-GAME ONLY, and re-applies the WC base-rate totals prior
so live pricing matches the replay.

    python3 live_promote_v3.py --select    # print the pre-game selector, exit
    python3 live_promote_v3.py             # DRY-RUN MIRROR (no orders)
    python3 live_promote_v3.py --execute   # REAL orders IFF master.armed (+!kill)
"""
import argparse
import datetime as dt
import json
import math
import os
from wc import paths
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from wc.kalshi.client_ext import KalshiClientV2
from wc.brain import BrainV2
from wc.lib.paper import PaperAccount
from wc.lib import kelly
import wc.scanner as scn
import wc.strategy as s3
import wc.markets as mv
import wc.arena as a3
from wc.core.promote_base import (_series_to_cat, _event_homeaway, _exit_decision,
                             _maintenance)

CAT = "game_lines"
SWITCHBOARD = paths.SWITCHBOARD_V3
STATE = os.path.join(paths.STATE_V3, "promote_state_v3.json")
PLOG = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
MIN_PREGAME_N = 10        # min resolved PRE-GAME bets before a team is promotable


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _log(rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(PLOG), exist_ok=True)
    with open(PLOG, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


ARMED_CATS = ["winner", "game_lines"]    # categories this promoter can arm (game_props
                                         # excluded — its pre-game forward P&L is negative)


def _brain(cat):
    """Same brain as the replay: tuned stacker weights, LLM off, so live pricing
    reproduces the validated edge. Per-category."""
    bw = _load(os.path.join(a3.STATE_V3, f"brain_{cat}.json"), None)
    bconf = {"use_llm": False}
    if bw:
        bconf["stacker_weights"] = bw
    return BrainV2(bconf)


def select(cat, n):
    """Top-n `cat` teams by FORWARD (out-of-sample) PRE-GAME fitness. Excludes the
    in-sample replay-seed games — arm ONLY on edge proven on games the population did
    NOT train on. Returns [] (refuse to arm) until a team has >=MIN_PREGAME_N forward
    pre-game bets with positive forward P&L."""
    seed = set(_load(os.path.join(a3.STATE_V3, "seed_events.json"), []))
    teams = _load(os.path.join(a3.STATE_V3, f"teams_{cat}.json"), [])
    board = []
    for t in teams:
        rows = [cb for cb in t["closed"]
                if cb.get("pnl") is not None and not cb.get("in_play")
                and "-" in cb["ticker"] and cb["ticker"].split("-")[1] not in seed]
        pn = len(rows)
        if pn < MIN_PREGAME_N:
            continue
        pp = sum(cb["pnl"] for cb in rows)
        if pp <= 0:
            continue
        staked = sum((cb.get("staked") or 0) for cb in rows)
        pf = pp / math.sqrt(staked + 1.0)
        board.append({"lineage": t["lineage"], "params": t["params"],
                      "focus": t.get("market_focus"), "pre_pnl": round(pp, 2),
                      "pre_n": pn, "pre_fit": round(pf, 3)})
    board.sort(key=lambda r: r["pre_fit"], reverse=True)
    return board[:n]


def _apply_base_rate(legs):
    """Re-apply the WC base-rate totals prior so live p_fair matches the replay."""
    for lg in legs:
        parsed = mv.parse_market_v2(lg["ticker"], lg.get("sub"))
        lg["_type"] = parsed.get("type")
        lg["_period"] = parsed.get("period")
        if parsed.get("type") == "total" and not lg.get("in_play"):
            lg["p_fair"] = a3._wc_total_pf(parsed, lg.get("sub"), lg["p_fair"])


def manage_exits(client, sb, state, manage_real, params_by_cat, brains, rows, ts):
    """Position-driven exit across ALL armed categories. SAFETY: runs on the REAL book
    whenever --execute is set, INDEPENDENT of master.armed — disarming/kill never
    abandons open real money. Per-category exit_mode + params + brain; 80% circuit-
    breaker; kill+kill_flattens flattens the REAL book. Returns False on a real
    position-read failure (caller aborts)."""
    master = sb["master"]
    hard = master.get("hard_stop_loss_pct", 0.8)
    kill_flat = master.get("kill") and master.get("kill_flattens")
    s2c = _series_to_cat()
    armed = set(params_by_cat)                        # cats we have params/brains for

    held, mirror_accts = {}, {}
    if manage_real:
        try:
            for p in client.get_positions():
                n = float(p.get("position_fp", 0) or 0)
                cat = s2c.get((p.get("ticker") or "").split("-")[0])
                if n <= 0 or cat not in armed:
                    continue
                tk, exp = p.get("ticker"), p.get("market_exposure")
                entry = (float(exp) / 100.0 / n) if exp else None
                if entry is not None and not (0.0 < entry < 1.0):
                    entry = None     # exposure unit looks wrong → don't trust breaker math
                held[tk] = {"n": n, "acct": None, "entry": entry, "cat": cat}
        except Exception as e:
            print(f"  [WARN] real position read FAILED ({e}) — skipping exits this cycle")
            return False
    else:
        for mkey, accd in (state.get("mirror") or {}).items():
            cat = mkey.split(":")[0]
            if cat not in armed:
                continue
            acc = PaperAccount.from_dict(accd)
            mirror_accts[mkey] = acc
            for tk, pos in acc.positions.items():
                held[tk] = {"n": pos["contracts"], "acct": mkey, "entry": pos["entry"], "cat": cat}
    if not held:
        return True

    ev_ha = _event_homeaway(client)
    sb_events = scn.lf.scoreboard_events()
    for tk, h in held.items():
        cat = h["cat"]
        params = params_by_cat.get(cat)
        slot = sb["slots"].get(cat, {})
        if params is None:
            continue
        m = client.get_market(tk)
        bid_c, _ = client.quote_cents(m)
        bid_d = (bid_c / 100.0) if bid_c else None
        parsed = mv.parse_market_v2(tk, m.get("yes_sub_title"))
        live_fair = None
        ha = ev_ha.get(tk.split("-")[1] if "-" in tk else None)
        if ha:
            home, away = ha
            stt = scn.lf.state_from_events(sb_events, home, away)
            if stt and stt.get("status") == "in":
                lp = brains[cat].live_prior(home, away, stt.get("minute") or 0,
                                            stt["home_score"], stt["away_score"])
                live_fair = brains[cat].live_pfair(parsed, lp, scn.leg_is_home(parsed, home, away))
        flags = state.setdefault("flags", {}).setdefault(tk, {})
        if kill_flat:
            action, frac = "KILL_FLATTEN", 1.0
        else:
            action, frac = _exit_decision(slot.get("exit_mode", "hold"), params,
                                          live_fair, bid_d, h["entry"], flags, hard)
        if action == "HOLD" or not bid_c:
            continue
        nsell = max(1, int(h["n"] * frac))
        if manage_real:
            ok, _, _ = client.sell_limit(tk, nsell, bid_c, dry_run=False)
            if not ok:
                continue                 # rested/cancelled — don't log a phantom exit
        else:
            mirror_accts[h["acct"]].sell(tk, nsell, bid_d)
        rows.append({"ts": ts, "mode": "REAL" if manage_real else "MIRROR", "cat": cat,
                     "act": action, "ticker": tk, "n": nsell, "bid": bid_c,
                     "entry": round(h["entry"], 3) if h["entry"] else None,
                     "live_fair": round(live_fair, 3) if live_fair is not None else None})
    if not manage_real:
        for mkey, acc in mirror_accts.items():
            state["mirror"][mkey] = acc.to_dict()
    return True


def _member_open_exposure(cat, team, real_open):
    """$ cost-basis a (cat, team) bot currently has tied up in still-OPEN real
    positions, read from the real-order log. Used to enforce a per-bot total
    exposure cap (open positions + new entries <= member_capital)."""
    path = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
    if not os.path.exists(path):
        return 0.0
    exp = 0.0
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if (r.get("mode") == "REAL-ORDERS" and r.get("act") == "BUY"
                and r.get("cat") == cat and r.get("team") == team
                and real_open.get(r.get("ticker"), 0) > 0):
            exp += r.get("cost", 0) or 0.0
    return exp


def run(execute=False):
    if _maintenance():
        print("kalshi maintenance window (3-5am ET) — skipping cycle")
        return
    sb = json.load(open(SWITCHBOARD))
    master = sb["master"]
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        from zoneinfo import ZoneInfo
        today = dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        today = ts[:10]
    executing = bool(execute)                        # we touch the REAL book this run
    real_entries = bool(master.get("armed") and execute and not master.get("kill"))
    mode_str = "REAL-ORDERS" if real_entries else "DRY-RUN MIRROR"

    # armed categories (slots that are on) → forward-only ensemble per category
    cats = [c for c in ARMED_CATS if sb["slots"].get(c, {}).get("mode", "off") != "off"]
    print(f"[live_promote_v3] {mode_str}  ({ts}; LA-day {today})  cats={cats}")
    members_by_cat, brains, params_by_cat = {}, {}, {}
    for c in cats:
        brains[c] = _brain(c)
        m = select(c, max(1, int(sb["slots"][c].get("ensemble", 1))))
        if m:
            members_by_cat[c] = m
            params_by_cat[c] = m[0]["params"]        # top member's params drive that cat's exits
        print(f"  {c}: " + (", ".join(f"{x['lineage']}(pre ${x['pre_pnl']:+.0f}/{x['pre_n']})"
                                      for x in m) if m else "no eligible team"))

    client = KalshiClientV2(req_per_sec=4)
    state = _load(STATE, {"mirror": {}, "daily": {}})
    daily_spent = state["daily"].get(today, 0.0)
    daily_cap = master.get("daily_cap_dollars", 0.0)

    # ONE real position read, shared by exits + entries; ABORT the cycle on failure
    # (acting on an unknown book risks double-buys / unmanaged exits).
    real_open = {}
    if executing:
        try:
            for p in client.get_positions():
                real_open[p.get("ticker")] = float(p.get("position_fp", 0) or 0)
        except Exception as e:
            print(f"  [ABORT] real position read failed ({e}) — no exits/entries this cycle")
            return

    rows = []
    # EXITS / KILL-FLATTEN: run on the real book whenever executing, regardless of
    # armed — disarming or kill must NEVER abandon open real positions.
    if manage_exits(client, sb, state, executing, params_by_cat, brains, rows, ts) is False:
        _log(rows); return
    if master.get("kill"):
        _save(STATE, state); _log(rows)
        print(f"  KILL active — flattened real book ({len(rows)} actions), no entries")
        return
    if executing and not real_entries:
        _save(STATE, state); _log(rows)
        print(f"  DISARMED — managed {len(rows)} real exits, placed NO new entries")
        return

    if not members_by_cat:
        state["daily"][today] = daily_spent; _save(STATE, state); _log(rows)
        print("  no eligible teams to enter")
        return

    # ENTRIES per armed category. live=True prices upcoming (pre-game) AND live
    # (in-play) games; each strategist bets per its own pregame/inplay flags.
    # SHARED: daily_spent (the daily cap), real_open, depth_left. PER-CAT: fuse, max_open.
    s2c = _series_to_cat()
    depth_left = {}                                  # live ask depth, shared across cats
    for c, members in members_by_cat.items():
        slot = sb["slots"][c]
        games = scn.price_games(c, client, brains[c], max_events=8, live=True)
        _apply_base_rate(sum((g["legs"] for g in games), []))
        only = set(slot.get("only_event_dates") or [])
        if only:
            games = [g for g in games if g.get("event", "")[:7] in only]
        # IN-PLAY FIRST so live games get slots before pre-game fills max_open
        games.sort(key=lambda g: not any(l.get("in_play") for l in g["legs"]))
        per_capital = slot.get("member_capital") or (slot["capital"] / max(1, len(members)))
        max_bet, max_open = slot.get("max_bet", 8.0), slot.get("max_open", 6)
        fuse = slot.get("fuse_per_cycle", 20.0)
        state.setdefault("ensemble", {})[c] = [m["lineage"] for m in members]
        for member in members:
            strat = s3.Strategist(member["params"], member.get("focus"))
            mkey = f"{c}:{member['lineage']}"
            mirror = (PaperAccount.from_dict(state["mirror"][mkey]) if state["mirror"].get(mkey)
                      else PaperAccount(mkey, per_capital))
            # per-bot TOTAL exposure cap ($ already tied up in open real positions +
            # any new entries this run must stay <= member_capital).
            member_cap = per_capital
            member_exp = (_member_open_exposure(c, member["lineage"], real_open)
                          if real_entries else 0.0)
            if real_entries:
                owned = set(real_open)
                cat_open = sum(1 for tk in real_open if real_open[tk] > 0
                               and s2c.get(tk.split("-")[0]) == c)
            else:
                owned = set(mirror.positions)
                cat_open = len(mirror.positions)
            cycle_spent = 0.0
            ev_seen = set()
            for g in games:
                for lg in g["legs"]:
                    tk, ask = lg["ticker"], lg["ask"]
                    if not ask or tk in owned or cat_open >= max_open:
                        continue
                    if lg.get("_period", "full") not in a3.BET_PERIODS:
                        continue                       # full-match legs only (no 1H/2H)
                    if g["event"] in ev_seen:          # one leg per event per member
                        continue
                    ip = lg.get("in_play", False)
                    if ip:                             # in-play near-certainty guard
                        nc = a3.NC_HI.get(c, a3.NC_DEFAULT)
                        if ask / 100.0 >= nc or lg["p_fair"] >= nc:
                            continue
                    depth = depth_left.get(tk, int(lg.get("ask_size", 0)))   # live book depth
                    if depth < 1:
                        continue
                    ctx = {"p_fair": lg["p_fair"], "ask": ask, "in_play": ip,
                           "minute": lg.get("minute"), "type": lg.get("_type"), "sigma": 0.12}
                    bet = min(strat.entry_size(ctx, per_capital), max_bet)
                    if bet <= 0:
                        continue
                    n = min(kelly.to_contracts(bet, ask / 100.0), depth)     # cap to live depth
                    if n < 1:
                        continue
                    cost = n * ask / 100.0
                    if member_exp + cost > member_cap:
                        continue                       # per-bot $100 total-exposure cap
                    if cycle_spent + cost > fuse:
                        continue
                    if real_entries and daily_cap and (daily_spent + cost) > daily_cap:
                        rows.append({"ts": ts, "cat": c, "act": "DAILY_CAP_HIT"})
                        continue
                    if real_entries:
                        ok, _, _ = client.buy(tk, n, ask, dry_run=False)
                        if ok:
                            daily_spent += cost
                            real_open[tk] = real_open.get(tk, 0) + n
                    else:
                        ok = mirror.buy(tk, n, ask / 100.0, {"sub": lg["sub"], "event": g["event"]})
                    if ok:
                        owned.add(tk); ev_seen.add(g["event"])
                        depth_left[tk] = depth - n          # deplete shared live depth
                        cycle_spent += cost; cat_open += 1; member_exp += cost
                        rows.append({"ts": ts, "mode": mode_str, "cat": c, "team": member["lineage"],
                                     "act": "BUY", "ticker": tk, "sub": lg["sub"], "n": n, "in_play": ip,
                                     "ask": ask, "edge": round(lg["p_fair"] - ask / 100.0, 3),
                                     "cost": round(cost, 2)})
            state["mirror"][mkey] = mirror.to_dict()
            print(f"  {c}/{member['lineage']}: cycle real spend ${cycle_spent:.2f}/{fuse}"
                  f" | exposure ${member_exp:.2f}/{member_cap:.0f}")

    state["daily"][today] = daily_spent
    _save(STATE, state); _log(rows)
    print(f"  orders/actions: {len(rows)} | daily real spend: ${daily_spent:.2f}/{daily_cap}")
    if not real_entries:
        print("  (mirror only — no real orders placed)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", action="store_true", help="print the pre-game selector, exit")
    ap.add_argument("--execute", action="store_true", help="place REAL orders IFF armed")
    args = ap.parse_args()
    if args.select:
        for c in ARMED_CATS:
            print(f"--- {c} ---")
            for m in select(c, 8):
                print(f"  {m['lineage']:<16} pre_fit={m['pre_fit']:+.3f} "
                      f"pre_pnl=${m['pre_pnl']:+.2f} ({m['pre_n']} bets) focus={m['focus'] or 'all'}")
    else:
        run(execute=args.execute)
