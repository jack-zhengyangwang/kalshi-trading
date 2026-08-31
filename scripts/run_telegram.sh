#!/bin/bash
# Telegram bot long-polling daemon. Restart via watchdog cron if it dies.
set -e
cd "$(dirname "$0")/.." || exit 1

# Source secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, KALSHI_KEY_ID)
[ -f .env ] && set -a && . ./.env && set +a

exec python3 -m wc.telegram_bot >> logs/telegram.out 2>&1
