# Architecture

## Pipeline

One cycle, driven by `wc/cycle.py`. Both cron entry points delegate into it.

```
scan → price → strategise → size → bid → exit → record → improve
```

| Stage | Module | What it does |
|---|---|---|
| **scan** | `wc/scanner.py` | Discovers every open Soccer series live from the Kalshi catalog by the `Soccer` tag — no hardcoded series list. Cached 1h in `arena_v3_state/soccer_series_cache.json`. |
| **price** | `wc/brains.py` → `wc/brain_v4.py` | One `BrainV4` per league. Blends structural Poisson/NegBin models, market data, and an optional LLM leg. |
| **strategise** | `wc/strategy.py` | A population of agents with differing risk/edge parameters, evolved by GA. |
| **size** | `wc/lib/kelly.py` | Kelly sizing, clamped by the switchboard caps. |
| **bid** | `wc/promote.py` | Places orders — real only when armed, unkilled, and `--execute`. |
| **exit** | `wc/lib/exit_rules.py` | Per-category exit modes; 80% circuit breaker. |
| **record** | `wc/prediction_db.py` | Every prediction logged, later graded against settlement. |
| **improve** | `wc/brains.py::train` | Re-tunes league brains from graded outcomes. |

## Layout

```
kalshi-trading/
├── guard.py              # safety watchdog — deliberately OUTSIDE wc/
├── wc/
│   ├── cycle.py          # the pipeline; single source of truth for a cycle
│   ├── scanner.py        # v4 live Soccer discovery
│   ├── brain_v4.py       # per-league pricing
│   ├── brains.py         # brain set: load / save / train
│   ├── promote.py        # real-money path
│   ├── arena.py          # paper arena
│   ├── paths.py          # ALL filesystem anchors resolve here
│   ├── core/             # v2 base classes that v3/v4 subclass
│   ├── lib/              # kelly, exit rules, market data, elo
│   └── kalshi/           # API client
├── config/               # switchboard, leagues, categories
├── scripts/              # cron wrappers + deploy
├── data/                 # graded predictions (evaluation history)
├── models/               # trained Elo / corners models
└── v3_archive/           # superseded v3 code, kept for reference
```

## Rules

**Paths.** Every module resolves files through `wc/paths.py`. Never re-anchor
with `os.path.dirname(__file__)` in a module — that broke ten files once and the
joins landed a level too deep.

**Config, not versions.** Experiments are switchboard entries, not new files or
renamed modules. There is no `arena_v5.py`.

**One source of truth.** This repo. The droplet is a deploy target, never a
place to edit. Anything hand-edited there is destroyed by the next
`rsync --delete` and exists nowhere else.

**Safety is layered.** `master.armed`, `master.kill`, and the `--execute` flag
are independent gates; `guard.py` trips the kill switch unattended on loss floor
or deadline.
