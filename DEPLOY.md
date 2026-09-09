# Deploy

The droplet is a plain Ubuntu box. There is no container and no orchestration:
deploying is an `rsync` plus a cron install.

**Current state (2026-09-07): deployed and verified, but idle.** The code, venv,
and secrets are in place at `/root/kalshi-trading` and the scanner authenticates
against live Kalshi. **No cron is installed**, so nothing runs on a schedule.
Install cron (below) when you want the system live.

## Target

| | |
|---|---|
| Host | `root@147.182.237.14` |
| Path | `/root/kalshi-trading` |

The path must match `TARGET` in `scripts/deploy.sh`. A mismatch here is the
historical cause of the "my change didn't take" / "disarm didn't work" bug
class: the deploy succeeds, writes to a directory nothing runs from, and
silently has no effect.

## First-time bootstrap

```bash
ssh root@147.182.237.14
apt update && apt install -y python3-venv python3-pip
mkdir -p /root/kalshi-trading
```

Then from your laptop:

```bash
./scripts/deploy.sh                                    # rsync the code
scp MyPersonalAgent.txt .env root@147.182.237.14:/root/kalshi-trading/
ssh root@147.182.237.14 'cd /root/kalshi-trading \
  && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt'
```

Secrets are excluded from rsync by design, so they are copied once, by hand.

## Routine deploy

```bash
./scripts/deploy.sh
```

`rsync --delete` is used, so **anything on the droplet that is not in the repo
gets deleted.** Excluded from deletion: `venv/`, `logs/`, state dirs, the two
secret files, and `data/market_history.db`. If you hand-edit a file on the
droplet, it is destroyed on the next deploy and exists nowhere else — don't.

The history DB is excluded for a specific reason: it is the one artefact here
that cannot be regenerated. Code, models, and state can all be rebuilt; a
snapshot the collector never took is gone permanently. Back it up before any
risky droplet work:

```bash
scp root@147.182.237.14:/root/kalshi-trading/data/market_history.db \
    ./data/market_history.$(date +%Y%m%d).db
```

## Cron

Not installed by the deploy; you install it deliberately when you want the
system live. Reference schedule:

```cron
*/15   * * * * cd /root/kalshi-trading && flock -n /tmp/collect.lock ./scripts/run_collect.sh >> logs/collect.out 2>&1
1-59/2 * * * * cd /root/kalshi-trading && flock -n /tmp/arena.lock  ./scripts/run_arena.sh   >> logs/arena.out  2>&1
3-59/5 * * * * cd /root/kalshi-trading && flock -n /tmp/promote.lock ./scripts/run_promote.sh >> logs/promote.out 2>&1
*/4    * * * * cd /root/kalshi-trading && flock -n /tmp/guard.lock  ./scripts/run_guard.sh   >> logs/guard.out  2>&1
*/5    * * * * pgrep -f wc.telegram_bot >/dev/null || (cd /root/kalshi-trading && nohup ./scripts/run_telegram.sh >/dev/null 2>&1 &)
```

`flock` prevents overlapping runs. Note `run_arena.sh` and `run_promote.sh` both
delegate into `wc/cycle.py` — running both concurrently runs the paper arena
twice (known issue #7).

**After any reboot, run `systemctl enable cron`.** A 2026-07-06 reboot left cron
disabled and the whole system was silent for 26 hours before anyone noticed.

### The collector is the one line worth installing on its own

`run_collect.sh` is **read-only against Kalshi** — it lists markets and writes to
a local SQLite file, and never places, cancels, or prices an order. It is safe
to run while the system is disarmed, and it *should* be: every cycle it does not
run is history that can never be recovered, whereas the trading crons can be
installed whenever you decide to go live.

So install it by itself first:

```bash
ssh root@147.182.237.14
cd /root/kalshi-trading
(crontab -l 2>/dev/null; echo '*/15 * * * * cd /root/kalshi-trading && flock -n /tmp/collect.lock ./scripts/run_collect.sh >> logs/collect.out 2>&1') | crontab -
systemctl enable cron && systemctl start cron
```

Check it after ten minutes — two cycles should have landed:

```bash
ssh root@147.182.237.14 'cd /root/kalshi-trading \
  && tail -3 logs/collect.jsonl \
  && ./venv/bin/python3 -m wc.backtest.quality --interval 900'
```

`flock` matters here for the same reason it does elsewhere: a cycle takes ~50s
and the interval is 900s, so overlap is unlikely but a slow API day would
otherwise stack cycles.

**Why 15 minutes and not 5:** Kalshi serves 1-minute historical candles for any
market that settles (see `wc/backtest/backfill.py`), so the collector is not
where resolution comes from. What it uniquely provides is the survivorship
record — `first_seen` answers "what existed on date D", including markets that
never resolved and that backfill can therefore never see — plus the live book.
At 5 minutes it was writing ~700k rows a day for resolution available for free
elsewhere. Snapshot timestamps are floored to the interval anyway,
so even a double-fire overwrites rather than duplicating.

**If `wc.backtest.quality` reports anything under "BLOCKING", stop and fix it
before building on the data.** A backtest on bad history produces a confident
wrong number, which is worse than no backtest.

## Safety

Deploys land disarmed. Arming is a separate, deliberate edit to
`config/switchboard_v3.json` on the droplet:

```bash
ssh root@147.182.237.14 'cd /root/kalshi-trading && cat config/switchboard_v3.json'
```

To stop everything immediately:

```bash
ssh root@147.182.237.14 "cd /root/kalshi-trading && ./venv/bin/python3 -c \"
import json,collections
p='config/switchboard_v3.json'
d=json.load(open(p),object_pairs_hook=collections.OrderedDict)
d['master'].update(armed=False, kill=True, kill_flattens=True)
json.dump(d,open(p,'w'),indent=2)\""
```

`guard.py` (cron, every 4 min) enforces this unattended: it trips the kill
switch when total P&L hits `guard.max_loss_dollars` or the optional
`guard.stop_after_utc` deadline passes.
