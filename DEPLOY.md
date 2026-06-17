# Deploy — keep the arena running 24/7 on a cloud server

Running on your laptop is fine for experimenting, but to catch **every game live**
and keep the teams learning continuously, run it on a small always-on server. The
code is cloud-portable by design (`group/env_portable.py` loads keys from the
process env or a `.env` file), so deployment is just: copy the repo, set keys, add a cron.

> **Region matters:** Kalshi is a US-regulated exchange and its API expects US access.
> Put your server in a **US region** (e.g. NYC/SFO). Non-US regions may be geoblocked.

---

## 1. Get a server
Any small VPS works — a $4–6/month instance is plenty (this is light, mostly I/O).
DigitalOcean, Hetzner, Vultr, AWS Lightsail, etc. Pick **Ubuntu, US region**, and
add your SSH key during creation so you can log in without a password.

```bash
ssh root@YOUR_SERVER_IP
```

## 2. Install dependencies
```bash
apt update && apt install -y python3 python3-venv python3-pip git
git clone https://github.com/<you>/WorldCupTrading.git
cd WorldCupTrading
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 3. Add your keys (never commit these)
```bash
cp .env.example .env
nano .env          # set KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_FILE, ANTHROPIC_API_KEY
```
Upload your Kalshi RSA private key to the path you set in `KALSHI_PRIVATE_KEY_FILE`
(from your laptop):
```bash
scp ./kalshi_private_key.pem root@YOUR_SERVER_IP:~/WorldCupTrading/kalshi_private_key.pem
chmod 600 ~/WorldCupTrading/kalshi_private_key.pem
```

## 4. One-time setup + a test run
```bash
./venv/bin/python3 models/fetch_stats.py
./venv/bin/python3 models/train.py
./venv/bin/python3 arena_v2.py --replay 30   # seed the population from resolved games
./venv/bin/python3 arena_v2.py --status      # confirm teams + standings
```
You should see `[KALSHI] RSA auth — key_id=…`.

## 5. Keep it running with cron
The arena does **one cycle per invocation** (settle → scan → bet → exit → evolve), so schedule
it on a timer. Every 2 minutes is a good default that stays under Kalshi's rate limit:

```bash
crontab -e
```
Add:
```
*/2 * * * * cd /root/WorldCupTrading && flock -n /tmp/arena_v2.lock ./venv/bin/python3 arena_v2.py --once >> logs/arena_v2.out 2>&1
```
- `flock -n /tmp/arena_v2.lock` ensures a slow cycle can never overlap the next one and corrupt state.
- The LLM (if enabled) is cadence-limited to one call per game per category; in-play legs are re-priced each cycle.

> **Rate limits:** each in-play cycle makes a burst of orderbook calls. 2-minute
> cadence is safe; going faster (1-min/30s) can trip Kalshi's per-window limit
> unless you add request throttling + 429 backoff to the fetch path first.

## 6. Watch it
```bash
tail -f logs/arena_v2.out                      # live console
./venv/bin/python3 arena_v2.py --status        # leaderboard
tail -5 logs/arena_v2.jsonl                     # one line per cycle
```

## 7. Security
- `.env` and the `.pem` live **only** on the server — never in git (both are gitignored).
- `chmod 600` the PEM. Lock SSH to key-only auth. Consider a non-root user.
- This repo is paper-only; if you ever add live execution, treat the server as
  production: least privilege, backups, and a kill switch.
