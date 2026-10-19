#!/bin/sh
# v2 Phase 2, full data (run on the droplet):
#   nohup scripts/run_phase2.sh > logs/phase2_run.log 2>&1 &
#
# Works on a SNAPSHOT of market_history.db so the collector's file is only
# read, never written (the kickoff build adds a `kickoffs` table to the DB it
# is given). Protocol: docs/NEXT_CHAPTER_v2.md, "Phase 2 protocol".
set -e
cd "$(dirname "$0")/.." || exit 1
PY="./venv/bin/python"
SNAP="data/research/history_snapshot.db"
mkdir -p data/research logs

echo "[$(date -u +%FT%TZ)] snapshot -> $SNAP"
rm -f "$SNAP"
"$PY" -c "import sqlite3; sqlite3.connect('data/market_history.db').execute(\"VACUUM INTO '$SNAP'\")"

echo "[$(date -u +%FT%TZ)] kickoff table (ESPN = truth, Kalshi = check)"
"$PY" -m wc.research.kickoff --db "$SNAP"

echo "[$(date -u +%FT%TZ)] dataset (Table A + Table B)"
"$PY" -m wc.research.dataset --history-db "$SNAP"

echo "[$(date -u +%FT%TZ)] phase 2"
"$PY" -W ignore -m wc.research.phase2

echo "[$(date -u +%FT%TZ)] done"
