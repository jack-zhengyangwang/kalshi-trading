# Deploy

The droplet is a plain Ubuntu box. There is no container and no orchestration:
deploying is an `rsync` plus a cron install.

**Current state (2026-09-07): the droplet is empty.** It was wiped for a clean
rebuild. Nothing is deployed and nothing is trading.

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
gets deleted.** Excluded from deletion: `venv/`, `logs/`, state dirs, and the
two secret files. If you hand-edit a file on the droplet, it is destroyed on
the next deploy and exists nowhere else — don't.

## Cron

Not installed by the deploy; you install it deliberately when you want the
system live. Reference schedule:

```cron
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
