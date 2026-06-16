"""
env_portable.py — load API keys from whatever environment we're in.

Order: real process env (already-exported, e.g. cloud/systemd) → a `.env` file
in the repo root (cloud VPS) → ~/.zshenv (the laptop). This lets the same code
run unchanged on the Mac (launchd + ~/.zshenv) and on a cloud server (.env / cron),
which is the whole point of being cloud-portable.
"""
import os
import re

_KEYS = ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_FILE", "ANTHROPIC_API_KEY", "KALSHI_API_KEY")
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_file(path):
    if not os.path.exists(path):
        return
    try:
        for line in open(path):
            m = re.match(r'\s*(?:export\s+)?(' + "|".join(_KEYS) +
                         r')\s*=\s*["\']?([^"\'\n]+)["\']?', line)
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = m.group(2).strip()
    except Exception:
        pass


def load_keys():
    """Populate os.environ with the API keys from the first source that has them."""
    # 1. already in the process env (cloud / systemd / docker) → nothing to do.
    if all(os.environ.get(k) for k in ("KALSHI_KEY_ID", "ANTHROPIC_API_KEY")):
        return
    # 2. repo-root .env (cloud VPS), then 3. ~/.zshenv (laptop).
    _load_file(os.path.join(_BASE, ".env"))
    _load_file(os.path.expanduser("~/.zshenv"))
