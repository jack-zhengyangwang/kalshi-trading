"""
cycle.py — Unified v3 loop: one pricing pass serves BOTH the paper arena AND the
real-money promoter. Eliminates the 3x duplicated entry/exit/settle paths.

    python3 -m wc.cycle              # paper arena + dry-run promoter mirror
    python3 -m wc.cycle --execute    # paper arena + REAL promoter (if armed)

Replaces: arena.py --once  +  promote.py [--execute]  (two crons, duplicated pricing).
After: one cron fires this; paper teams evolve, real armed agents trade the same scan.
"""
import argparse
import datetime as dt
import json
import os
import random
import sys

from wc import paths
from wc.kalshi.client_ext import KalshiClientV2
from wc.lib.paper import PaperAccount
from wc.lib import kelly
import wc.arena as a3
import wc.brain as b2
import wc.markets as mv
import wc.promote as promo
import wc.scanner as scn
import wc.strategy as s3
import wc.prediction_db as pdb
import wc.core.arena_base as A

# ── paths ──────────────────────────────────────────────────────────────────────

STATE_V3      = a3.STATE_V3
TALLY_V3      = a3.TALLY_V3
EVO_LOG_V3    = a3.EVO_LOG_V3
SWITCHBOARD   = paths.SWITCHBOARD_V3
PROMOTE_STATE = os.path.join(STATE_V3, "promote_state_v3.json")
PLOG          = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
SEED_CATS     = a3.SEED_CATS
LLM_CATS      = a3.LLM_CATS
POP_PER_CAT   = a3.POP_PER_CAT

_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


# ── helpers ────────────────────────────────────────────────────────────────────

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


def _event_tau_days(event, now=None):
    """Days until a game resolves, from the DATE in its event code."""
    try:
        y = 2000 + int(event[0:2]); mo = _MON[event[2:5]]; d = int(event[5:7])
        game = dt.datetime(y, mo, d, 23, 59, tzinfo=dt.timezone.utc)
        now = now or dt.datetime.now(dt.timezone.utc)
        return (game - now).total_seconds() / 86400.0
    except Exception:
        return 999.0


# ── unified pricing (dual-frequency per-game scanner from promoter) ────────────

def _price_surface(client, brains, state, today, use_llm=False):
    """One pricing pass for ALL consumers. Dual-frequency per-game scan:
    full Soccer surface for NEAR games only. Live games every cycle,
    pre-game games once per day. Returns the priced games list.
    When use_llm=True, creates an LLM-enabled brain for pricing
    (real money gets the extra signal; paper can use it too)."""
    gseries = scn.game_series(client)
    sb_events = scn.lf.scoreboard_events()
    scanned = state.setdefault("pregame_scanned", {})
    targets = set()
    for m in scn.iter_game_series(client):
        gc = scn.event_code(m["ticker"])
        if not gc or _event_tau_days(gc) > 2.0:         # 48h horizon
            continue
        h, a = scn.home_away(m.get("title"))
        st = scn.lf.state_from_events(sb_events, h, a) if h else None
        if st and st.get("status") == "in":
            targets.add(gc)                              # in-play → every cycle
        elif scanned.get(gc) != today:                   # pre-game → once per day
            targets.add(gc); scanned[gc] = today
    # Use the best available brain. When executing with real money, enable LLM
    # for the pricing brain (extra signal on top of structural model).
    if use_llm:
        scan_brain = b2.BrainV2({"use_llm": True})
    else:
        scan_brain = brains.get("game_lines") or brains.get("winner") or next(iter(brains.values()))
    all_games = (scn.price_games(None, client, scan_brain, max_events=len(targets) + 1,
                                 live=True, series=gseries, only_games=targets)
                 if targets else [])
    # Apply WC base-rate totals prior so live pricing matches the replay
    a3._base_rate_legs(sum((g["legs"] for g in all_games), []))
    print(f"  scan: {len(all_games)} game(s) full-surface, targets={sorted(targets)}"
          + (" [LLM on]" if use_llm else ""))
    return all_games


