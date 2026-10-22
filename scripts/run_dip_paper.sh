#!/bin/bash
# Dip catcher, PAPER (no orders are ever sent), on live Kalshi data.
# Rules: docs/findings/13_DIP_PAPER_PLAN.md. Run on the droplet:
#   nohup scripts/run_dip_paper.sh 2026-10-22 > logs/dip_run_2026-10-22.out 2>&1 &
# Exits by itself 30 min after the last game's expected end. Then:
#   ./venv/bin/python -m wc.live.dip_paper report --date 2026-10-22
set -e
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
PY="./venv/bin/python3"; [ -x "$PY" ] || PY="python3"
exec "$PY" -m wc.live.dip_paper run --date "${1:?usage: run_dip_paper.sh YYYY-MM-DD}"
