# Finding 02: Is 40–60¢ mispriced?

**Data:** every settled Kalshi soccer winner market in the local store (~3,260), not only the
trades the desks chose. Price = last hourly bid/ask 24h, 6h or 2h before close.
**Charts:** `venv/bin/python scripts/finding02_midrange.py`.

![Gap by price](img/05_midrange_gap.png)

- **40–45¢ wins more than its price at all three times:** +5.8, +7.1 and +4.8 points.
- **45–50¢ is not mispriced** (about 0 points), so the whole 40–60¢ range isn't either.
- Every bar crosses 0, and with 9 bins one looking lucky is expected.
- The dip at 50–60¢ in Finding 01 came from which bets the desks chose. Across all markets it isn't there.
- The gap looks the same for home, away and draw legs and across leagues. Nothing structural explains it yet.

![P&L after costs](img/06_midrange_pnl.png)

- Buying YES at 40–45¢, paying the ask plus the fee: **+2 to +4¢ per contract**, but the 95% interval runs from about −4¢ to +11¢.
- Widening the band to 40–55¢ or 40–60¢ shrinks the profit to about 0.
- Confirming a +2¢ edge takes **about 2,400 bets**. This store has 211.

## Next

1. Re-run on the droplet's full store (about 19,000 markets, roughly 5× the data).
2. If 40–45¢ holds there, forward paper-test it before it counts. It's a hypothesis, not a result.
