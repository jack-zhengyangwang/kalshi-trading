# Adding a strategy agent

Agents are strategy parameter sets evolved by the GA in `wc/strategy.py`. They
are **configuration, not code** — you do not add a file or a class to add an
agent.

## Add one

Edit `config/switchboard_v3.json`. Each category's `slots` list holds its
agents:

```json
"slots": {
  "game_lines": {
    "mode": "paper",
    "agents": [
      {"name": "conservative", "min_edge": 0.08, "kelly_frac": 0.25, "max_bet": 5.0}
    ]
  }
}
```

| Field | Meaning |
|---|---|
| `name` | Unique within the category; used in logs and P&L attribution. |
| `min_edge` | Model probability minus market price required to bid. |
| `kelly_frac` | Fraction of full Kelly. Below 0.5 in practice. |
| `max_bet` | Hard per-order dollar cap, applied after Kelly. |

`mode` is `off`, `paper`, or `real`. A new agent starts at `paper`.

## Promote it

1. Run it in `paper` until it has enough graded predictions to mean something.
2. Compare against the incumbents in `data/predictions_graded.jsonl`.
3. Only then set the category `mode` to `real` — and only with
   `master.per_game_cap_dollars` and `daily_cap_dollars` set.

The GA mutates surviving agents between cycles, so a manually added agent is a
seed, not a fixture — it can be bred out.

## Don't

- Don't add `agent_v2.py`. Parameters go in config.
- Don't promote on paper P&L alone; check calibration on graded predictions.
- Don't edit agents on the droplet. Edit here, deploy, verify.
