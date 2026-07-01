#!/usr/bin/env python3
"""
live_promote_v2.py — Arena v2 promotion execution bus.

Routes a winning arena_v2 team's LIVE decisions to REAL Kalshi orders, one per
category "slot", governed by switchboard_v2.json.

╔═══════════════════════════════════════════════════════════════════════════╗
║ SAFE BY DEFAULT. A real order is placed ONLY when ALL THREE hold:          ║
║   1. switchboard_v2.json  master.armed = true                             ║
║   2. master.kill = false                                                   ║
║   3. this process is run with  --execute                                   ║
║ Otherwise it runs a DRY-RUN MIRROR: computes + logs every order it WOULD   ║
║ place (logs/promote_v2.jsonl) and a paper mirror ledger, but places none.  ║
╚═══════════════════════════════════════════════════════════════════════════╝

Reuses scanner_v2 / brain_v2 / strategy_v2 / promotion_v2 read-only; the paper
arena is never touched. One cycle per invocation.

    python3 live_promote_v2.py --select    # print the selector table, exit
    python3 live_promote_v2.py             # DRY-RUN MIRROR (no orders)
    python3 live_promote_v2.py --execute   # REAL orders IFF master.armed (+!kill)
"""
import argparse
import datetime as dt
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from kalshi_client_v2 import KalshiClientV2
from brain_v2 import BrainV2
from group.paper import PaperAccount
from group import kelly
from group.exit_rules import decide_exit, disarm
import scanner_v2 as scn
import strategy_v2 as sv
import promotion_v2 as promo
import markets_v2 as mv

SWITCHBOARD = os.path.join(BASE, "switchboard_v2.json")
STATE = os.path.join(BASE, "arena_v2_state", "promote_state_v2.json")
PLOG = os.path.join(BASE, "logs", "promote_v2.jsonl")


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


def _brain(cat):
    sd = os.path.join(BASE, "arena_v2_state")
    bw = _load(os.path.join(sd, f"brain_{cat}.json"), None)
    lc = _load(os.path.join(sd, f"llm_{cat}.json"), {})
    cfg = json.load(open(os.path.join(BASE, "categories_v2.json")))
    use_llm = cfg["super_categories"].get(cat, {}).get("use_llm", False)
    bconf = {"use_llm": use_llm, "llm_cache": lc}
    if bw:
        bconf["stacker_weights"] = bw
    return BrainV2(bconf)


def _series_to_cat():
    cfg = json.load(open(os.path.join(BASE, "categories_v2.json")))["super_categories"]
    return {s: cat for cat, c in cfg.items() for s in c["series"]}


def _event_homeaway(client):
    """event_code -> (home, away) from KXWCGAME titles (open + settled)."""
    out = {}
    for status in ("open", "settled"):
        for m in client.list_markets_by_series("KXWCGAME", status=status):
            ec = m["ticker"].split("-")[1]
            if ec in out:
                continue
            h, a = scn.home_away(m.get("title"))
            if h:
                out[ec] = (h, a)
    return out


def _team_params(cat, slot):
    if slot.get("mode") == "manual" and slot.get("pin"):
        r = [x for x in promo.rank(cat) if x["lineage"] == slot["pin"]]
        t = r[0] if r else None
    else:
        t = promo.best(cat)
    return t["params"] if t else None


def _exit_decision(exit_mode, params, live_fair, bid_d, entry, flags, hard_pct):
    """Hardened exit: master circuit-breaker first (all modes), then per-category
    mode. 'hold' = ride to resolution, only a model-death STOP. 'active' = full
    evolved OVERPRICED/TAKE_PROFIT/STOP rules."""
    # circuit-breaker: position value collapsed to <= (1-hard)*entry  → flatten
    if entry and bid_d is not None and bid_d <= (1.0 - hard_pct) * entry:
        return ("CIRCUIT_BREAKER", 1.0)
    if exit_mode == "hold":
        stop = params.get("exit_stop_fair")
        if stop is not None and live_fair is not None and live_fair < stop:
            return ("STOP", 1.0)
        return ("HOLD", 0.0)
    if live_fair is None:
        return ("HOLD", 0.0)
    return decide_exit(live_fair, bid_d, entry, flags, params)


