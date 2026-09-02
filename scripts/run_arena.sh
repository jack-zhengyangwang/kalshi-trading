#!/bin/bash
# Paper arena forward cycle (v3). One cycle per invocation — cron fires this
# every 2 min. Paper only — never places real orders.
set -e
cd "$(dirname "$0")/.." || exit 1

# Source secrets (KALSHI_KEY_ID)
[ -f .env ] && set -a && . ./.env && set +a

exec python3 -m wc.arena --once
