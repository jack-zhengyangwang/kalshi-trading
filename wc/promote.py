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
from wc.lib.exit_rules import disarm            # #7: consume one-shot exit triggers
import wc.prediction_db as pdb                  # #8/#9: record live predictions + close the loop

_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _event_tau_days(event, now=None):
    """Days until a game resolves, from the DATE in its event code (e.g.
    '26JUL01USABIH' -> 2026-07-01, end of day). Kalshi's close_time is a far-out
    admin date, so we use the game date. Unparseable -> 999 (treated as far-out)."""
    try:
        y = 2000 + int(event[0:2]); mon = _MON[event[2:5]]; d = int(event[5:7])
        game = dt.datetime(y, mon, d, 23, 59, tzinfo=dt.timezone.utc)
        now = now or dt.datetime.now(dt.timezone.utc)
        return (game - now).total_seconds() / 86400.0
    except Exception:
        return 999.0

CAT = "game_lines"
SWITCHBOARD = paths.SWITCHBOARD_V3
STATE = os.path.join(paths.STATE_V3, "promote_state_v3.json")
PLOG = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
MIN_PREGAME_N = 10        # min resolved FORWARD pre-game bets before a team is promotable
MIN_INSAMPLE_N = 5        # relaxed floor when master.override_forward_gate=true (in-sample test)


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
    """Tuned stacker weights + LIVE LLM pricing. The LLM is stacked as an extra source
    on top of the validated structural model (it augments, doesn't replace it), priced
    in real time per RECAP ('LLM belongs live'). Model: claude-haiku-4-5 (cheap/fast for
    frequent cycles; override via config 'claude_model'). Per-category."""
    bw = _load(os.path.join(a3.STATE_V3, f"brain_{cat}.json"), None)
    bconf = {"use_llm": True}
    if bw:
        bconf["stacker_weights"] = bw
    return BrainV2(bconf)


def select(cat, n, allow_insample=False, window="pregame"):
    """Top-n teams by `window` (pregame|inplay) fitness *in `cat`* from the UNIFIED pool
    (teams_all.json —
    one population that bets every category), scored ONLY on bets whose series maps to
    `cat` so per-category fitness stays honest.

    Default (allow_insample=False): FORWARD / out-of-sample only — excludes the in-sample
    replay-seed games and needs >=MIN_PREGAME_N forward pre-game bets. Arms ONLY on edge
    the population did NOT train on. Returns [] (refuse to arm) otherwise.

    OVERRIDE (allow_insample=True, driven by switchboard master.override_forward_gate):
    relaxes the gate — counts ALL pre-game bets incl. the in-sample seed and uses the
    lower MIN_INSAMPLE_N floor. Still requires positive P&L (never arms a losing agent).
    This bets UNVALIDATED (overfit-risk) in-sample edge — use ONLY for a deliberate live
    test with tiny caps + kill ready."""
    seed = set(_load(os.path.join(a3.STATE_V3, "seed_events.json"), []))
    teams = _load(os.path.join(a3.STATE_V3, "teams_all.json"), [])
    s2c = _series_to_cat()
    min_n = MIN_INSAMPLE_N if allow_insample else MIN_PREGAME_N
    want_ip = (window == "inplay")           # select on the in-play record instead
    board = []
    for t in teams:
        rows = [cb for cb in t["closed"]
                if cb.get("pnl") is not None and bool(cb.get("in_play")) == want_ip
                and "-" in cb["ticker"]
                and (allow_insample or cb["ticker"].split("-")[1] not in seed)
                and (cat is None or s2c.get(cb["ticker"].split("-")[0]) == cat)]
        pn = len(rows)
        if pn < min_n:
            continue
        pp = sum(cb["pnl"] for cb in rows)
        if pp <= 0:
            continue
        staked = sum((cb.get("staked") or 0) for cb in rows)
        pf = pp / math.sqrt(staked + 1.0)
        board.append({"lineage": t["lineage"], "params": t["params"],
                      "focus": t.get("market_focus"), "pre_pnl": round(pp, 2),
                      "pre_n": pn, "pre_fit": round(pf, 3), "window": window})
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
        disarm(action, flags)     # #7: consume OVERPRICED/TAKE_PROFIT one-shot after a confirmed sell
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


def _event_open_exposure(real_open):
    """$ cost-basis tied up PER GAME (event code) in still-open real positions, across
    ALL agents — read from the real-order log. Enforces the per-game spend cap."""
    path = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        tk = r.get("ticker") or ""
        if (r.get("mode") == "REAL-ORDERS" and r.get("act") == "BUY"
                and "-" in tk and real_open.get(tk, 0) > 0):
            ev = tk.split("-")[1]
            out[ev] = out.get(ev, 0.0) + (r.get("cost", 0) or 0.0)
    return out


