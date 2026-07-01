#!/usr/bin/env python3
"""
rebuild_aggressive.py — one-shot: wipe the arena and replay every resolved WC game
ONE AT A TIME under the AGGRESSIVE params, mimicking live cadence.

- Seeds all 16 teams with the aggressive risk settings (min_edge 0.005, kelly x1.4,
  entry_prob=mean, min_entry_price 0.05, loosened scalp gates) — NO LLM seed, so the
  aggression is actually used (a plain `arena.py --reset` would re-seed via LLM and
  overwrite it).
- Replays games chronologically with a pause between each (mimic live spacing) and an
  automatic 429 backoff (Kalshi rate-limit → sleep and retry, never a silent skip).
- Resumable: re-run with FRESH=0 to continue from where it stopped (uses played.json).

Run on the droplet, cron PAUSED:
    FRESH=1 SLEEP=30 nohup ./venv/bin/python3 rebuild_aggressive.py > logs/rebuild.out 2>&1 &
"""
import os
import sys
import json
import time

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

import arena as A                                   # noqa: E402
from group.paper import PaperAccount                # noqa: E402

SLEEP = float(os.environ.get("SLEEP", "30"))        # pause between games (mimic live)
FRESH = os.environ.get("FRESH", "1") == "1"


def _aggressive(par):
    par = dict(par)
    par["min_edge"] = 0.005
    par["kelly_fraction"] = round(min(0.60, par.get("kelly_fraction", 0.25) * 1.4), 4)
    par["entry_prob"] = "mean"
    par["min_entry_price"] = 0.05
    if "scalp_min_prob" in par:
        par["scalp_min_prob"] = 0.70
    if "scalp_min_minute" in par:
        par["scalp_min_minute"] = 60
    return par


def aggressive_reset(tcfg, acfg):
    os.makedirs(A.ARENA, exist_ok=True)
    os.makedirs(os.path.dirname(A.TALLY), exist_ok=True)
    for f in os.listdir(A.ARENA):
        os.remove(os.path.join(A.ARENA, f))
    open(A.TALLY, "w").close()
    kp = A._kind_params(tcfg)
    for cat, cc in acfg["categories"].items():
        dw = ({"w_llm": 0.34, "w_model": 0.33, "w_data": 0.33} if cc.get("llm")
              else {"w_llm": 0.0, "w_model": 0.5, "w_data": 0.5})
        A._save(A._brain_path(cat), {"brain_weights": dw, "llm_addendum": "",
                                     "model_calibration": None})
        for s in acfg["strategies"]:
            name = f"{cat}_{s['suffix']}"
            A._save(A._team_path(name), {
                "cat": cat, "kind": s["kind"], "params": _aggressive(kp[s["kind"]]),
                "account": PaperAccount(name, acfg.get("starting_cash", 200.0)).to_dict(),
                "ledger": {"bets": [], "skipped": []}})
    A._save(A.PLAYED, [])
    print("=== aggressive reset: 16 teams seeded, board wiped ===", flush=True)


def main():
    A.load_keys()
    base = json.load(open(A.GCONFIG))
    tcfg = json.load(open(A.TCONFIG))
    acfg = json.load(open(A.ACONFIG))
    client = A.KalshiClient()

    # 429 backoff on ALL Kalshi GETs (candlesticks, price paths, orderbooks).
    _orig = client.session.get
    def _get(*a, **k):
        for i in range(10):
            r = _orig(*a, **k)
            if getattr(r, "status_code", 200) == 429:
                w = min(120, 5 * (2 ** i))
                print(f"  [KALSHI 429] backoff {w}s (attempt {i+1})", flush=True)
                time.sleep(w)
                continue
            return r
        return r
    client.session.get = _get

    if FRESH:
        aggressive_reset(tcfg, acfg)

    brains, teams = A.load_arena(base, acfg)
    played = set(A._load(A.PLAYED, []))
    events = A.resolved_events(client, acfg)
    todo = [ev for ev in events if ev["prefix"] not in played]
    print(f"=== rebuild: {len(todo)} games to replay (of {len(events)} resolved) "
          f"| sleep {SLEEP}s/game ===", flush=True)

    for i, ev in enumerate(todo, 1):
        t0 = time.time()
        print(f"--- [{i}/{len(todo)}] {ev['prefix']} ({ev['home']} vs {ev['away']}) ---", flush=True)
        gres = A.play_event(client, ev, acfg, brains, teams)
        A.learn_and_tally(brains, teams, ev["prefix"], gres, played, "(rebuild-agg)", acfg)
        print(f"    done in {time.time()-t0:.0f}s", flush=True)
        if i < len(todo):
            time.sleep(SLEEP)

    A.print_tally(teams)
    print("=== REBUILD COMPLETE ===", flush=True)


if __name__ == "__main__":
    main()
