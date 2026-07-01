"""
notify.py — best-effort local alerts ("ping" the user).

On macOS, posts a native notification via osascript. Always also prints a loud
console line and appends to logs/alerts.jsonl so nothing is lost if the GUI
notification can't fire (e.g. headless run).
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
ALERTS_PATH = os.path.join(LOG_DIR, "alerts.jsonl")


def ping(title, message, payload=None):
    """Fire a local notification + console alert + append to alerts.jsonl."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Console (always visible in the run output)
    print(f"\n  *** PING [{title}] {message} ***\n", flush=True)

    # 2. macOS native notification (best effort)
    if sys.platform == "darwin":
        try:
            safe_msg = message.replace('"', "'")
            safe_title = title.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe_msg}" with title "EBK Trader" subtitle "{safe_title}" sound name "Glass"'],
                timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # 3. Durable log
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(ALERTS_PATH, "a") as f:
            f.write(json.dumps({"ts": ts, "title": title, "message": message,
                                "payload": payload or {}}) + "\n")
    except Exception:
        pass
