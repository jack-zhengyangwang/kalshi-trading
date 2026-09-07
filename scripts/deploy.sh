#!/bin/bash
# Deploy to the droplet. Rsyncs the repo (excluding venv/cache/logs/state) then
# restarts the cron jobs. Requires SSH key access to the droplet.
set -e
cd "$(dirname "$0")/.." || exit 1

DROPLET="${1:-root@147.182.237.14}"
TARGET="/root/WorldCupTrading"

echo "Deploying to $DROPLET:$TARGET ..."

rsync -avz --delete \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'arena_v3_cache/' \
    --exclude 'arena_v3_state/' \
    --exclude 'arena_v2_state/' \
    --exclude 'logs/' \
    --exclude '.git/' \
    --exclude '.claude/' \
    --exclude 'legacy/' \
    --exclude 'guard.py' \
    --exclude 'group/' \
    --exclude 'MyPersonalAgent.txt' \
    --exclude '.env' \
    ./ "$DROPLET:$TARGET/"

echo "Deploy done. Restarting cron on droplet..."
ssh "$DROPLET" "cd $TARGET && crontab -l 2>/dev/null | grep -v '^#' | grep -q 'run_promote' || echo 'WARNING: no promoter cron found'"

echo "OK"
