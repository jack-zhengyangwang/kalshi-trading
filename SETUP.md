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
python3 models/fetch_stats.py        # download team ELO ratings → models/team_elo.json
python3 models/train.py              # train the win-probability model → models/model.pkl
# (optional) regenerate the v2 corner model from the bundled corpus:
python3 models/train_corners.py      # → models/corners_model.json (already included)
```
(If `clubelo.com` is unreachable, ELO falls back to bundled ratings.)

---

## 5. Run the arena (v2)
```bash
python3 arena_v2.py --replay 30   # seed the evolving population from resolved games
python3 arena_v2.py --once        # one live cycle: settle → scan → bet → exit → evolve
python3 arena_v2.py --status      # leaderboard: fitness, P&L, params per team
```
Outputs:
- `logs/arena_v2.jsonl` — one line per cycle
- `arena_v2_state/` — per-category team populations, stacker weights, LLM cache (the "save file")

Verify auth is working: on start you should see `[KALSHI] RSA auth — key_id=…`. If you see `No auth credentials found`, re-check `.env` and the PEM path.

> The legacy v1 arena runs the same way from its folder: `cd v1 && python3 arena.py --reset`.

To keep it running 24/7 on a server, see **[DEPLOY.md](DEPLOY.md)**.