def _train_from_real(brains, client):
    """#9: close the loop — grade REAL predictions and re-tune each category's brain
    stacker weights from realized outcomes, so forward games keep updating the model.
    Idempotent grading; thin-sample-guarded (update_stacker no-ops below 15 outcomes)."""
    try:
        pdb.grade(client)                       # settle newly-resolved real predictions
    except Exception as e:
        print(f"  [train] grade skipped: {e}")
        return
    src_by_tk, cat_by_tk = {}, {}               # join graded outcome -> prediction's source blend
    for p in pdb._read(pdb.PRED_LOG):
        tk = p.get("ticker")
        if tk and p.get("sources"):
            src_by_tk[tk] = p["sources"]; cat_by_tk[tk] = p.get("cat")
    by_cat = {}
    for g in pdb._read(pdb.GRADED_LOG):
        tk = g.get("ticker"); s = src_by_tk.get(tk); cat = cat_by_tk.get(tk)
        if s and cat in brains and g.get("outcome") is not None:
            by_cat.setdefault(cat, []).append({"sources": s, "outcome": g["outcome"]})
    for cat, resolved in by_cat.items():
        old = dict(brains[cat].weights)
        neww = brains[cat].update_stacker(resolved)   # in-place; no-op < min_sample
        if neww != old:
            _save(os.path.join(a3.STATE_V3, f"brain_{cat}.json"), neww)
            print(f"  [train] {cat}: stacker re-tuned from {len(resolved)} real outcomes -> {neww}")


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

    # armed categories (slots that are on) → ensemble per category (forward-only unless
    # master.override_forward_gate relaxes it to in-sample for a deliberate live test)
    ovr = bool(master.get("override_forward_gate", False))
    cats = [c for c in ARMED_CATS if sb["slots"].get(c, {}).get("mode", "off") != "off"]
    print(f"[live_promote_v3] {mode_str}  ({ts}; LA-day {today})  cats={cats}"
          + ("  [OVERRIDE: in-sample gate]" if ovr else ""))
    members_by_cat, brains, params_by_cat = {}, {}, {}
    for c in cats:
        brains[c] = _brain(c)
        n_ens = max(1, int(sb["slots"][c].get("ensemble", 1)))
        # UNIFIED-POOL SELECTION: pick agents by their WINDOW record ACROSS ALL categories
        # (cat=None), not per-category — else a category the pool never bet pre-game (e.g.
        # winner: 0 pre-game history) gets ZERO pre-game agents and its pre-game edges go
        # unbet. The pre-game agents (aggressive_hold/favorite/...) are armed for EVERY
        # armed category and bet whatever has edge.
        # Ensemble = top-N by PRE-GAME record + top-N by IN-PLAY record (how they're
        # RANKED). This is the "top-3 pregame + top-3 in-play" selection.
        m = select(None, n_ens, allow_insample=ovr, window="pregame")
        seen = {x["lineage"] for x in m}
        m = m + [x for x in select(None, n_ens, allow_insample=ovr, window="inplay")
                 if x["lineage"] not in seen]
        # AGENTS ARE FREE TO BET BOTH WINDOWS + ALL CATEGORIES. Selection ranks them by
        # their proven window, but once armed every agent may bet BOTH pre-game AND in-play,
        # across every scanned market, wherever there's edge — no per-window / per-category
        # restriction (force pregame=1 AND inplay=1 on each, overriding the archetype flag).
        for x in m:
            x["params"] = dict(x["params"], pregame=1, inplay=1)
        if m:
            members_by_cat[c] = m
            params_by_cat[c] = m[0]["params"]        # top member's params drive exits
        print(f"  {c}: " + (", ".join(f"{x['lineage']}({'ip' if x['window']=='inplay' else 'pre'} "
                                      f"${x['pre_pnl']:+.0f}/{x['pre_n']})" for x in m)
                            if m else "no eligible team"))

    client = KalshiClientV2(req_per_sec=4)
    state = _load(STATE, {"mirror": {}, "daily": {}})
    daily_spent = state["daily"].get(today, 0.0)
    daily_cap = master.get("daily_cap_dollars", 0.0)
    per_game_cap = master.get("per_game_cap_dollars", 0.0)     # 0 = disabled

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

    # #9: close the loop — grade real outcomes + re-tune each brain BEFORE this cycle
    # prices, so forward games keep updating the model (in-place, applies this cycle too).
    if executing:
        _train_from_real(brains, client)

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
    event_exp = _event_open_exposure(real_open) if real_entries else {}   # per-game spend so far
    # DUAL-FREQUENCY PER-GAME SCANNER: full Soccer surface, but only for NEAR games.
    # game_series() gives the game-level series (cached) so the fetch is cheap.
    # Selection: within the 48h horizon, LIVE games are scanned EVERY cycle (fast), and
    # each PRE-GAME game is scanned once per day; far/idle games are skipped entirely.
    gseries = scn.game_series(client)
    sb_events = scn.lf.scoreboard_events()
    scanned = state.setdefault("pregame_scanned", {})
    targets = set()
    for m in scn.iter_game_series(client):
        gc = scn.event_code(m["ticker"])
        if not gc or _event_tau_days(gc) > 2.0:      # 48h horizon
            continue
        h, a = scn.home_away(m.get("title"))
        st = scn.lf.state_from_events(sb_events, h, a) if h else None
        if st and st.get("status") == "in":
            targets.add(gc)                          # in-play → scan every cycle
        elif scanned.get(gc) != today:               # pre-game → once per day
            targets.add(gc); scanned[gc] = today
    scan_brain = brains.get("winner") or brains.get("game_lines") or next(iter(brains.values()))
    all_games = (scn.price_games(None, client, scan_brain, max_events=len(targets) + 1,
                                 live=True, series=gseries, only_games=targets)
                 if targets else [])
    _apply_base_rate(sum((g["legs"] for g in all_games), []))
    print(f"  per-game scan: {len(all_games)} game(s) full-surface, targets={sorted(targets)}")
    for c, members in members_by_cat.items():
        slot = sb["slots"][c]
        games = [dict(g, legs=list(g["legs"])) for g in all_games]   # shared per-game full-surface scan
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
                # #6 CONFLICT: only block a YES buy if the account already holds NO on the
                # EXACT same market (Kalshi nets YES+NO → self-cancel). Same-side pile-on by
                # multiple agents on the same ticker is allowed.
                opposing = {tk for tk, pos in real_open.items() if pos < 0}
                cat_open = sum(1 for tk in real_open if real_open[tk] > 0
                               and s2c.get(tk.split("-")[0]) == c)
            else:
                opposing = set()                     # mirror only ever holds YES
                cat_open = len(mirror.positions)
            cycle_spent = 0.0
            ev_seen = set()
            for g in games:
                for lg in g["legs"]:
                    tk, ask = lg["ticker"], lg["ask"]
                    if not ask or tk in opposing or cat_open >= max_open:
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
                    tau = _event_tau_days(g["event"])            # real days to game (event date, not close_time)
                    if tau > 2.0:                                # #2a HARD STOP: no bets resolving >48h out
                        continue
                    ctx = {"p_fair": lg["p_fair"], "ask": ask, "in_play": ip,
                           "minute": lg.get("minute"), "type": lg.get("_type"), "sigma": 0.12}
                    # pass tau so the TVM time-discount also applies within the 48h window
                    bet = min(strat.entry_size(ctx, per_capital, tau_days=tau), max_bet)
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
                    if (real_entries and per_game_cap
                            and event_exp.get(g["event"], 0.0) + cost > per_game_cap):
                        continue                       # per-GAME spend cap (across all agents)
                    if real_entries:
                        ok, _, _ = client.buy(tk, n, ask, dry_run=False)
                        if ok:
                            daily_spent += cost
                            real_open[tk] = real_open.get(tk, 0) + n
                    else:
                        ok = mirror.buy(tk, n, ask / 100.0, {"sub": lg["sub"], "event": g["event"]})
                    if ok:
                        ev_seen.add(g["event"])
                        depth_left[tk] = depth - n          # deplete shared live depth
                        cycle_spent += cost; cat_open += 1; member_exp += cost
                        event_exp[g["event"]] = event_exp.get(g["event"], 0.0) + cost
                        rows.append({"ts": ts, "mode": mode_str, "cat": c, "team": member["lineage"],
                                     "act": "BUY", "ticker": tk, "sub": lg["sub"], "n": n, "in_play": ip,
                                     "ask": ask, "edge": round(lg["p_fair"] - ask / 100.0, 3),
                                     "cost": round(cost, 2)})
                        if real_entries:                    # #8: record the REAL bet + its p_fair sources
                            pdb.log_prediction({
                                "ticker": tk, "series": tk.split("-")[0], "event": g["event"],
                                "sub": lg["sub"], "type": lg.get("_type"), "period": lg.get("_period"),
                                "in_play": ip, "minute": lg.get("minute"), "p_fair": lg["p_fair"],
                                "sources": lg.get("sources") or {}, "source": lg.get("source"),
                                "ask": ask, "bid": lg.get("bid"),
                                "edge": round(lg["p_fair"] - ask / 100.0, 3),
                                "entered": True, "n": n, "team": member["lineage"], "cat": c,
                            }, ts=ts)
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
        from wc import cycle; cycle.run(execute=args.execute)
