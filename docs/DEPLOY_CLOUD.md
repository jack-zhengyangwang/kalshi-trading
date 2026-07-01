# Deploying the paper arena to a cloud server

The arena (`arena.py`) is **cloud-portable**: keys load from the process env →
`.env` → `~/.zshenv`, and it runs **one cycle per invocation**, so a cron job
replaces launchd. A cloud box never sleeps, which fixes the laptop-sleep
deadlock that silently killed the tournament before.

**Kalshi needs no proxy** (US-regulated) — unlike the fund's Polymarket path.
This is **paper only**; no real orders. Real-money-from-cloud is a separate,
later decision.

## One-time setup on the VPS (any small Linux box, Python 3.10+)

```bash
# 1. copy the repo over (rsync/scp/git). Then:
cd /opt/ebk-personal
python3 -m pip install -r requirements.txt

# 2. secrets — do NOT commit these:
cp .env.example .env
#    edit .env: set ANTHROPIC_API_KEY and KALSHI_KEY_ID
scp your-mac:.../ebk-personal/MyPersonalAgent.txt .   # the Kalshi RSA private key

# 3. first run — reset, Claude-seed 16 teams, catch up every resolved game:
python3 arena.py --reset
```

## Keep it running (cron, every 2 minutes)

```bash
crontab -e
# add:
*/2 * * * * cd /opt/ebk-personal && /usr/bin/python3 arena.py >> logs/arena.out 2>&1
```

Each fire: settle+learn any newly-resolved game, manage live games in-play
(C/D), pre-game upcoming games (A/B). State persists in `arena_state/`, so
crashes/restarts resume cleanly.

## Check on it

```bash
tail -f logs/arena.out                 # live cycle output ([IN-PLAY]/[PRE-GAME])
cat logs/arena.jsonl | tail            # per-game standings tally
python3 -c "import json,glob,os; [print(os.path.basename(f), json.load(open(f))['account']['realized_pnl']) for f in glob.glob('arena_state/team_*.json')]"
```

## Notes
- `arena.py --catchup-only` replays resolved games without touching live (useful
  for a clean re-seed).
- To move the laptop tournament here, just run the setup above — the laptop
  launchd job can be unloaded once the VPS cron is confirmed working.
- Logs/state are gitignored; back up `arena_state/` if you want history.
