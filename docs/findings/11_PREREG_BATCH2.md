# Pre-registration: Batch 2 — does "dumb money" leave a mark on the price?

**Status: fixed 2026-10-18, before any Batch 2 run.** Jack's request: "draft the four not really
tested hypothesis and use our current data to test that". The git commit of this file is its
timestamp. Changing a rule after its test has run means a new pre-registration, not an edit.

**The idea (Jack):** we can't win on speed or private information, but we may win on common
knowledge if enough casual bettors make predictable mistakes. Each test below names one such
mistake, the bet that profits from it, and holds the bet to settlement.

**Why it might fail:** professional market makers fill casual bettors at a fair price, so the
casual money is lost through the spread and fees **without moving the price** (Finding 01: price
≈ win rate overall). These tests look for pockets where the price does move.

## Common to every test

| Item | Fixed value |
|---|---|
| Data | **Droplet snapshot** `history_snapshot.db` (Phase 2 run, 2026-09-29), copied read-only to `data/droplet/`, with the droplet's ESPN cache |
| Markets | Kalshi soccer `*GAME` legs, result yes/no (voids excluded), game has an ESPN kickoff (`kickoffs.status` ∈ agree / disagree / espn_only) |
| Prices | Hourly bars. A quote counts only if bid > 0, ask > 0 and spread ≤ 10¢ |
| Entry | Last valid bar at or before **kickoff − entry_h** (at most 6h stale) |
| Bet | 1 contract per signal, **taker at the ask**, held to settlement. YES costs the YES ask; NO costs 100 − YES bid |
| Taker fee | ceil(0.07 · p · (1−p)) per contract, in cents |
| **Develop** | Games closing through **2026-08-31**. Settings may be chosen, only within the listed ranges |
| **Test** | Games closing **2026-09-01 → end of the snapshot**. Run **once**, with the frozen settings |
| Selection on develop | Highest mean net ¢ per contract **among settings with ≥ 50 bets**; ties go to more bets. No eligible setting → the test is reported as "too few bets" and not run |
| Error bars | Bootstrap resampling **whole games**, 2,000 draws, seed 0 |
| **Pass** | Test: **≥ 50 bets and the 99% interval of net ¢ per contract above 0** (4 tests, so 99%) |
| Also reported (not judged) | **Bias before costs:** won − mid in ¢, with a 99% interval. This shows whether casual money moves the price at all, even when fees eat the profit. Random-entry benchmark (same games and entry bar, random leg and side) |

Caveat: the test period overlaps the period of Phase 1/2 (which looked at overall accuracy, not
these rules) and Batch 1's test (Sep 1–14, different rules). No rule below was looked at on any data.

---

## H1 Big-club bias

- **Mistake:** fans overpay for famous clubs.
- **Clubs (fixed):** Real Madrid, Barcelona, Manchester City, Manchester United, Liverpool, Arsenal, Chelsea, Bayern Munich, PSG, Juventus, Inter, Milan.
  - Matched by exact Kalshi team name. Spelling variants are listed **from team names only** before any run (e.g. "Paris Saint-Germain" = PSG). "Inter Toronto", "Inter Milano U23", "Paris FC" etc. are **not** big clubs.
- **Bet:** buy **NO** on the big club's leg.
- **Develop may choose:** entry_h ∈ {24, 6}.

## H2 Overreaction to the last result

- **Mistake:** fans overrate a team after a big win and underrate it after a big loss.
- **Last result:** the team's previous **completed** ESPN match, with kickoff ≥ 3h before our entry time and ≤ 14 days before this game. Teams are matched by ESPN team id through `kickoffs.espn_id` (honouring `swapped`). Matches decided after extra time or penalties count as margin 0.
- **Bet:** previous win by ≥ **D** goals → buy **NO** on the team's leg. Previous loss by ≥ D → buy **YES** on the team's leg. Both kinds pooled.
- **Develop may choose:** D ∈ {2, 3}, entry_h = 24.

## H3 Draws are underpriced

- **Mistake:** casual bettors back a team and rarely the draw.
- **Bet:** buy **YES** on the Tie leg.
- **Develop may choose:** entry_h ∈ {24, 6} × filter ∈ {all draws, draw mid ≥ 25¢ (close games)}.

## H4 Busy games: the public backs the favourite

- **Mistake:** in high-attention games, casual money piles onto the favourite.
- **Busy:** total contracts traded across the game's three legs from the first bar up to the entry time. The cut-off is a **number of contracts**, set as a percentile of develop games and frozen.
- **Favourite:** the home or away leg with the highest mid at entry (the Tie leg is never the favourite).
- **Bet:** in busy games, buy **NO** on the favourite.
- **Develop may choose:** busy = top {25%, 10%} of develop games by volume, entry_h = 24.
- **Also reported:** won − mid of the favourite in busy vs quiet games.

---

## How it will be run

- `wc/research/batch2.py`, reusing Batch 1's quote, fee and bootstrap code. Tests in `tests/test_research_batch2.py`.
- `python -m wc.research.batch2 develop` → writes `results/batch2_develop.json` and the chosen settings to `results/batch2_frozen.json`.
- `batch2_frozen.json` is committed, then `python -m wc.research.batch2 test` runs once. **`test` refuses to run until the frozen file is committed unchanged.**
- Results go in Finding 12 with one chart, in the same style as Finding 06.

## Power caveat

- About 1,900 bets are needed to detect a 1¢ edge. H1 and H2 will have far fewer, so **a fail means "not detected", not "not there"**. The bias-before-costs number says which direction the data leans.

## Added 2026-10-18, before any run (team-name check only)

- Cup series label legs `Reg Time: <team>` (about 2% of legs). Batch 2 strips the prefix and redoes home / away / tie, so those games aren't silently dropped. Batch 1's loader is unchanged.
- Big-club names on the laptop DB match the fixed list exactly. Look-alikes that are **not** big clubs: Inter Toronto, Inter Bratislava, Inter Milano U23, Juventus Next Gen, Liverpool M, Paris FC, Alcione Milano. The same check is re-run on the droplet snapshot before `develop`.

## Added 2026-10-18, before any run (droplet snapshot, team names and series only)

- Snapshot: 7,622 settled games, last close 2026-09-29.
- **H1 skips women's series** (`KXUCLWGAME`, `KXSERIEAWGAME`, `KXEREDIVISIEWGAME`, `KXFIFAWGAME`). The women's sides appear as "Bayern", "Inter Milano" and also "PSG". The hypothesis is about the famous men's club brands.
- More look-alikes that are **not** big clubs: Bayern (women), Inter Milano (women), Paris (Paris FC in Ligue 1), Real Madrid Castilla / C, Barcelona Atletic, Atletico Madrid B, Esp. Barcelona B, United of Manchester, Inter Turku, LAC Inter.
- Pre-season friendlies (`KXCLUBFGAME`) are included: same clubs, same fans.
