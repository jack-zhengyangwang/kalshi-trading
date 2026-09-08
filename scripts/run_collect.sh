#!/bin/bash
# Forward collector — one snapshot cycle per invocation. Cron fires this every
# 5 minutes. READ-ONLY against Kalshi: it lists markets and writes to the local
# history DB. It never places, cancels, or touches an order, so it is safe to
# run while the trading system is disarmed — and it should be, because the data
# is what makes arming worthwhile later.
set -e
cd "$(dirname "$0")/.." || exit 1

# Source secrets (KALSHI_KEY_ID)
[ -f .env ] && set -a && . ./.env && set +a

# cron runs with a minimal PATH — prefer the venv interpreter
PY="./venv/bin/python3"; [ -x "$PY" ] || PY="python3"

exec "$PY" -m wc.backtest.collect --once >> logs/collect.log 2>&1
