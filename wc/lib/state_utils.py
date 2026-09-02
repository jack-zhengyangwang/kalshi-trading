"""
state_utils.py — Shared file-state helpers extracted from v2 arena_base.

Small utility functions used by arena.py, cycle.py, and promote.py for
JSON state persistence and Kalshi maintenance-window detection.
"""
import datetime as dt
import json
import os


def kalshi_maintenance():
    """True during Kalshi's daily maintenance window (3:00–5:00am ET) — the API is
    down then, so cron cycles should skip rather than spin on retries."""
    try:
        from zoneinfo import ZoneInfo
        h = dt.datetime.now(ZoneInfo("America/New_York")).hour
        return 3 <= h < 5
    except Exception:
        return False


def load_json(path, default=None):
    """Load a JSON file, returning `default` on FileNotFound or decode error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    """Atomically write a JSON object to `path` (tmp + rename)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
