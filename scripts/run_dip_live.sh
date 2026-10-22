#!/bin/bash
# Dip catcher with REAL orders (post-only, 1 contract, hard $ cap). Run on the droplet:
#   nohup scripts/run_dip_live.sh 2026-10-22 30 --live > logs/dip_live_run.out 2>&1 < /dev/null &
# Without --live it only logs what it would do. Stop early (cancels all orders):
#   touch logs/dip_live.KILL   (delete it yourself before the next start)
set -e
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
PY="./venv/bin/python3"; [ -x "$PY" ] || PY="python3"
exec "$PY" -m wc.live.dip_live --date "${1:?date}" --max-dollars "${2:?max dollars}" ${3:-}
