#!/bin/bash
# Live-promote v3 cron — REAL pilot. Fires real orders only if switchboard armed.
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env 2>/dev/null; set +a
exec python3 -m wc.promote --execute
