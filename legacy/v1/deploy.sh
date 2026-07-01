#!/usr/bin/env bash
# Run this ON the DigitalOcean droplet, inside the extracted ebk-personal/ dir.
# It installs deps into a venv and runs the first catch-up. You must have already
# put your keys in .env and copied MyPersonalAgent.txt into this directory.
set -e
cd "$(dirname "$0")"

echo ">>> installing python + venv"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# sanity: keys + RSA present
if [ ! -f .env ]; then cp .env.example .env; fi
. ./.env 2>/dev/null || true
if [ -z "$ANTHROPIC_API_KEY" ] || [ -z "$KALSHI_KEY_ID" ]; then
  echo "!!! .env is missing ANTHROPIC_API_KEY or KALSHI_KEY_ID — edit .env then re-run."; exit 1
fi
if [ ! -f MyPersonalAgent.txt ]; then
  echo "!!! MyPersonalAgent.txt (Kalshi RSA key) not found — scp it here then re-run."; exit 1
fi

echo ">>> first run: reset + Claude-seed 16 teams + catch up every resolved game"
./venv/bin/python3 arena.py --reset

CRON="*/2 * * * * cd $(pwd) && ./venv/bin/python3 arena.py >> logs/arena.out 2>&1"
echo ""
echo ">>> DONE. To run it 24/7, add this cron line (run: crontab -e):"
echo "    $CRON"
echo ">>> watch it:  tail -f logs/arena.out   |   tally:  cat logs/arena.jsonl"
