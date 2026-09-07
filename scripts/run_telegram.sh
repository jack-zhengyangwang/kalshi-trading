#!/bin/bash
# Telegram bot long-polling daemon. Restart via watchdog cron if it dies.
set -e
cd "$(dirname "$0")/.." || exit 1

# Source secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, KALSHI_KEY_ID)
[ -f .env ] && set -a && . ./.env && set +a

# cron runs with a minimal PATH — prefer the venv interpreter
PY="./venv/bin/python3"; [ -x "$PY" ] || PY="python3"

exec "$PY" -m wc.telegram_bot >> logs/telegram.out 2>&1
