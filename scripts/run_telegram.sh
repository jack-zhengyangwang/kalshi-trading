#!/bin/bash
# Telegram bot launcher: sources .env then runs the long-polling bot.
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env 2>/dev/null; set +a
exec python3 -m wc.telegram_bot >> logs/telegram.out 2>&1
