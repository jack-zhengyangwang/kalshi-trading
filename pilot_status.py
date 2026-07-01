#!/usr/bin/env python3
"""Live snapshot of the armed v3 real-money pilot. Run anytime:
    ./venv/bin/python3 pilot_status.py
Read-only: reads the switchboard, the real-order log, and settles each bet against
its Kalshi result. Shows armed state, real orders (pre vs in-play, by category/agent),
daily spend vs cap, and realized P&L so far."""
import collections
import datetime as dt
import json
import os

import arena_v2 as A

BASE = os.path.dirname(os.path.abspath(__file__))


def _load(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return d


def main():
    sb = _load(os.path.join(BASE, "switchboard_v3.json"), {})
    m = sb.get("master", {})

    rows = []
    pl = os.path.join(BASE, "logs", "promote_v3.jsonl")
    if os.path.exists(pl):
        rows = [json.loads(l) for l in open(pl)]
    buys = [r for r in rows if r.get("mode") == "REAL-ORDERS" and r.get("act") == "BUY"]

    # settle each bet against its Kalshi result for realized P&L
    c = A.KalshiClientV2(req_per_sec=6)
    realized = 0.0
    settled = openn = 0
    agent = collections.defaultdict(lambda: [0.0, 0])     # team -> [cum pnl, settled_n]
    agent_open = collections.defaultdict(int)             # team -> live (open) bets
    for b in buys:
        tk, n, cost = b["ticker"], b["n"], b.get("cost", 0)
        team = b.get("team") or "?"
        try:
            mk = c.get_market(tk)
            r = (mk.get("result") or "").lower()
            st = mk.get("status")
        except Exception:
            r, st = "", "?"
        if st in ("settled", "finalized") and r in ("yes", "no"):
            pnl = (n * 1.0 if r == "yes" else 0.0) - cost
            realized += pnl
            settled += 1
            agent[team][0] += pnl
            agent[team][1] += 1
        else:
            openn += 1
            agent_open[team] += 1

    try:
        from zoneinfo import ZoneInfo
        today = dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        today = ""
    state = _load(os.path.join(BASE, "arena_v3_state", "promote_state_v3.json"), {})
    flag = "🛑KILL" if m.get("kill") else ("armed ✅" if m.get("armed") else "off ⏸️")

    print("═══ PILOT ═══")
    print("P&L  $%+.2f" % realized)
    print("settled %d · open %d" % (settled, openn))
    print("spend $%.0f/$%s  %s"
          % (state.get("daily", {}).get(today, 0.0), m.get("daily_cap_dollars"), flag))
    print("")
    print("%-10s %7s %4s" % ("Team", "PnL", "Live"))
    teams = set(agent) | set(agent_open)
    for a in sorted(teams, key=lambda x: -agent[x][0]):
        print("%-10s %+7.2f %4d" % (a[:10], agent[a][0], agent_open.get(a, 0)))
    if not teams:
        print("(no real orders yet)")


if __name__ == "__main__":
    main()
