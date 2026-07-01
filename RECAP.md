# WorldCup Trading — Recap (2026-06-30)

## Where we are
- **Phase B built** (in the root code): dynamic `^KXWC` scanner (no hardcoded menu), brain v3
  (structural model + LLM, LLM fallback for novel types), prediction DB, resolution-time (`tau`)
  sizing, **unified pool** (super-category barrier collapsed → one population), **inverted-cull bug fixed**,
  `EVOLVE_EVERY=12`, `gx_name` lineage, causal in-play LLM.
- **No-LLM full retrain ran** (75 games, 6 generations, tape-fill). Real money is **DISARMED — nothing live.**

## Key results (no-LLM, in-sample)
- Top strategist: `late_scalp` **+$142** (bred up over 6 gens). Pool is all **generalists** (specialists culled).
- **Edges: SPREAD +$268 (69% win), WINNER +$226** are real. BTTS/totals modestly +. 
- **Pre-game +$31 (54%)** = the trustworthy number.
- **In-play +$460 BUT SUSPECT**: it's 167 held-to-settle bets @76% win = betting near-decided games,
  not scalping (exits ≈ $0) and not outliers. Do **not** trust as live alpha until validated forward.

## Findings worth remembering
- **Corners "0% win" = replay artifact**, not a broken model: replay has no historical corner feed →
  every corner bet decays and hits the 100%-dump STOP before it can settle. Corners are only testable
  **forward/live (in-play)** or via a **pre-game NB backtest we haven't run**. Thesis: corners are an
  under-modeled market = real edge potential (pursue it).
- **LLM-everything replay abandoned** — too slow / rate-limited (thousands of calls). LLM belongs **live**
  (price a few markets in real time), not batch-replaying history. No-LLM results are what we evaluate on.

## Next (pick up here)
1. **Phase C** — run the trained pool **forward/OOS on the knockouts** (the gate before any real money;
   esp. to see if in-play scalpers survive on games caught *before* they're decided).
2. **Pre-game corner backtest** — add a pre-game corner specialist, re-run replay → real read on corner edge.
3. **Promotion** (#32) — arm a diversified top-4 from the **PRE-GAME** board, not the in-play scalpers.
4. Deferred: #29 xG/live-stats into brain · #33 clean v2/v3 repo reorg.
5. GitHub: `group/` + `models/` copied into this clone — still need commit + push.

## Run
- Retrain (local, fast):  `python3 -c "import arena_v3 as a; a.simulate(None, use_llm=False, persist=True)"`
- Status: `python3 arena_v3.py --status`  ·  OOS gate: `python3 arena_v3.py --forward-report`
