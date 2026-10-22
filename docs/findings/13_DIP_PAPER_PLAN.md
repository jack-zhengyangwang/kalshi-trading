# Plan 13: Dip catcher, paper-traded on live Kalshi data

**Status: rules fixed 2026-10-22, before the bot runs.** Jack's idea: prices overshoot when
sellers rush in (news, a goal), then partly come back. Rest a buy a little below the price,
and when it fills, rest a sell 5¢ higher.

**No real money.** The bot reads Kalshi's live order book and trade feed (WebSocket `ticker` +
`trade` channels). A simulated order counts as filled only when the real market trades through
it. Nothing is armed. The switchboard is not touched.

## Games

- Europa League (`KXUELGAME`) and Conference League (`KXUECLGAME`) games on **Thu 2026-10-22**: 18 + 18 games, 108 legs. Reported per competition and together.
- Kickoff = Kalshi `occurrence_datetime` − 3h (Finding 03: median exactly 3.00h).

## The rules (one contract per leg at a time)

| # | Rule | Value |
|---|---|---|
| 1 | **Reference price** | Last trade on the leg (from the trade feed; from REST `last_price` at start) |
| 2 | **Resting buy** | YES at **reference − 2¢**, never at or above the ask (we never take) |
| 3 | **Re-peg** | Every **60 s** while unfilled, move the buy to the new reference − 2¢. A slow drift never fills; only a fall faster than a minute does: **that's the dip** |
| 4 | **Buy fill** | A trade prints **below** our price after we posted, or the best ask drops **to or below** our price. Fill at our price. (A trade exactly at our price doesn't count: orders already there fill first) |
| 5 | **Resting sell** | Immediately after the fill, sell at **fill + 5¢** (in Kalshi terms, buy NO at 100 − that) |
| 6 | **Sell fill** | A trade prints **above** our sell price after it was posted, or the best bid rises **to or above** it |
| 7 | **No bounce** | An unsold position is **held to settlement** ($1 or $0) |
| 8 | **Re-arm** | After a completed round trip the leg re-arms at the next re-peg. At most **3 round trips per leg** |
| 9 | **Where** | Only legs whose reference is **5–95¢**. No new buys after **kickoff + 90 min** (late-game prices are close to settled) |
| 10 | **Fees** | Both competitions are `quadratic`: **no maker fee**. Holding to settlement pays nothing |

**Phases:** a fill before kickoff is **pre-game**; from kickoff on it's **in-play**. Reported separately.

## What gets measured

- **Per round trip:** buy and sell price, minutes to bounce, phase. Result: **+5¢** if the sell filled, else settlement minus buy price.
- **Per leg and overall:** fills, completed round trips, unsold positions, net ¢, by phase and by competition.
- **Bounce rate:** share of buy fills whose sell filled. Break-even depends on how often unsold positions lose.
- **Everything raw:** every `ticker` and `trade` message is saved (`logs/dip_raw_*.jsonl.gz`), so other offsets, targets and re-peg speeds can be replayed afterwards without re-running live.

## What one night can and can't show

- **It can show** whether the mechanics work: do dips happen, do our orders fill, do bounces come, and how fast.
- **It can't prove profit.** 108 legs over one evening is a small sample, and one big upset can swing the total. A result is a reason to keep collecting (the recorder makes that cheap), not a verdict.
- **Paper limits:** queue position is approximated by the cautious fill rule, and our order doesn't move the market. Real fills could be fewer.
