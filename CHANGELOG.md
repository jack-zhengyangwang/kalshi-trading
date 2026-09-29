# Changelog

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