# ── entry: paper teams ─────────────────────────────────────────────────────────

def _enter_paper(teams_by_cat, all_games):
    """Paper teams bet on the shared priced surface. Same logic as arena._enter_live
    but consumes the single pricing pass. Returns count of placed bets."""
    placed = 0
    depth_left = {}   # shared ask depth across ALL paper teams + categories
    for cat, teams in teams_by_cat.items():
        for g in all_games:
            for lg in g["legs"]:
                tk, ask = lg["ticker"], lg["ask"]
                in_play = lg.get("in_play", False)
                if not ask or not a3._window_open(lg.get("_period", "full"),
                                                  lg.get("minute"), in_play):
                    continue
                if in_play:
                    nc = a3.NC_HI.get(cat, a3.NC_DEFAULT)
                    if ask / 100.0 >= nc or lg["p_fair"] >= nc:
                        continue
                depth = depth_left.get(tk, int(lg.get("ask_size", 0)))
                if depth < 1:
                    continue
                ctx = {"p_fair": lg["p_fair"], "ask": ask, "in_play": in_play,
                       "minute": lg.get("minute"), "type": lg.get("_type"), "sigma": 0.12}
                tau = _event_tau_days(g["event"])
                for t in teams:
                    if depth < 1:
                        break
                    if tk in t["bets"] or len(t["bets"]) >= a3.MAX_OPEN:
                        continue
                    if any(b.get("event") == g["event"] for b in t["bets"].values()):
                        continue
                    acc = PaperAccount.from_dict(t["account"])
                    stake = s3.Strategist(t["params"], t.get("market_focus")).entry_size(
                        ctx, acc.cash, tau_days=tau)
                    if stake <= 0:
                        continue
                    n = min(kelly.to_contracts(stake, ask / 100.0), depth)
                    if n < 1 or not acc.buy(tk, n, ask / 100.0,
                                            {"sub": lg["sub"], "event": g["event"]}):
                        continue
                    depth -= n
                    cost = n * ask / 100.0
                    t["staked"] += cost
                    t["bets"][tk] = {
                        "sources": lg.get("sources", {}), "p_fair": lg["p_fair"],
                        "ask": ask, "edge": round(lg["p_fair"] - ask / 100.0, 4),
                        "contracts": n, "cost": round(cost, 3), "event": g["event"],
                        "in_play": lg.get("in_play", False), "flags": {}}
                    t["account"] = acc.to_dict()
                    placed += 1
        depth_left[tk] = depth
    return placed


# ── exit: paper teams ──────────────────────────────────────────────────────────

def _exit_paper(teams_by_cat, all_games):
    """Paper teams exit open positions in live games. Same logic as arena._exit_live."""
    exited = 0
    live_legs = {}
    for g in all_games:
        for lg in g["legs"]:
            if lg.get("in_play"):
                live_legs[lg["ticker"]] = lg
    if not live_legs:
        return 0
    bid_left = {tk: int(lg.get("bid_size", 0)) for tk, lg in live_legs.items()}
    for cat, teams in teams_by_cat.items():
        for t in teams:
            acc = PaperAccount.from_dict(t["account"])
            changed = False
            for tk, lg in live_legs.items():
                if tk not in t["bets"]:
                    continue
                pos = acc.positions.get(tk)
                if not pos:
                    continue
                flags = t["bets"][tk].setdefault("flags", {})
                carrier = {"entry": pos["entry"], **flags}
                action, frac = s3.Strategist(t["params"], t.get("market_focus")).exit_decision(
                    carrier, lg["p_fair"], lg.get("bid"))
                if action != "HOLD" and lg.get("bid"):
                    n = min(max(1, int(pos["contracts"] * frac)), bid_left.get(tk, 0))
                    if n >= 1 and acc.sell(tk, n, lg["bid"] / 100.0):
                        bid_left[tk] -= n
                        s3.disarm(action, carrier)
                        changed = True; exited += 1
                if tk in t["bets"]:
                    if tk not in acc.positions:
                        t["bets"].pop(tk, None)
                        t["closed"].append({"ticker": tk, "outcome": "exit",
                                            "pnl": None, "in_play": True})
                        t["n_closed"] += 1
                    else:
                        t["bets"][tk]["flags"] = {k: v for k, v in carrier.items()
                                                  if k != "entry"}
            if changed:
                t["account"] = acc.to_dict()
                t["fitness"] = a3._fitness(t)
    return exited


