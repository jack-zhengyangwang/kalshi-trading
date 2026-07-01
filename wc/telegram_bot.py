#!/usr/bin/env python3
"""Personal Telegram bot for the WC v3 real-money pilot. Standalone (only touches the
droplet + Kalshi; no EBK dependency). Long-polls Telegram, obeys ONLY the allowlisted
chat id, and pushes alerts on new real orders.

Commands: /status /pnl /positions /agents  ·  /arm /disarm /kill /cap <n>
(/arm and /kill require a confirm reply within 60s — real money.)

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from env (.env). Run via run_telegram.sh.
"""
import json
import os
import sys
from wc import paths
import subprocess
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
API = f"https://api.telegram.org/bot{TOKEN}"
SB = paths.SWITCHBOARD_V3
PLOG = os.path.join(paths.LOGS_DIR, "promote_v3.jsonl")
PY = None  # unused: subprocess uses sys.executable -m
HELP = ("WC Pilot bot 🤖\n"
        "/pilot   real-money pilot — total P&L + team table\n"
        "/arena   paper arena — top-3 + worst per category (forward)\n"
        "/investigate <agent>  one agent's bets in detail\n"
        "/status /pnl /positions /agents  = /pilot\n"
        "/arm  → /arm_confirm  (arm real money)\n"
        "/disarm  stop new entries (exits keep running)\n"
        "/kill  → /kill_confirm  (flatten + halt)\n"
        "/cap <n>  set daily cap, e.g. /cap 200")
_pending = {}


def send(text):
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": CHAT, "text": text[:4000]}, timeout=15)
    except Exception as e:
        print("send err", e, flush=True)


def _sb():
    return json.load(open(SB))


def _save_sb(d):
    tmp = SB + ".tmp"
    json.dump(d, open(tmp, "w"), indent=2)
    os.replace(tmp, SB)


def _set_master(k, v):
    d = _sb(); d["master"][k] = v; _save_sb(d)
    return d["master"]


def _mono(body):
    return "```\n" + body.strip()[-3500:] + "\n```"


def status_text():
    try:
        out = subprocess.run([sys.executable, "-m", "wc.pilot_status"],
                             capture_output=True, text=True, timeout=70, cwd=paths.ROOT, env=os.environ)
        return _mono(out.stdout or out.stderr or "no output")
    except Exception as e:
        return f"status error: {e}"


def arena_text():
    try:
        out = subprocess.run([sys.executable, "-m", "wc.arena", "--forward-report"],
                             capture_output=True, text=True, timeout=90, cwd=paths.ROOT, env=os.environ)
        return _mono(out.stdout or out.stderr or "no output")
    except Exception as e:
        return f"arena error: {e}"


def investigate_text(arg):
    arg = arg.strip()
    if not arg:
        return "usage: /investigate <agent>  (e.g. /investigate flow)"
    try:
        out = subprocess.run([sys.executable, "-m", "wc.investigate", arg],
                             capture_output=True, text=True, timeout=90, cwd=paths.ROOT, env=os.environ)
        return _mono(out.stdout or out.stderr or "no output")
    except Exception as e:
        return f"investigate error: {e}"


def handle(text):
    t = text.strip().lower()
    if t in ("/start", "/help"):
        return HELP
    if t in ("/status", "/pilot", "/pnl", "/positions", "/agents"):
        return status_text()
    if t == "/arena":
        return arena_text()
    if t.startswith("/investigate"):
        parts = t.split(None, 1)
        return investigate_text(parts[1] if len(parts) > 1 else "")
    if t == "/arm":
        _pending["arm"] = time.time()
        return "⚠️ ARM real money? reply /arm_confirm within 60s"
    if t == "/arm_confirm":
        if time.time() - _pending.get("arm", 0) < 60:
            return f"✅ ARMED. {_set_master('armed', True)}"
        return "no pending arm — send /arm first"
    if t == "/disarm":
        return f"⏸️ disarmed (exits still managed). {_set_master('armed', False)}"
    if t == "/kill":
        _pending["kill"] = time.time()
        return "⚠️ KILL = flatten all real positions + halt. reply /kill_confirm within 60s"
    if t == "/kill_confirm":
        if time.time() - _pending.get("kill", 0) < 60:
            return f"🛑 KILLED (flatten next cycle). {_set_master('kill', True)}"
        return "no pending kill — send /kill first"
    if t.startswith("/cap"):
        try:
            n = float(t.split()[1])
            _set_master("daily_cap_dollars", n)
            return f"daily cap set to ${n:.0f}"
        except Exception:
            return "usage: /cap 200"
    return "unknown — /help"


def _n_real():
    try:
        b = [l for l in open(PLOG) if '"mode": "REAL-ORDERS"' in l and '"act": "BUY"' in l]
        return len(b), sum(1 for l in b if '"in_play": true' in l)
    except Exception:
        return 0, 0


def main():
    if not TOKEN or not CHAT:
        print("missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in env", flush=True)
        return
    send("🤖 WC Pilot bot online. /help for commands.")
    offset = None
    last_total, last_ip = _n_real()
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"timeout": 25, "offset": offset},
                             timeout=35).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                if str((msg.get("chat") or {}).get("id")) != CHAT:
                    continue                       # allowlist: ignore everyone else
                txt = msg.get("text", "")
                if txt:
                    send(handle(txt))
        except Exception as e:
            print("poll err", e, flush=True)
            time.sleep(5)
        # push alert: new real orders since last check
        try:
            tot, ip = _n_real()
            if tot > last_total:
                new_ip = ip - last_ip
                send(f"🟢 {tot - last_total} new REAL order(s)"
                     + (f" — {new_ip} IN-PLAY" if new_ip > 0 else " (pre-game)")
                     + f". total {tot}. /status for detail.")
                last_total, last_ip = tot, ip
        except Exception:
            pass


if __name__ == "__main__":
    main()
