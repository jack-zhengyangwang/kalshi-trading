# Adding a portfolio manager or a strategy

The firm is config. Adding a competitor should never mean writing a new module,
and it never means a `strategy_v2.py`.

---

## The three stages

```
STRATEGY        config/pms/*.json  +  strategies/agents/*.json
   ↓
BACKTEST        python3 -m wc.firm.run          Kalshi's archive
   ↓
FORWARD TEST    the arena                        live book, paper money
   ↓
PROMOTION       docs/backtester/07_PROMOTION.md  gates, then real money
```

Nothing skips a stage, including a PM that looks obviously good.

---

## 1. A new strategy for an existing PM

A strategy is a DSL spec: JSON, schema-validated, no code. Write one by hand, or
have the model write it:

```bash
python3 -m wc.backtest.author \
  "buy the favourite when our view says it is 8 percent underpriced, liquid markets only" \
  -o strategies/agents/my-idea.json
```

The author prints every assumption it had to make before writing the file, and
the validator is the gate — an invented signal name fails, and missing caps
fail. Read the diff before you commit it; nothing auto-promotes.

Then add it to a PM's desk:

```json
{
  "agents": [
    {"name": "edge-taker", "spec": "strategies/agents/edge-taker.json"},
    {"name": "my-idea",    "spec": "strategies/agents/my-idea.json"}
  ]
}
```

Capital splits automatically. Its P&L is reported separately, so you can see
whether the desk's profit is broad or one lucky agent.

---

## 2. A new portfolio manager

One file, `config/pms/<name>.json`:

```json
{
  "name": "my-desk",
  "description": "What this PM believes, and why that might be true.",
  "view": {"view": "elo", "params": {"k": 32.0, "home_bonus": 40.0}},
  "bankroll": 1000.0,
  "allocation": "equal",
  "agents": [
    {"name": "edge-taker", "spec": "strategies/agents/edge-taker.json"}
  ],
  "caps": {"daily_spend_dollars": 120.0, "total_exposure_dollars": 500.0}
}
```

It competes on the next run. No registration, no code.

| field | meaning |
|---|---|
| `view` | what it believes. `market`, `elo`, `momentum`, `mean_reversion`, `structural` |
| `bankroll` | its own capital. Its track record is its own P&L |
| `allocation` | `equal`, or `weighted` with a `weight` on each agent |
| `agents` | one or more DSL specs sharing its book |
| `caps` | firm-level limits binding ACROSS its agents |

**`description` is not decoration.** When a PM loses money in three months, the
only question that matters is what you believed when you created it, and memory
supplies a flattering answer if the file does not supply the real one.

---

## 3. A new view

A view is the one thing that is code, because it is a genuinely new way of
forming a belief. Add a class to `wc/firm/views.py`:

```python
@register
class MyView(View):
    name = "my_view"

    def price(self, bars):
        return {(b["ticker"], b["ts"]): p for b in bars ...}
```

Three rules, and they are not negotiable:

1. **No lookahead.** Bars arrive in chronological order. Use only what precedes
   each bar. If your view learns, it learns from matches already settled — see
   `EloView`, which updates a rating only after a match resolves.
2. **Own your state.** Do not read a shared fitted model. `models/team_elo.json`
   is a present-day snapshot; using it means your PM knows who won. If you must,
   set `lookahead = True` on the class so reports mark it ranking-only.
3. **Absent, not guessed.** A bar you cannot price is simply missing from the
   dict. The interpreter's fail-closed rule turns any condition on `model_prob`
   or `edge` into False, so a market you cannot price is one you do not trade.

Then add a test in `tests/test_firm_views.py` proving the no-lookahead property
holds. There is an existing one to copy.

---

## The two databases

The firm keeps knowledge apart from money, deliberately.

| | holds | rebuilt by |
|---|---|---|
| `data/market_history.db` | Kalshi candles, prices, outcomes | `wc.backtest.backfill` |
| `data/soccer.db` | Elo, form, head-to-head, home/away rates, news | `wc.firm.sources.derived` + `espn` |

They are separate files because they answer different questions and carry
different lookahead risks. One file invites a join that quietly reads a fact
from after the bar it is pricing.

**Everything in `soccer.db` from `derived.py` is point-in-time**: it is rebuilt
by walking finished matches forward, so a fact dated day D reflects only matches
finished by day D. No cheat needed, and a backtest using it is genuine
promotion evidence.

`espn.py` is the exception — ESPN serves the current world, so news and
bookmaker odds accrue forward only.

## Running the firm

```bash
python3 -m wc.backtest.backfill --days 60          # 1. financial history
python3 -m wc.firm.sources.derived                 # 2. soccer knowledge
python3 -m wc.firm.run                             # 3. every PM competes
python3 -m wc.backtest.dashboard --results 'data/backtests/pm-*.json'
```

`--pm <name>` runs one. `--assume-fills` answers "is there an edge at all"
before worrying about liquidity.

## How a PM learns

A PM walks the archive forward and keeps a **journal** — its own record, per
league and per price band, written only when a bet settles.

- A league it has never traded gets a normal stake. Inexperience is not a
  reason to size down; it is a reason to find out.
- After ~8 settled bets it has earned an opinion about its own reliability
  there, and a Brier worse than a coin flip scales the stake toward a floor.
- A good run never scales it **up**. That would be a martingale.

This is why a desk can enter a thin league it knows nothing about: it starts
blind, logs what happens, and by day 30 has a record it earned. The knowledge
base tells it about the world; the journal tells it about itself.

Set `"learns": false` on a PM to turn it off — useful for a control.

## A new PM joins

1. Write `config/pms/<name>.json`.
2. `python3 -m wc.firm.run --pm <name>` — it completes its own backtest over
   the same archive, on the same fee and latency model, with the same
   knowledge every other desk had.
3. Read it against the leaderboard, and against `baseline-market`.
4. If it clears gate 1 in [07_PROMOTION.md](backtester/07_PROMOTION.md), it
   joins the arena for forward testing.

No registration and no code. The comparison is fair because every desk is
scored on the same bars with the same costs — the only thing that differs is
what it believes.

---

## Why independence matters more than it looks

Before the firm existed, every arena strategist read `p_fair` from one shared
per-league brain and differed only in risk parameters. Sixteen agents, one
opinion. When the brain was wrong about a match they all lost together.

That makes "promote the best performer" nearly meaningless: best-of-16
correlated agents is roughly best-of-1, and the ranking measures
risk-parameter luck rather than skill. The window-A/window-B selection test in
[07_PROMOTION.md](backtester/07_PROMOTION.md) needs genuine diversity to select
across, and independent views are what supply it.

So the rule that governs this whole directory:

> **Share observations. Never share beliefs.**

Two PMs read the same market — there is one market, and no choice about that.
The moment they share a fitted model, the firm is one opinion wearing several
hats, and every number the leaderboard produces is worth less than it looks.

---

## The baseline

`config/pms/baseline-market.json` believes the book is right, so its edge is
exactly zero and it can only lose the spread and fees. Keep it. Promotion gate
2.5 measures against it, and a PM that cannot beat it has variance, not edge.
