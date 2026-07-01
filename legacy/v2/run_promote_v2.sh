#!/bin/bash
# Promotion executor cron wrapper: load secrets (.env), run one --execute cycle.
# Real orders only fire if switchboard_v2.json master.armed=true (and !kill).
cd /root/ebk-personal || exit 1
set -a
. ./.env 2>/dev/null
set +a
exec ./venv/bin/python3 live_promote_v2.py --execute
