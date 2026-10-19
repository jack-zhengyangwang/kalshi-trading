# Changelog

## 2.0.0 — 2026-10-18: research report, public-data prediction chapter closed

Closes the question "can a model built from public match data beat Kalshi soccer prices?"
The answer on the evidence is no. See [docs/REPORT_v2.0.md](docs/REPORT_v2.0.md).

**Built**
- Point-in-time dataset: 23 features rebuilt in one forward pass, leakage-tested (`wc/research/dataset.py`)
- Table A (55,667 matches, Pinnacle closing odds) and Table B (Kalshi legs at 24h / 6h / 1h)
- Baseline models M0–M3 and the two gates with 99% bootstrap intervals (`wc/research/models.py`)
- Full-data droplet run on a DB snapshot (`scripts/run_phase2.sh`)

**Found**
- 0 of 4 models more accurate than Pinnacle closing or Kalshi; best Brier 0.2027 vs 0.1981
- 0 of 4 add information next to the price (Gate 2)
- 0 of 12 league-tier × entry-time groups pass on 1,363 Kalshi games
- The price can't be ignored: stakes must follow the edge (Finding 07)

**Left open:** profit on selected bets (Gate 3) was never run; non-public information; other market types.

## 1.0.0 — 2026-09-28: research report, trading-mechanics chapter closed

The first versioned release. It closes the question "can we profit on Kalshi soccer through how
we trade?" The answer on the evidence is no. See [docs/REPORT_v1.0.md](docs/REPORT_v1.0.md).

**Built**
- Walk-forward runner, LLM desk managers (OpenRouter) and a live dashboard (`wc/firm/`)
- Engine fixes: settlement during a walk; stranded stakes released
- Kickoff dataset, with ESPN as the truth and Kalshi as the check (`wc/research/kickoff.py`)
- Pre-registered strategy test runner with a freeze guard (`wc/research/batch1.py`)
- Chart scripts for every finding (`scripts/`)

**Found**
- Kalshi soccer prices are accurate probabilities; every trading desk lost money
- Prices move about 1.5¢ before kickoff against about 5.5¢ taker round-trip cost
- Batch 1: 0 of 4 pre-registered rule strategies passed
- Maker orders save about 3.5¢ per bet; 85 of 91 soccer series charge makers no fee

**Next:** v2, a prediction model held to settlement ([docs/NEXT_CHAPTER_v2.md](docs/NEXT_CHAPTER_v2.md)).
