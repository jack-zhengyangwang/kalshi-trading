# Research findings — 2026-09-07

*Web survey of the Kalshi API, open-source Kalshi tooling, and the
prediction-market literature. Sources at the bottom.*

---

## 1. API layer — do not build it

Kalshi ships official SDKs: `kalshi_python_sync` and `kalshi_python_async`. The
old `kalshi-python` PyPI package is **deprecated**. All authenticate with an API
key and RSA-PSS request signing.

Strongest third-party options: `TexasCoding/kalshi-python-sdk` (107 of 108 REST
operations, generated from the OpenAPI spec, plus WebSocket) and `apty/kalshi-py`
(rebuilt daily from the spec).

**Decision: keep `wc/kalshi/`.** It does RSA-PSS auth and pulled 1,404 soccer
series against the live API on 2026-09-07. Migrate only if an endpoint is
missing. Kalshi's own docs warn the SDKs lag the API, so a working client is
not obviously worse than a generated one.

## 2. Historical data — the binding constraint

| Available | Not available |
|---|---|
| Candlesticks: OHLC, `yes_bid`, `yes_ask`, volume, open interest | **L2 order-book history** |
| Trade prints: price, size, taker side | |
| `/historical/*` for archived markets | |

Vendors sell order-book history (LycheeData 36GB+, DepthFeed, Allium). Until
then, fill modelling is candlestick-resolution and optimistic.

**Consequence:** start the forward collector immediately. Every day it is not
running is history we cannot recover.

Rate limits are tiered and token-costed per endpoint; `GET /account/api-limits`
reports the current tier.

## 3. Existing frameworks

| Project | License | Maturity | Verdict |
|---|---|---|---|
| `braedonsaunders/homerun` | ⚠️ **AGPL-3.0** | 176★, 2,458 commits | **Read only, never vendor.** Most complete — 25+ strategies, backtester, React dashboard, Cox-model fills. But AGPL is viral *over a network*: serving its dashboard obliges publishing our strategies. Fatal for a private edge |
| `Quentin-Piot/prediction-market-backtester` | MIT | 6★, 15 commits | **Port the execution model.** Spread, fees, slippage, latency; PnL, drawdown, turnover, fills, equity curves |
| `kapelame/kalshi-crypto-bot` | MIT | 7★, 4 commits | **Follow the pipeline shape.** collector -> quality_check -> train -> backtest -> paper -> live -> dashboard. Strategy logic intentionally empty |
| `Viprasol-Tech/kalshi-trading-bot` | — | — | Strategy engine, backtester, risk manager, dry-run default |

Note that `homerun` runs **AST analysis** on strategies before activating them —
because it accepts arbitrary Python. Our DSL removes the need for that check
entirely, which is independent evidence the problem is real.

## 4. What the literature says to trade

**Favorite–longshot bias — the most robust finding in the field.** Low-
probability contracts are systematically overpriced; favorites underpriced.

- Contracts under 10¢ **lose 60%+ on average**
- NO longshots beat YES longshots by up to **64 percentage points** across
  300,000+ contracts
- Mechanism is behavioural: a 5¢ contract reads as a lottery ticket, and
  entertainment-seeking demand pushes it above fair value

**Calibration depends on horizon** — and this is the actionable part:

| Horizon | Calibration slope | Reading |
|---|---|---|
| < 1 week (sports) | 0.90 – 1.10 | Near-efficient. Hard to beat |
| > 1 month | 1.74 | Strong bias. Where the edge is |

**So `days_to_resolution` is a first-class signal, not a filter.** It also
matches the "resolution-time-aware strategists" idea already in this repo's
v3 plan.

**Maker/taker economics.** Takers are systematically too optimistic about their
contracts (Whelan). Being the maker is itself an edge.

**Other viable directions:** cross-venue Kalshi↔Polymarket arbitrage; whale and
order-flow following — Kalshi hides trader identities but **trade sizes are
public via the trade API**.

## 5. Gamma exposure does not transfer

Worth stating plainly, because it was an explicit ask. GEX describes options
dealers delta-hedging a continuous underlying. **Binary event contracts have no
underlying to hedge, so there is no gamma.**

The real analogues, which are worth building:

- **Open-interest concentration** — where positioning is crowded
- **Order-book imbalance** — market-maker inventory skew
- **Pin risk near resolution** — behaviour as time-to-resolution goes to zero

Same instinct, different mechanism.

## 6. A caution on the sources

The strategy-content search surfaced a large number of SEO blog posts
("7 Proven Strategies", "Kalshi Easy Money") with no evidence behind them. The
findings in §4 are taken from the academic papers and QuantPedia, not from
those. This is the same evidence-quality discipline the repo already applies to
its own in-play results, and it applies doubly to strategies sourced from
video.

---

## Sources

**API & SDKs**
- https://docs.kalshi.com/sdks/overview
- https://github.com/TexasCoding/kalshi-python-sdk
- https://apty.github.io/kalshi-py/
- https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks

**Historical data**
- https://www.allium.so/blog/kalshi-historical-data-a-practical-guide/
- https://lycheedata.com/kalshi-historical-data
- https://depthfeed.com/resources/kalshi-api-guide

**Frameworks**
- https://github.com/braedonsaunders/homerun
- https://github.com/Quentin-Piot/prediction-market-backtester
- https://github.com/kapelame/kalshi-crypto-bot
- https://github.com/Viprasol-Tech/kalshi-trading-bot

**Research**
- https://quantpedia.com/systematic-edges-in-prediction-markets/
- https://arxiv.org/html/2602.19520v1 — Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics
- https://arxiv.org/html/2607.14430 — Prices, Probabilities, and Parlays: Systematic Bias in Sports Prediction Markets
- https://www.karlwhelan.com/Papers/Kalshi.pdf — Makers and Takers: The Economics of the Kalshi Prediction Market
- https://cepr.org/voxeu/columns/economics-kalshi-prediction-market

**Whale / order flow**
- https://www.crossodds.app/prediction-market-whale-tracking
- https://whaletracks.com/