def manage_exits(client, sb, state, real_mode, rows, ts):
    """Position-DRIVEN exit pass: evaluates EVERY open position (real or mirror),
    not just scanned legs, so nothing is orphaned. Uses the real cost basis,
    the live model fair, the 80% circuit-breaker, and per-category exit mode.
    master.kill + kill_flattens → flatten everything."""
    master = sb["master"]
    hard = master.get("hard_stop_loss_pct", 0.8)
    kill_flat = master.get("kill") and master.get("kill_flattens")
    s2c = _series_to_cat()

    held, mirror_accts = {}, {}
    if real_mode:
        try:
            for p in client.get_positions():
                n = float(p.get("position_fp", 0) or 0)
                if n <= 0:
                    continue
                tk = p.get("ticker")
                exp = p.get("market_exposure")
                held[tk] = {"n": n, "cat": s2c.get((tk or "").split("-")[0]),
                            "entry": (float(exp) / 100.0 / n) if exp else None}
        except Exception as e:
            print(f"  [WARN] read positions: {e}")
    else:
        for cat, accd in (state.get("mirror") or {}).items():
            acc = PaperAccount.from_dict(accd)
            mirror_accts[cat] = acc
            for tk, pos in acc.positions.items():
                held[tk] = {"n": pos["contracts"], "cat": cat, "entry": pos["entry"]}
    if not held:
        return

    ev_ha = _event_homeaway(client)
    sb_events = scn.lf.scoreboard_events()
    brains, pcache = {}, {}
    for tk, h in held.items():
        cat = h["cat"]
        if not cat or cat not in sb["slots"] or sb["slots"][cat].get("mode") == "off":
            continue
        slot = sb["slots"][cat]
        if cat not in pcache:
            pcache[cat] = _team_params(cat, slot)
        params = pcache[cat]
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
                if cat not in brains:
                    brains[cat] = _brain(cat)
                stats = scn.lf.get_game_stats(home, away)
                csf = (stats["home"]["corners"] + stats["away"]["corners"]) if stats else 0
                lp = brains[cat].live_prior(home, away, stt.get("minute") or 0,
                                            stt["home_score"], stt["away_score"], stats=stats)
                live_fair = brains[cat].live_pfair(
                    parsed, lp, scn.leg_is_home(parsed, home, away), csf)
        flags = state.setdefault("flags", {}).setdefault(tk, {})
        if kill_flat:
            action, frac = "KILL_FLATTEN", 1.0
        else:
            action, frac = _exit_decision(slot.get("exit_mode", "active"), params,
                                          live_fair, bid_d, h["entry"], flags, hard)
        if action == "HOLD" or not bid_c:
            continue
        nsell = max(1, int(h["n"] * frac))
        if real_mode:
            ok, _, _ = client.sell_limit(tk, nsell, bid_c, dry_run=False)
        else:
            ok = mirror_accts[cat].sell(tk, nsell, bid_d)
        if ok and action in ("OVERPRICED", "TAKE_PROFIT"):
            disarm(action, flags)
        rows.append({"ts": ts, "mode": "REAL" if real_mode else "MIRROR", "cat": cat,
                     "act": action, "ticker": tk, "n": nsell, "bid": bid_c,
                     "entry": round(h["entry"], 3) if h["entry"] else None,
                     "live_fair": round(live_fair, 3) if live_fair is not None else None})
    if not real_mode:
        for cat, acc in mirror_accts.items():
            state["mirror"][cat] = acc.to_dict()


def _maintenance():
    """Kalshi daily maintenance 3:00–5:00am ET — skip real trading then."""
    try:
        from zoneinfo import ZoneInfo
        return 3 <= dt.datetime.now(ZoneInfo("America/New_York")).hour < 5
    except Exception:
        return False


