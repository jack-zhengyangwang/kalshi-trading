# Setup — keys, wallet, and running locally

## 0. Prerequisites
- Python 3.10+ and `pip`
- A [Kalshi](https://kalshi.com) account (US-based — Kalshi is a US-regulated exchange)
- An [Anthropic](https://console.anthropic.com) account (for the Brain's LLM source)

```bash
pip install -r requirements.txt
```

---

## 1. Kalshi API credentials (Key ID + RSA private key)

The client authenticates with an **RSA key pair** (recommended) — you sign each request, so nothing secret travels over the wire.

1. Log in to Kalshi → **Profile → API Keys → "Create API Key"**.
2. You get two things:
   - a **Key ID** (a UUID like `a1b2c3d4-…`)
   - a **private key file** to download (an RSA PEM). **You can only download it once** — save it.
3. Put the private key file somewhere in the repo (it's gitignored), e.g. `./kalshi_private_key.pem`.
4. Set, in your `.env` (next step):
   - `KALSHI_KEY_ID` = your Key ID
   - `KALSHI_PRIVATE_KEY_FILE` = the path to that PEM (e.g. `./kalshi_private_key.pem`)

> **Bearer fallback:** if you'd rather use a bearer token, set `KALSHI_API_KEY` instead and skip the PEM. RSA is preferred.

**Wallet / funding:** the paper arena only *reads* market data, so you don't need a funded wallet to run it. You'd only fund the wallet if you later wire up real execution (out of scope for this repo — keep it paper).

---

## 2. Anthropic API key

The Brain blends an LLM estimate into the `winner`-market fair value. Get a key at
**console.anthropic.com → API Keys**, then set `ANTHROPIC_API_KEY` in `.env`.

> Cost is small — the arena uses a cheap model (`claude-haiku-*`) and only for the
> winner category. The other categories use the stats model only (no LLM).

---

## 3. Create your `.env`

```bash
cp .env.example .env
```
Edit `.env`:
```
KALSHI_KEY_ID=a1b2c3d4-....
KALSHI_PRIVATE_KEY_FILE=./kalshi_private_key.pem
ANTHROPIC_API_KEY=sk-ant-....
```
`group/env_portable.py` loads these from (in order): the process environment → this `.env` → `~/.zshenv`. The same code therefore runs unchanged on a laptop or a cloud server.

**Never commit `.env` or the PEM.** Both are already in `.gitignore`.

---

## 4. One-time model setup
```bash
python3 group/run.py --fetch-stats   # download team ELO ratings → models/team_elo.json
python3 models/train.py              # train the win-probability model → models/model.pkl
```
(If `clubelo.com` is unreachable, `--fetch-stats` falls back to bundled ratings.)

---

## 5. Run the arena
```bash
python3 arena.py --reset    # wipe state, replay every resolved game, train all teams
python3 arena.py            # later: catch up new games + keep learning
```
Outputs:
- `logs/arena.jsonl` — one line per game with the running standings
- `logs/arena.out` — the live console log
- `arena_state/` — each team's params, brain, account, and ledger (this is the "save file")

Verify auth is working: on start you should see `[KALSHI] RSA auth — key_id=…`. If you see `No auth credentials found`, re-check `.env` and the PEM path.

To keep it running 24/7 on a server, see **[DEPLOY.md](DEPLOY.md)**.