# ── entry: real armed agents ───────────────────────────────────────────────────

def _enter_real(client, sb, state, all_games, brains, real_open, daily_spent,
                daily_cap, today, ts):
    """Real-money entries for armed slots. Same logic as promoter.run entry section
    but consumes the shared pricing pass. Returns (rows, daily_spent)."""
    if not all_games:
        return [], daily_spent
    master = sb["master"]
    per_game_cap = master.get("per_game_cap_dollars", 0.0)
    s2c = promo._series_to_cat()
    ovr = bool(master.get("override_forward_gate", False))
    rows = []

    # Select armed agents per category
    from wc.core.promote_base import _series_to_cat as _s2c_pb
    s2c_pb = _s2c_pb()
    cats = [c for c in promo.ARMED_CATS
            if sb["slots"].get(c, {}).get("mode", "off") != "off"]
    members_by_cat, params_by_cat = {}, {}
    for c in cats:
        n_ens = max(1, int(sb["slots"][c].get("ensemble", 1)))
        # unified-pool selection: top-N pregame + top-N inplay
        sel = promo.select(None, n_ens, allow_insample=ovr, window="pregame")
        seen = {x["lineage"] for x in sel}
        sel += [x for x in promo.select(None, n_ens, allow_insample=ovr, window="inplay")
                if x["lineage"] not in seen]
        for x in sel:
            x["params"] = dict(x["params"], pregame=1, inplay=1)
        if sel:
            members_by_cat[c] = sel
            params_by_cat[c] = sel[0]["params"]
        print(f"  {c}: " + (", ".join(
            f"{x['lineage']}({'ip' if x['window'] == 'inplay' else 'pre'} "
            f"${x['pre_pnl']:+.0f}/{x['pre_n']})" for x in sel)
            if sel else "no eligible team"))

    if not members_by_cat:
        return rows, daily_spent

    event_exp = promo._event_open_exposure(real_open) if real_open else {}
    depth_left = {}
    for c, members in members_by_cat.items():
        slot = sb["slots"][c]
        games = [dict(g, legs=list(g["legs"])) for g in all_games]
        only = set(slot.get("only_event_dates") or [])
        if only:
            games = [g for g in games if g.get("event", "")[:7] in only]
        games.sort(key=lambda g: not any(l.get("in_play") for l in g["legs"]))
        per_capital = slot.get("member_capital") or (slot["capital"] / max(1, len(members)))
        max_bet, max_open = slot.get("max_bet", 8.0), slot.get("max_open", 6)
        fuse = slot.get("fuse_per_cycle", 20.0)
        state.setdefault("ensemble", {})[c] = [m["lineage"] for m in members]
        for member in members:
            strat = s3.Strategist(member["params"], member.get("focus"))
            mkey = f"{c}:{member['lineage']}"
            mirror = (PaperAccount.from_dict(state["mirror"][mkey])
                      if state["mirror"].get(mkey)
                      else PaperAccount(mkey, per_capital))
            member_cap = per_capital
            member_exp = (promo._member_open_exposure(c, member["lineage"], real_open)
                          if real_open else 0.0)
            opposing = ({tk for tk, pos in real_open.items() if pos < 0}
                        if real_open else set())
            cat_open = (sum(1 for tk in real_open
                           if real_open[tk] > 0 and s2c.get(tk.split("-")[0]) == c)
                        if real_open else len(mirror.positions))
            cycle_spent, ev_seen = 0.0, set()
            for g in games:
                for lg in g["legs"]:
                    tk, ask = lg["ticker"], lg["ask"]
                    if not ask or tk in opposing or cat_open >= max_open:
                        continue
                    if lg.get("_period", "full") not in a3.BET_PERIODS:
                        continue
                    if g["event"] in ev_seen:
                        continue
                    ip = lg.get("in_play", False)
                    if ip:
                        nc = a3.NC_HI.get(c, a3.NC_DEFAULT)
                        if ask / 100.0 >= nc or lg["p_fair"] >= nc:
                            continue
                    depth = depth_left.get(tk, int(lg.get("ask_size", 0)))
                    if depth < 1:
                        continue
                    tau = _event_tau_days(g["event"])
                    if tau > 2.0:
                        continue
                    ctx = {"p_fair": lg["p_fair"], "ask": ask, "in_play": ip,
                           "minute": lg.get("minute"), "type": lg.get("_type"),
                           "sigma": 0.12}
                    bet = min(strat.entry_size(ctx, per_capital, tau_days=tau), max_bet)
                    if bet <= 0:
                        continue
                    n = min(kelly.to_contracts(bet, ask / 100.0), depth)
                    if n < 1:
                        continue
                    cost = n * ask / 100.0
                    if member_exp + cost > member_cap:
                        continue
                    if cycle_spent + cost > fuse:
                        continue
                    if real_open and daily_cap and (daily_spent + cost) > daily_cap:
                        rows.append({"ts": ts, "cat": c, "act": "DAILY_CAP_HIT"})
                        continue
                    if (real_open and per_game_cap
                            and event_exp.get(g["event"], 0.0) + cost > per_game_cap):
                        continue
                    if real_open:
                        ok, _, _ = client.buy(tk, n, ask, dry_run=False)
                        if ok:
                            daily_spent += cost
                            real_open[tk] = real_open.get(tk, 0) + n
                    else:
                        ok = mirror.buy(tk, n, ask / 100.0,
                                        {"sub": lg["sub"], "event": g["event"]})
                    if ok:
                        ev_seen.add(g["event"])
                        depth_left[tk] = depth - n
                        cycle_spent += cost; cat_open += 1; member_exp += cost
                        event_exp[g["event"]] = event_exp.get(g["event"], 0.0) + cost
                        rows.append({"ts": ts, "mode": "REAL-ORDERS" if real_open else "DRY-RUN MIRROR",
                                     "cat": c, "team": member["lineage"], "act": "BUY",
                                     "ticker": tk, "sub": lg["sub"], "n": n, "in_play": ip,
                                     "ask": ask, "edge": round(lg["p_fair"] - ask / 100.0, 3),
                                     "cost": round(cost, 2)})
                        if real_open:
                            pdb.log_prediction({
                                "ticker": tk, "series": tk.split("-")[0],
                                "event": g["event"], "sub": lg["sub"],
                                "type": lg.get("_type"), "period": lg.get("_period"),
                                "in_play": ip, "minute": lg.get("minute"),
                                "p_fair": lg["p_fair"], "sources": lg.get("sources") or {},
                                "source": lg.get("source"), "ask": ask, "bid": lg.get("bid"),
                                "edge": round(lg["p_fair"] - ask / 100.0, 3),
                                "entered": True, "n": n, "team": member["lineage"], "cat": c,
                            }, ts=ts)
            state["mirror"][mkey] = mirror.to_dict()
            print(f"  {c}/{member['lineage']}: spent ${cycle_spent:.2f}/{fuse}"
                  f" | exposure ${member_exp:.2f}/{member_cap:.0f}")
    return rows, daily_spent


