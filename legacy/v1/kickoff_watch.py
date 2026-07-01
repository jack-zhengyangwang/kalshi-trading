#!/usr/bin/env python3
"""
kickoff_watch.py — ping when USA or Canada is about to play (or is live), and
surface every decision the model has logged.

Two modes:
  (default)   Check ESPN. If a USA/Canada WC game kicks off within the next
              ~60 min or is currently live, send a macOS notification + print a
              decision summary. Deduped so it pings once per game. Driven by the
              launchd agent tech.ebk.kickoff-ping (every 30 min).
  --watch     Print ALL of today's model decisions (entries + exits) in full,
              for you to read live. Run this by hand during a game.
"""
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "logs")
STATE = os.path.join(LOG_DIR, ".kickoff_notified.json")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
TEAMS = ("United States", "USA", "Canada")


def _utcnow():
    return datetime.now(timezone.utc)


def _todays_decisions():
    """Return (entries, exits) lists from today's logs."""
    def _read_jsonl(path):
        rows = []
        if os.path.exists(path):
            for l in open(path):
                l = l.strip()
                if not l:
                    continue
                try:                       # tolerate a partially-written line
                    rows.append(json.loads(l))
                except json.JSONDecodeError:
                    continue
        return rows

    day = _utcnow().strftime("%Y%m%d")
    entries = _read_jsonl(os.path.join(LOG_DIR, f"group_{day}.jsonl"))
    exits = _read_jsonl(os.path.join(LOG_DIR, "exits.jsonl"))
    return entries, exits


def _summary_line():
    entries, exits = _todays_decisions()
    from collections import Counter
    acts = Counter(d.get("action") for d in entries)
    bets = [d for d in entries if d.get("action") == "BET"]
    staked = sum(d.get("bet_dollars", 0) for d in bets)
    return (f"{len(entries)} decisions today "
            f"(BET {acts.get('BET',0)}, PASS {acts.get('PASS',0)}, "
            f"capped {acts.get('PASS_CAP',0)}), ${staked:.0f} staked, "
            f"{len(exits)} exits.")


def _notify(title, message):
    print(f"\n*** PING [{title}] {message} ***\n", flush=True)
    if sys.platform == "darwin":
        try:
            subprocess.run(["osascript", "-e",
                f'display notification "{message}" with title "EBK ⚽ {title}" sound name "Glass"'],
                timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def watch():
    entries, exits = _todays_decisions()
    print(f"=== Model decisions — {_utcnow():%Y-%m-%d %H:%M UTC} ===")
    if not entries:
        print("  (no entry decisions logged today — loop idle or no liquidity yet)")
    for d in entries:
        print(f"  [{d.get('action'):9}] {d.get('ticker'):<32} "
              f"p_mkt={d.get('p_market')} p_fair={d.get('p_fair')} "
              f"edge={d.get('edge'):+.3f} bet=${d.get('bet_dollars')}")
        if d.get("reasoning"):
            print(f"             llm: {d['reasoning']}")
        if d.get("cap_reason"):
            print(f"             capped: {d['cap_reason']}")
    if exits:
        print("\n  --- Keeper exits ---")
        for e in exits:
            print(f"  {e.get('ticker'):<32} {e.get('action'):10} @ bid={e.get('bid')} "
                  f"fair={e.get('live_fair')} {e.get('score')} {e.get('minute')}'")
    print("\nLive tails:  tail -f logs/loop.out.log   |   tail -f logs/keeper_*.log")


def check_and_notify():
    try:
        import requests
        events = requests.get(ESPN, timeout=10).json().get("events", [])
    except Exception as e:
        print(f"[kickoff_watch] ESPN fetch failed: {e}", flush=True)
        return

    try:
        state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    except Exception:
        state = {}

    now = _utcnow()
    for ev in events:
        name = ev.get("name", "")
        if not any(t in name for t in TEAMS):
            continue
        st = ev.get("status", {}).get("type", {}).get("state", "")
        try:
            ko = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        mins = (ko - now).total_seconds() / 60.0
        relevant = st == "in" or (-5 <= mins <= 60)
        if not relevant:
            continue
        key = f"{ev.get('id')}::{ev.get('date')}"
        if state.get(key):
            continue  # already pinged this game

        when = "LIVE now" if st == "in" else f"kicks off in ~{int(mins)} min"
        _notify(name, f"{when}. {_summary_line()} Run: python3 kickoff_watch.py --watch")
        state[key] = now.isoformat()

    os.makedirs(LOG_DIR, exist_ok=True)
    json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch()
    else:
        check_and_notify()
