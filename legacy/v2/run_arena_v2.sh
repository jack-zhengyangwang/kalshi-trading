#!/bin/bash
# Arena v2 cron wrapper: load secrets from .env (for ANTHROPIC_API_KEY, used by
# the LLM brain) then run one cycle. Cron calls this under flock.
cd /root/ebk-personal || exit 1
set -a
. ./.env 2>/dev/null
set +a
exec ./venv/bin/python3 arena_v2.py --once
