#!/bin/bash
# Arena v3 cron wrapper: forward paper cycle. Sources .env for Kalshi auth.
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env 2>/dev/null; set +a
exec python3 -m wc.arena --once
