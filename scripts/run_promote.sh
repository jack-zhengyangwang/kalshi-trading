#!/bin/bash
# Real-money promoter (v3). A real order fires ONLY when:
#   switchboard_v3.json master.armed=true AND --execute AND master.kill=false
# Otherwise: dry-run mirror — logs intended orders, places none.
set -e
cd "$(dirname "$0")/.." || exit 1

# Source secrets (KALSHI_KEY_ID, ANTHROPIC_API_KEY)
[ -f .env ] && set -a && . ./.env && set +a

# cron runs with a minimal PATH — prefer the venv interpreter
PY="./venv/bin/python3"; [ -x "$PY" ] || PY="python3"

exec "$PY" -m wc.promote --execute
