#!/bin/bash
# Deploy to the droplet. Rsyncs the repo (excluding venv/cache/logs/state) then
# restarts the cron jobs. Requires SSH key access to the droplet.
set -e
cd "$(dirname "$0")/.." || exit 1

DROPLET="${1:-root@147.182.237.14}"
TARGET="/root/kalshi-trading"

echo "Deploying to $DROPLET:$TARGET ..."

# NOTE: --delete means anything on the droplet that is not here is destroyed.
# data/market_history.db is excluded because it is the ONE artefact in this
# system that cannot be regenerated: every snapshot the collector missed is
# history Kalshi will not sell back to us. Without this line, a routine deploy
# silently deletes months of collection.
rsync -avz --delete \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'arena_v3_cache/' \
    --exclude 'arena_v3_state/' \
    --exclude 'arena_v2_state/' \
    --exclude 'logs/' \
    --exclude 'data/market_history.db' \
    --exclude 'data/backtests/' \
    --exclude '.git/' \
    --exclude '.claude/' \
    --exclude 'legacy/' \
    --exclude 'MyPersonalAgent.txt' \
    --exclude '.env' \
    ./ "$DROPLET:$TARGET/"

echo "Deploy done. Verifying droplet..."
ssh "$DROPLET" "cd $TARGET \
  && echo -n 'armed/kill: ' \
  && ./venv/bin/python3 -c \"import json;m=json.load(open('config/switchboard_v3.json'))['master'];print(m['armed'], m['kill'])\" \
  && (crontab -l 2>/dev/null | grep -q 'run_promote' || echo 'NOTE: no promoter cron installed (system idle)')"

echo "OK"
