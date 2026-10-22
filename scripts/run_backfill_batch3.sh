#!/bin/bash
# Batch 3 data (run on the droplet, ~4-5h at the client's 4 req/s throttle):
#   nohup scripts/run_backfill_batch3.sh > logs/backfill_batch3.out 2>&1 &
#
# READ-ONLY against Kalshi: pulls the candle archive for settled markets into
# backfill_candles (upserts, so overlap with earlier runs is harmless). Places
# no orders. Runs alongside the collector (WAL + busy_timeout).
#   1. hourly bars, last 50 days   -> extends the backfill past 2026-09-14
#   2. 1-minute bars, 30h before each game's expected end (kickoff-27h to
#      kickoff+3h), every settled market Kalshi still lists
set -e
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
PY="./venv/bin/python3"; [ -x "$PY" ] || PY="python3"

echo "[$(date -u +%FT%TZ)] hourly, 50 days"
"$PY" -m wc.backtest.backfill --days 50 --interval 60

echo "[$(date -u +%FT%TZ)] 1-minute, 30h match windows, 120 days"
"$PY" -m wc.backtest.backfill --days 120 --interval 1 --window-hours 30

echo "[$(date -u +%FT%TZ)] done"
