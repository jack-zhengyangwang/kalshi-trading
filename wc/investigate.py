#!/usr/bin/env python3
"""Detail view of ONE agent's bets:  ./venv/bin/python3 investigate.py <agent>
Primary: the agent's REAL-money pilot orders (leg, size, price, edge, in-play flag,
settled result/P&L). Falls back to the paper arena if the name is a paper-only team.
Read-only — settles each real bet against its Kalshi result. Name match is a
case-insensitive substring, so `/investigate flow` or `/investigate g1` both work."""
import json
import os
from wc import paths
import sys

import wc.core.arena_base as A

BASE = os.path.dirname(os.path.abspath(__file__))
SEED_CATS = ["winner", "game_lines", "game_props"]


def _short(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _leg(tk):
    """Readable leg from a ticker: SERIES-EVENT-OUTCOME -> 'TOTAL:OVER25'."""
    p = (tk or "").split("-")
    return _short("%s:%s" % (p[0].replace("KXWC", ""), p[-1] if len(p) > 2 else "?"), 18)


def investigate_real(who):
    pl = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
    rows = [json.loads(l) for l in open(pl)] if os.path.exists(pl) else []
    buys = [r for r in rows if r.get("mode") == "REAL-ORDERS" and r.get("act") == "BUY"
            and who in (r.get("team") or "").lower()]
    if not buys:
        names = sorted({r.get("team") for r in rows if r.get("act") == "BUY" and r.get("team")})
        return None, names
    team = buys[0]["team"]
    c = A.KalshiClientV2(req_per_sec=6)
    realized = 0.0
    openb, donb = [], []
    for b in buys:
        try:
            mk = c.get_market(b["ticker"])
            r = (mk.get("result") or "").lower(); st = mk.get("status")
        except Exception:
            r, st = "", "?"
        if st in ("settled", "finalized") and r in ("yes", "no"):
            pnl = (b["n"] * 1.0 if r == "yes" else 0.0) - b.get("cost", 0)
            realized += pnl; donb.append((b, pnl))
        else:
            openb.append(b)
    out = ["═ %s ═" % _short(team, 16),
           "REAL  P&L $%+.2f" % realized,
           "open %d · done %d" % (len(openb), len(donb))]
    if openb:
        out.append("\n▸ open")
        for b in openb:
            tag = " LIVE" if b.get("in_play") else ""
            out.append("%-18s %3d¢" % (_short(b.get("sub") or _leg(b["ticker"]), 18),
                                       int(b.get("ask", 0) or 0)))
            out.append("  %dc $%.2f ed%+.2f%s"
                       % (b["n"], b.get("cost", 0), b.get("edge", 0) or 0, tag))
    if donb:
        out.append("\n▸ settled")
        for b, pnl in donb:
            mark = "✅" if pnl >= 0 else "❌"
            out.append("%-15s %s%+6.2f"
                       % (_short(b.get("sub") or _leg(b["ticker"]), 15), mark, pnl))
    return "\n".join(out), None


def investigate_arena(who):
    hits = []
    for cat in SEED_CATS:
        teams = A._load(os.path.join(paths.STATE_V3, "teams_%s.json" % cat), [])
        for t in teams:
            if who in (t.get("lineage") or "").lower():
                hits.append((cat, t))
    if not hits:
        return None
    out = []
    for cat, t in hits[:3]:
        closed = [cb for cb in t.get("closed", []) if cb.get("pnl") is not None]
        opened = t.get("bets", {})
        pnl = sum(cb["pnl"] for cb in closed)
        out += ["═ %s (%s) ═" % (_short(t["lineage"], 14), cat),
                "PAPER P&L $%+.2f" % pnl,
                "open %d · done %d" % (len(opened), len(closed))]
        if opened:
            out.append("▸ open")
            for tk, bt in list(opened.items())[:6]:
                tag = " LIVE" if bt.get("in_play") else ""
                out.append("%-18s %3d¢%s" % (_leg(tk), int(bt.get("ask", 0) or 0), tag))
        if closed:
            out.append("▸ recent")
            for cb in closed[-6:]:
                mark = "✅" if cb["pnl"] >= 0 else "❌"
                out.append("%-15s %s%+6.2f" % (_leg(cb["ticker"]), mark, cb["pnl"]))
        out.append("")
    return "\n".join(out)


def main():
    who = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if not who:
        print("usage: investigate.py <agent>"); return
    txt, names = investigate_real(who)
    if txt:
        print(txt); return
    ar = investigate_arena(who)
    if ar:
        print(ar); return
    print("no bets found for '%s'" % who)
    if names:
        print("known (real): " + ", ".join(n for n in names if n))


if __name__ == "__main__":
    main()
