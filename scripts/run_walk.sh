#!/bin/bash
# Walk-forward the firm with managers. Sources .env so OPENROUTER_API_KEY /
# ANTHROPIC_API_KEY reach the managers. Pass any wc.firm.walk flags through.
#   scripts/run_walk.sh --tick-days 2 --start 2026-07-10
#   scripts/run_walk.sh --tick-days 7 --source collector      # the arena
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
PY=venv/bin/python; [ -x "$PY" ] || PY=python3
exec "$PY" -u -m wc.firm.walk "$@"   # -u: unbuffered, so the log shows progress