# ── main loop ──────────────────────────────────────────────────────────────────

def run(execute=False):
    """One unified tick: settle → price(once) → enter(paper+real) → exit(paper+real)
    → evolve(paper) → train(real). Paper always runs; real only if armed+execute+!kill."""
    if A.kalshi_maintenance():
        print("kalshi maintenance window (3-5am ET) — skipping cycle")
        return

    # ── load state ──────────────────────────────────────────────────────────
    sb = json.load(open(SWITCHBOARD))
    master = sb["master"]
    executing = bool(execute)
    real_entries = bool(master.get("armed") and execute and not master.get("kill"))
    mode_str = "REAL-ORDERS" if real_entries else "DRY-RUN MIRROR"
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        from zoneinfo import ZoneInfo
        today = dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        today = ts[:10]

    meta = A._load(os.path.join(STATE_V3, "meta.json"),
                   {"cycle": 0, "next_id": 0, "generation": {},
                    "resolved_since_evolve": {}})
    next_id = [meta["next_id"]]
    cycle = meta["cycle"] + 1
    rng = random.Random(90000 + cycle)

    # ── load paper brains + teams ───────────────────────────────────────────
    brains, teams_by_cat = {}, {}
    for c in SEED_CATS:
        bw = A._load(os.path.join(STATE_V3, f"brain_{c}.json"), None)
        bconf = {"use_llm": c in LLM_CATS}
        if bw:
            bconf["stacker_weights"] = bw
        bconf["llm_cache"] = A._load(os.path.join(STATE_V3, f"llm_cache_{c}.json"), {})
        brains[c] = b2.BrainV2(bconf)
        t = A._load(os.path.join(STATE_V3, f"teams_{c}.json"), None)
        teams_by_cat[c] = t if t else a3.seed_population(c, next_id, rng)

    client = KalshiClientV2(req_per_sec=6)

    # ── load promoter state ────────────────────────────────────────────────
    pstate = _load(PROMOTE_STATE, {"mirror": {}, "daily": {}, "flags": {},
                                   "pregame_scanned": {}})
    daily_spent = pstate["daily"].get(today, 0.0)
    daily_cap = master.get("daily_cap_dollars", 0.0)

    # ── real position read (once) ──────────────────────────────────────────
    real_open = {}
    if executing:
        try:
            for p in client.get_positions():
                real_open[p.get("ticker")] = float(p.get("position_fp", 0) or 0)
        except Exception as e:
            print(f"  [ABORT] real position read failed ({e}) — no exits/entries this cycle")
            return

    # ── 1. SETTLE (paper + prediction DB, one pass) ─────────────────────────
    n_settled, ev = a3._settle_live(client, teams_by_cat, brains, cycle)
    try:
        pdb.grade(client)
    except Exception as e:
        print(f"  [WARN] grade predictions: {e}")

    # ── train real brains from resolved outcomes BEFORE pricing ────────────
    if executing:
        try:
            promo._train_from_real(brains, client)
        except Exception as e:
            print(f"  [WARN] train from real: {e}")

    # ── 2. PRICE (one pass, all consumers) ──────────────────────────────────
    print(f"[cycle v3] {mode_str}  cycle={cycle}  ({ts}; LA-day {today})")
    all_games = _price_surface(client, brains, pstate, today, use_llm=executing)

    # ── record predictions for every priced leg ────────────────────────────
    for g in all_games:
        for lg in g["legs"]:
            try:
                pdb.log_prediction({
                    "ticker": lg["ticker"], "series": lg["ticker"].split("-")[0],
                    "event": g.get("event"), "sub": lg.get("sub"),
                    "type": lg.get("_type") or lg.get("type"),
                    "period": lg.get("_period") or lg.get("period"),
                    "in_play": lg.get("in_play"), "minute": lg.get("minute"),
                    "p_fair": lg.get("p_fair"), "source": lg.get("source"),
                    "sources": lg.get("sources"), "ask": lg.get("ask"),
                    "bid": lg.get("bid"),
                    "edge": round((lg.get("p_fair") or 0) - (lg.get("ask") or 0) / 100.0, 4),
                    "cat": "all", "close_time": lg.get("close_time")})
            except Exception:
                pass

    # ── 3. ENTER (paper first, then real) ───────────────────────────────────
    paper_placed = _enter_paper(teams_by_cat, all_games)

    promoter_rows = []
    # EXITS first on the real book (never abandon open positions)
    real_entries_ok = True
    if executing:
        if promo.manage_exits(client, sb, pstate, True,
                              {c: teams_by_cat.get("all", [{}])[0].get("params", {})
                               for c in promo.ARMED_CATS},
                              brains, promoter_rows, ts) is False:
            _log(promoter_rows); return
        if master.get("kill"):
            _save(PROMOTE_STATE, pstate); _log(promoter_rows)
            print(f"  KILL active — flattened real book, no entries")
            real_entries_ok = False

    if real_entries and real_entries_ok:
        promoter_rows, daily_spent = _enter_real(
            client, sb, pstate, all_games, brains, real_open,
            daily_spent, daily_cap, today, ts)

    # ── 4. EXIT (paper) ────────────────────────────────────────────────────
    paper_exited = _exit_paper(teams_by_cat, all_games)

    # ── 5. EVOLVE (paper only) ─────────────────────────────────────────────
    rse = meta.get("resolved_since_evolve", {})
    gens = meta.get("generation", {})
    evo_rows = []
    for c in SEED_CATS:
        rse[c] = rse.get(c, 0) + ev.get(c, 0)
        if rse[c] >= a3.EVOLVE_EVERY:
            gens[c] = gens.get(c, 0) + 1
            a3.evolve_v3(teams_by_cat, c, next_id, gens[c], rng, evo_rows)
            rse[c] = 0

    # ── 6. PERSIST ─────────────────────────────────────────────────────────
    for c in SEED_CATS:
        A._save(os.path.join(STATE_V3, f"teams_{c}.json"), teams_by_cat[c])
        A._save(os.path.join(STATE_V3, f"brain_{c}.json"), brains[c].weights)
        A._save(os.path.join(STATE_V3, f"llm_cache_{c}.json"), brains[c].llm_cache)
    meta.update({"cycle": cycle, "next_id": next_id[0], "generation": gens,
                 "resolved_since_evolve": rse})
    A._save(os.path.join(STATE_V3, "meta.json"), meta)
    if evo_rows:
        os.makedirs(os.path.dirname(EVO_LOG_V3), exist_ok=True)
        with open(EVO_LOG_V3, "a") as f:
            for r in evo_rows:
                f.write(json.dumps(r) + "\n")

    pstate["daily"][today] = daily_spent
    _save(PROMOTE_STATE, pstate)
    _log(promoter_rows)

    # ── 7. TALLY ───────────────────────────────────────────────────────────
    tally = {"cycle": cycle, "settled": n_settled, "paper_placed": paper_placed,
             "paper_exited": paper_exited,
             "real_actions": len(promoter_rows),
             "real_spend": round(daily_spent, 2)}
    for c in SEED_CATS:
        bp = a3.pregame_board(teams_by_cat[c])
        best = bp[0] if bp else None
        tally[c] = ({"best_pregame": best[0]["lineage"], "pre_pnl": round(best[1], 2),
                     "pre_n": best[2]} if best else {"best_pregame": None})
    os.makedirs(os.path.dirname(TALLY_V3), exist_ok=True)
    with open(TALLY_V3, "a") as f:
        f.write(json.dumps(tally) + "\n")
    print(json.dumps(tally, indent=2))
    return tally


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Place REAL orders IFF switchboard armed + !kill")
    ap.add_argument("--select", action="store_true",
                    help="Print the promoter selector table and exit")
    args = ap.parse_args()
    if args.select:
        for c in promo.ARMED_CATS:
            print(f"--- {c} ---")
            for m in promo.select(c, 8):
                print(f"  {m['lineage']:<16} pre_fit={m['pre_fit']:+.3f} "
                      f"pre_pnl=${m['pre_pnl']:+.2f} ({m['pre_n']} bets) "
                      f"focus={m['focus'] or 'all'}")
    else:
        run(execute=args.execute)