def run(execute=False):
    if _maintenance():
        print("kalshi maintenance window (3-5am ET) — skipping promotion cycle")
        return
    sb = json.load(open(SWITCHBOARD))
    master = sb["master"]
    real_mode = bool(master.get("armed") and execute and not master.get("kill"))
    mode_str = "REAL-ORDERS" if real_mode else "DRY-RUN MIRROR"
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    try:                                   # daily cap rolls over at LA (Pacific) midnight
        from zoneinfo import ZoneInfo
        today = dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        today = ts[:10]
    print(f"[live_promote_v2] {mode_str}  ({ts}; LA-day {today})")

    client = KalshiClientV2(req_per_sec=4)
    state = _load(STATE, {"mirror": {}, "daily": {}})
    daily_spent = state["daily"].get(today, 0.0)
    daily_cap = master.get("daily_cap_dollars", 0.0)

    # real open positions (once), to avoid double-entry and to manage exits
    real_open = {}
    if real_mode:
        try:
            for p in client.get_positions():
                real_open[p.get("ticker")] = float(p.get("position_fp", 0) or 0)
        except Exception as e:
            print(f"  [WARN] could not read real positions: {e}")

    rows = []
    # EXITS FIRST — position-driven, hardened (covers every open position).
    manage_exits(client, sb, state, real_mode, rows, ts)
    if master.get("kill"):
        state["daily"][today] = daily_spent
        _save(STATE, state)
        _log(rows)
        print(f"  KILL active — flattened/blocked; {len(rows)} exit actions, no entries")
        return

    for cat, slot in sb["slots"].items():
        if slot.get("mode") == "off" or slot.get("capital", 0) <= 0:
            continue
        # promoted team(s): ensemble of the top-N eligible (or a manual pin)
        ensemble = max(1, int(slot.get("ensemble", 1)))
        if slot.get("mode") == "manual" and slot.get("pin"):
            members = [r for r in promo.rank(cat) if r["lineage"] == slot["pin"]][:1]
        else:
            members = promo.top(cat, ensemble)
        if not members:
            print(f"  {cat}: no eligible team — skipped")
            continue

        # dynamic ensemble: re-picks the current top-N paper teams each cycle and
        # logs any substitution vs last cycle (auto promote/demote from the arena).
        prev = set(state.get("ensemble", {}).get(cat, []))
        cur = [m["lineage"] for m in members]
        if prev and set(cur) != prev:
            rows.append({"ts": ts, "cat": cat, "act": "ENSEMBLE_CHANGE",
                         "added": list(set(cur) - prev), "dropped": list(prev - set(cur))})
            print(f"  {cat}: ensemble change +{list(set(cur)-prev)} -{list(prev-set(cur))}")
        state.setdefault("ensemble", {})[cat] = cur

        # fixed $ per member if set (honors "$100 each"), else split the slot capital
        per_capital = slot.get("member_capital") or (slot["capital"] / len(members))
        max_bet = slot.get("max_bet", 5.0)
        max_open = slot.get("max_open", 5)
        fuse = slot.get("fuse_per_cycle", 10.0)
        brain = _brain(cat)
        games = scn.price_games(cat, client, brain, max_events=4, live=True)
        s2c = _series_to_cat()

        for member in members:                              # each ensemble member: own $ + own ledger
            strat = sv.Strategist(member["params"])
            mkey = f"{cat}:{member['lineage']}"
            mirror = (PaperAccount.from_dict(state["mirror"][mkey]) if state["mirror"].get(mkey)
                      else PaperAccount(mkey, per_capital))
            if real_mode:
                owned = set(real_open)                      # shared book: avoid exact-ticker double-buy
                cat_open = sum(1 for tk in real_open if real_open[tk] > 0
                               and s2c.get(tk.split("-")[0]) == cat)
            else:
                owned = set(mirror.positions)
                cat_open = len(mirror.positions)
            cycle_spent = 0.0
            for g in games:
                for lg in g["legs"]:
                    tk, ask = lg["ticker"], lg["ask"]
                    if not ask or tk in owned or cat_open >= max_open:
                        continue
                    edge = lg["p_fair"] - ask / 100.0
                    ctx = {"p_fair": lg["p_fair"], "ask": ask,
                           "in_play": lg.get("in_play", False), "minute": lg.get("minute")}
                    bet = min(strat.entry_size(ctx, per_capital), max_bet)
                    if bet <= 0:
                        continue
                    n = kelly.to_contracts(bet, ask / 100.0)
                    cost = n * ask / 100.0
                    if cycle_spent + cost > fuse:
                        continue
                    if real_mode and daily_cap and (daily_spent + cost) > daily_cap:
                        rows.append({"ts": ts, "mode": mode_str, "cat": cat, "act": "DAILY_CAP_HIT"})
                        continue
                    if real_mode:
                        ok, _, _ = client.buy(tk, n, ask, dry_run=False)
                        if ok:
                            daily_spent += cost
                            real_open[tk] = real_open.get(tk, 0) + n
                    else:
                        ok = mirror.buy(tk, n, ask / 100.0, {"sub": lg["sub"], "event": g["event"]})
                    if ok:
                        owned.add(tk)
                        cycle_spent += cost
                        cat_open += 1
                        rows.append({"ts": ts, "mode": mode_str, "cat": cat, "team": member["lineage"],
                                     "act": "BUY", "ticker": tk, "sub": lg["sub"], "n": n,
                                     "ask": ask, "edge": round(edge, 3), "cost": round(cost, 2)})
            state["mirror"][mkey] = mirror.to_dict()
            print(f"  {cat}/{member['lineage']}: spent ${cycle_spent:.2f} (cap ${per_capital:.0f})")

    state["daily"][today] = daily_spent
    _save(STATE, state)
    _log(rows)
    print(f"  orders/actions this cycle: {len(rows)} | daily real spend: ${daily_spent:.2f}"
          f"/{daily_cap}")
    if not real_mode:
        print("  (mirror only — no real orders placed)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", action="store_true", help="print selector table and exit")
    ap.add_argument("--execute", action="store_true", help="place REAL orders IFF armed")
    args = ap.parse_args()
    if args.select:
        promo.table()
    else:
        run(execute=args.execute)
