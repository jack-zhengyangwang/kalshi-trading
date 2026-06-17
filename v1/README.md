# v1 — legacy arena (frozen)

This is the **original** WorldCupTrading system: a fixed tournament of **16 teams** (4 market categories — winner / spread / team-totals / game-events — × 4 strategy archetypes A/B/C/D), one shared Brain per category blending an LLM + an ELO model + the market, with per-team learning ("Agent 3", `agents/tournament_trainer.py`).

It's kept here for reference. **Active development is on v2** (at the repo root) — see the top-level [README](../README.md). v2 generalizes everything v1 pioneered: the Brain-vs-teams split, the shared exit logic, Kelly + time-value sizing, and the catch-up-replay tournament.

## Run it
```bash
cd v1
python3 arena.py --reset    # wipe state, replay all resolved games, train the 16 teams
python3 arena.py            # later: catch up new games + keep learning
python3 arena.py            # --status-style standings print at the end of each run
```

v1 reuses the **shared core** at the repo root (`group/`, `models/`, `kalshi_client.py`) via a small `sys.path` shim at the top of `arena.py` — so it runs standalone from this folder. Setup (keys, ELO model) is identical to v2; see [SETUP.md](../SETUP.md).
