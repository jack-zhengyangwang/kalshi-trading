# Setup

Local development environment for `kalshi-trading`.

## Requirements

- Python 3.10+
- A Kalshi account with API access (RSA key pair)
- Optional: Anthropic API key (LLM pricing leg), Telegram bot token (mobile control)

## Local install

```bash
git clone https://github.com/jack-zhengyangwang/kalshi-trading.git ~/dev/kalshi-trading
cd ~/dev/kalshi-trading
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> Do not put this repo under `~/Desktop` — it is iCloud-synced, files evict to
> `dataless` placeholders and git hangs for minutes. See CLAUDE.md.

## Secrets

Two files, both gitignored. Neither is ever committed or rsynced by `deploy.sh`.

**`MyPersonalAgent.txt`** — your Kalshi RSA private key, repo root.

**`.env`** — repo root:

```bash
KALSHI_KEY_ID=<uuid from the Kalshi API keys page>
ANTHROPIC_API_KEY=<optional; only needed when a brain has use_llm=true>
TELEGRAM_BOT_TOKEN=<optional>
TELEGRAM_CHAT_ID=<optional>
```

## Verify

```bash
source venv/bin/activate
python3 -m pytest tests/ -q          # test suite
python3 -m wc.arena --status         # paper arena state
python3 guard.py                     # safety watchdog, one pass
```

## Safety defaults

`config/switchboard_v3.json` ships **disarmed**: `master.armed=false`,
`master.kill=true`. A real order requires `armed=true` AND `kill=false` AND the
promoter invoked with `--execute`. Leave it disarmed until you have deliberately
decided to trade real money.
