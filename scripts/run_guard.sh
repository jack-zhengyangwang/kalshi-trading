#!/bin/bash
# Safety watchdog cron. Enforces the limits in switchboard_v3.json "guard":
#   max_loss_dollars — total-loss floor; stop_after_utc — optional hard deadline.
# Trips the kill switch (promote.py then flattens). See guard.py.
set -e
cd "$(dirname "$0")/.." || exit 1

[ -f .env ] && set -a && . ./.env && set +a

# cron runs with a minimal PATH — prefer the venv interpreter
PY="./venv/bin/python3"; [ -x "$PY" ] || PY="python3"

exec "$PY" guard.py
