#!/usr/bin/env python3
"""Live-pilot SAFETY WATCHDOG (added 2026-07-03; generalized 2026-09-07).

Two hard, unattended guarantees, enforced every cron cycle:

  1. TOTAL-LOSS STOP: if the bot's own real-money P&L (realized settled bets +
     unrealized open bets marked at current bid) drops to <= -$MAX_LOSS, trip
     the kill switch.
  2. DEADLINE STOP: on/after guard.stop_after_utc (if set), trip the kill
     switch unconditionally so nothing keeps betting past a season end.

"Trip" = set master.kill=true, master.armed=false, kill_flattens=true in the
live switchboard. promote.py (cron, --execute) then flattens the real book on
its next cycle INDEPENDENT of armed. Idempotent + self-healing: if anything
flips the flags back, the next guard run re-trips within minutes.

Fail-safe: a Kalshi read error skips ONLY the loss check (never trip on bad
data); the date check is pure and always runs. Placed OUTSIDE wc/ so a `wc/`
rsync deploy cannot clobber it. Read-only w.r.t. everything except the
switchboard master flags."""
import datetime as dt
import json
import os

from wc import paths
import wc.core.arena_base as A

# Limits live in config/switchboard_v3.json under "guard" so they can be tuned
# without editing code (2026-09-07: was hardcoded MAX_LOSS/WC_END_UTC; the WC
# date stop had been unconditionally tripping since 2026-07-20).
#   guard.max_loss_dollars : float  — total-loss floor (default 200.0)
#   guard.stop_after_utc   : str|null — ISO "YYYY-MM-DD" hard deadline; null = no
#                            date stop. Use for a season/tournament end date.
DEFAULT_MAX_LOSS = 200.0
GUARD_LOG = os.path.join(paths.LOGS_DIR, "guard.jsonl")


def _limits():
    """Read (max_loss, stop_after) from the switchboard. Fails safe: on any read
    error, fall back to the default loss floor and no date stop."""
    try:
        sb = json.load(open(paths.SWITCHBOARD_V3))
        g = sb.get("guard", {}) or {}
    except Exception:
        return DEFAULT_MAX_LOSS, None
    max_loss = float(g.get("max_loss_dollars", DEFAULT_MAX_LOSS))
    raw = g.get("stop_after_utc")
    stop_after = None
    if raw:
        try:
            stop_after = dt.datetime.fromisoformat(str(raw).replace("Z", ""))
        except ValueError:
            stop_after = None
    return max_loss, stop_after


def _now_utc():
    return dt.datetime.utcnow()


def _log(rec):
    rec["ts"] = _now_utc().isoformat() + "Z"
    with open(GUARD_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"[guard {rec['ts']}] " + " | ".join(f"{k}={v}" for k, v in rec.items() if k != "ts"))


def total_pnl():
    """Bot-only real P&L: realized (settled) + unrealized (open @ current bid).
    Returns (pnl, ok). ok=False on any Kalshi read failure → caller must NOT
    trip the loss stop this cycle."""
    pl = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
    if not os.path.exists(pl):
        return 0.0, True
    buys = []
    for line in open(pl):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("mode") == "REAL-ORDERS" and r.get("act") == "BUY":
            buys.append(r)
    if not buys:
        return 0.0, True

    c = A.KalshiClientV2(req_per_sec=6)
    pnl = 0.0
    for b in buys:
        tk, n, cost = b.get("ticker"), b.get("n", 0), b.get("cost", 0) or 0.0
        try:
            mk = c.get_market(tk)
        except Exception:
            return 0.0, False                      # bad data → abort loss check
        result = (mk.get("result") or "").lower()
        status = mk.get("status")
        if status in ("settled", "finalized") and result in ("yes", "no"):
            pnl += (n * 1.0 if result == "yes" else 0.0) - cost
        else:
            # unrealized: mark open position at current YES bid (conservative:
            # unreadable bid → mark flat at cost, contributes 0)
            bid_c = None
            try:
                bid_c, _ = c.quote_cents(mk)
            except Exception:
                bid_c = None
            if bid_c:
                pnl += n * (bid_c / 100.0) - cost
    return pnl, True


def trip(reason, detail):
    sb = json.load(open(paths.SWITCHBOARD_V3))
    m = sb.setdefault("master", {})
    already = m.get("kill") and not m.get("armed")
    m["kill"] = True
    m["armed"] = False
    m["kill_flattens"] = True
    tmp = paths.SWITCHBOARD_V3 + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sb, f, indent=2)
    os.replace(tmp, paths.SWITCHBOARD_V3)
    _log({"event": "TRIP", "reason": reason, "detail": detail,
          "was_already_stopped": bool(already)})


def main():
    now = _now_utc()
    max_loss, stop_after = _limits()

    # 1. DEADLINE STOP (pure, always runs when configured)
    if stop_after and now >= stop_after:
        trip("deadline-reached", f"now={now.isoformat()}Z >= {stop_after.isoformat()}Z")
        return

    # 2. TOTAL-LOSS STOP (skipped on bad data)
    pnl, ok = total_pnl()
    if not ok:
        _log({"event": "SKIP", "reason": "kalshi-read-failed", "note": "loss check skipped"})
        return
    if pnl <= -max_loss:
        trip("max-loss", f"total_pnl=${pnl:.2f} <= -${max_loss:.2f}")
        return

    rec = {"event": "OK", "total_pnl": round(pnl, 2), "floor": -max_loss}
    if stop_after:
        rec["days_to_deadline"] = round((stop_after - now).total_seconds() / 86400, 2)
    _log(rec)


if __name__ == "__main__":
    main()
