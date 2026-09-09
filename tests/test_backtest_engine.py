"""Engine validation.

The engine is what every future decision rests on, so it is tested harder than
anything else. The five cases from docs/backtester/02_ENGINE.md section 6:

  1. Known-answer   — hand-computed P&L
  2. Zero-edge      — random trading must lose roughly the fee take
  3. Perfect-foresight — knowing the outcome must be hugely profitable
  4. No-lookahead   — a cheating strategy cannot see the result
  5. Cap enforcement — portfolio caps bind across concurrent positions
"""
import pytest

from wc.backtest import engine, interpret


def bar(ticker="M1", ts=0, bid=9, ask=11, close=10, volume=10_000,
        oi=1000, close_time=10 * 86400, result=None, series="S"):
    """A candlestick row shaped like data.load_bars returns."""
    return {"ticker": ticker, "series": series, "ts": ts,
            "yes_bid": bid, "yes_ask": ask, "open": close, "high": close,
            "low": close, "close": close, "volume": volume, "open_interest": oi,
            "close_time": close_time, "status": "active", "result": result}


def spec(**over):
    s = {
        "name": "t", "side": "yes",
        "universe": {"min_volume": 1},
        "entry": {"all": [{"signal": "price", "op": "lt", "value": 0.50}]},
        "sizing": {"method": "fixed", "dollars": 110.0, "max_bet_dollars": 110.0},
        "exit": {"any": [{"signal": "hold_to_settlement", "op": "eq", "value": True}]},
        "caps": {"daily_spend_dollars": 1e6, "per_market_dollars": 1e6,
                 "total_exposure_dollars": 1e6},
    }
    s.update(over)
    return s


NO_COST = engine.Costs(fee_rate=0.0, slippage_cents=0.0, max_volume_share=1.0)

# Fills on the deciding bar. A DIAGNOSTIC, never a result — see
# Costs.fill_delay_bars. Used only where a test isolates something other than
# latency and a second bar would add noise rather than realism.
IMMEDIATE = engine.Costs(fee_rate=0.0, slippage_cents=0.0, max_volume_share=1.0,
                         fill_delay_bars=0)


def with_fill_bar(bars, gap=60):
    """Give a scenario a bar to fill on.

    The engine fills on the bar AFTER the one whose conditions fired, so a
    two-bar scenario (decide, settle) never trades. Duplicating the entry bar
    `gap` seconds later keeps the fill price identical, so hand-computed
    arithmetic is unchanged while the realistic path is what gets exercised.
    """
    fill = dict(bars[0])
    fill["ts"] = bars[0]["ts"] + gap
    return [bars[0], fill] + list(bars[1:])


# ── 1. known answer ───────────────────────────────────────────────────────────

def test_known_answer_settlement_win():
    """Buy 1000 contracts at 11c (ask, no slippage) = $110. Market settles YES,
    paying $1 each = $1000. Gross P&L = 1000 * (1.00 - 0.11) = $890."""
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)

    assert len(trades) == 1
    t = trades[0]
    assert t["contracts"] == 1000
    assert t["entry_price"] == pytest.approx(0.11)
    assert t["gross_pnl"] == pytest.approx(890.0)
    assert t["net_pnl"] == pytest.approx(890.0)
    assert t["outcome"] == 1


def test_known_answer_settlement_loss():
    """Same entry, settles NO: lose the full $110 stake."""
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="no", close_time=86400)])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["gross_pnl"] == pytest.approx(-110.0)
    assert trades[0]["outcome"] == 0


def test_fees_reported_separately_and_reduce_net():
    costs = engine.Costs(fee_rate=0.07, slippage_cents=0.0, max_volume_share=1.0)
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=costs)
    t = trades[0]
    assert t["fees"] > 0
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"])


# ── 2. zero edge ──────────────────────────────────────────────────────────────

def test_zero_edge_loses_approximately_the_fees():
    """A coin-flip strategy on 50c markets must lose about the fee take. If
    random trading looks profitable, the cost model is wrong."""
    costs = engine.Costs(fee_rate=0.07, slippage_cents=0.0, max_volume_share=1.0)
    bars, ts = [], 0
    for i in range(200):
        tk = f"M{i}"
        bars.append(bar(ticker=tk, ts=ts, bid=49, ask=51, close=50,
                        close_time=ts + 86400))
        bars.append(bar(ticker=tk, ts=ts + 60, bid=49, ask=51, close=50,
                        close_time=ts + 86400))                    # fill bar
        bars.append(bar(ticker=tk, ts=ts + 86400, bid=49, ask=51, close=50,
                        close_time=ts + 86400,
                        result="yes" if i % 2 == 0 else "no"))
        ts += 2 * 86400

    s = spec(entry={"all": [{"signal": "price", "op": "lt", "value": 0.99}]})
    s["caps"] = {"daily_spend_dollars": 1e6, "per_market_dollars": 1e6,
                 "total_exposure_dollars": 1e6}
    trades, _ = engine.run(s, bars, starting_bankroll=100_000.0, costs=costs)

    assert len(trades) == 200
    net = sum(t["net_pnl"] for t in trades)
    gross = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)

    assert fees > 0
    assert gross < 0, "buying at the ask against a fair coin must lose the spread"
    assert net < gross, "fees must make it worse still"
    assert net < 0, "a zero-edge strategy must never look profitable"


# ── 3. perfect foresight ──────────────────────────────────────────────────────

def test_perfect_foresight_is_hugely_profitable():
    """Sanity check on the settlement path: entering only winners must profit.
    If it does not, settlement logic is broken."""
    bars = []
    for i in range(20):
        tk = f"W{i}"
        bars.append(bar(ticker=tk, ts=i * 86400, close_time=(i + 1) * 86400))
        bars.append(bar(ticker=tk, ts=i * 86400 + 60,               # fill bar
                        close_time=(i + 1) * 86400))
        bars.append(bar(ticker=tk, ts=(i + 1) * 86400,
                        close_time=(i + 1) * 86400, result="yes"))
    trades, _ = engine.run(spec(), bars, starting_bankroll=10_000.0, costs=NO_COST)
    assert sum(t["net_pnl"] for t in trades) > 0
    assert all(t["outcome"] == 1 for t in trades)


# ── 4. no lookahead ───────────────────────────────────────────────────────────

def test_result_is_unreadable_before_close():
    v = engine.BarView(bar(ts=0, result="yes", close_time=86400))
    assert v.settled is False
    assert v.result is None, "outcome must not be visible before close_time"


def test_result_readable_only_after_close():
    v = engine.BarView(bar(ts=86400, result="yes", close_time=86400))
    assert v.settled is True
    assert v.result == "yes"


def test_context_never_contains_the_outcome():
    """The interpreter is handed ctx. If the outcome leaked into it, every
    backtest would be fiction."""
    ctx = engine.BarView(bar(ts=0, result="yes")).to_ctx("yes")
    assert "result" not in ctx
    assert "outcome" not in ctx


def test_trailing_features_use_history_only():
    hist = [{"price": p, "open_interest": oi}
            for p, oi in ((0.10, 100), (0.12, 110), (0.14, 120))]
    v = engine.BarView(bar(ts=100, bid=15, ask=17), history=hist)
    ctx = v.to_ctx("yes")
    assert ctx["price_change_pct"] == pytest.approx((0.16 - 0.10) / 0.10)


def test_oi_change_pct_is_computed_not_merely_declared():
    """It was in the vocabulary but never computed, so a strategy gating on it
    validated, ran, placed nothing, and gave no reason why."""
    hist = [{"price": 0.10, "open_interest": 100},
            {"price": 0.12, "open_interest": 110}]
    row = bar(ts=100, bid=15, ask=17)
    row["open_interest"] = 150
    v = engine.BarView(row, history=hist)
    assert v.to_ctx("yes")["oi_change_pct"] == pytest.approx(0.5)


def test_trailing_features_absent_without_history():
    """No history means no derived feature — never a fabricated zero, which a
    condition could accidentally satisfy."""
    ctx = engine.BarView(bar(ts=100, bid=15, ask=17)).to_ctx("yes")
    assert "oi_change_pct" not in ctx and "volatility" not in ctx


def test_volume_24h_sums_the_trailing_day_not_one_bar():
    bars = [bar(ticker="M", ts=t, close_time=90 * 86400) for t in
            (0, 3600, 7200, 200_000)]
    for b in bars:
        b["volume"] = 100
    seen = {}
    s = spec()
    s["entry"] = {"all": [{"signal": "volume_24h", "op": "gt", "value": 10 ** 9}]}

    real_evaluate = interpret.evaluate

    def spy(cond, ctx):
        if "ticker" in ctx:
            seen[ctx.get("price") and len(seen)] = ctx.get("volume_24h")
        return real_evaluate(cond, ctx)

    interpret.evaluate = spy
    try:
        engine.run(s, bars, starting_bankroll=1000.0, costs=NO_COST)
    finally:
        interpret.evaluate = real_evaluate
    vols = [v for v in seen.values() if v is not None]
    assert max(vols) == 300          # three bars inside 24h
    assert min(vols) == 100          # the bar >24h later starts a fresh window


# ── 5. cap enforcement ────────────────────────────────────────────────────────

def test_daily_cap_binds_across_concurrent_markets():
    """The reason bars are replayed chronologically across ALL markets: caps
    can only bind if the engine holds several positions at once."""
    bars = [bar(ticker=f"M{i}", ts=0, close_time=10 * 86400) for i in range(20)]
    s = spec()
    s["sizing"] = {"method": "fixed", "dollars": 10.0, "max_bet_dollars": 10.0}
    s["caps"]["daily_spend_dollars"] = 25.0        # only 2 x $10 fit
    trades, rej = engine.run(s, bars, starting_bankroll=10_000.0, costs=NO_COST)
    assert rej.get("daily_cap", 0) > 0


def test_total_exposure_cap_binds():
    bars = [bar(ticker=f"M{i}", ts=i, close_time=10 * 86400) for i in range(20)]
    s = spec()
    s["sizing"] = {"method": "fixed", "dollars": 10.0, "max_bet_dollars": 10.0}
    s["caps"]["total_exposure_dollars"] = 30.0
    _, rej = engine.run(s, bars, starting_bankroll=10_000.0, costs=NO_COST)
    assert rej.get("total_exposure_cap", 0) > 0


def test_max_concurrent_positions_binds():
    bars = [bar(ticker=f"M{i}", ts=i, close_time=10 * 86400) for i in range(20)]
    s = spec()
    s["sizing"]["max_concurrent_positions"] = 3
    _, rej = engine.run(s, bars, starting_bankroll=10_000.0, costs=NO_COST)
    assert rej.get("max_concurrent_positions", 0) > 0


def test_per_market_cap_limits_stake():
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    s = spec()
    s["caps"]["per_market_dollars"] = 2.0
    trades, _ = engine.run(s, bars, starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["cost"] <= 2.0


def test_volume_cap_limits_contracts():
    """Never fill more than a share of the bar's traded volume."""
    costs = engine.Costs(fee_rate=0.0, slippage_cents=0.0, max_volume_share=0.10)
    bars = with_fill_bar([bar(ts=0, volume=50),
                          bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=costs)
    assert trades[0]["contracts"] <= 5


# ── fills ─────────────────────────────────────────────────────────────────────

def test_buy_fills_at_ask_not_close():
    """Filling at close assumes size existed at the midpoint — the most common
    way a backtest flatters itself."""
    bars = with_fill_bar([bar(ts=0, bid=9, ask=11, close=10),
                          bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["entry_price"] == pytest.approx(0.11)


def test_slippage_worsens_entry():
    costs = engine.Costs(fee_rate=0.0, slippage_cents=2.0, max_volume_share=1.0)
    bars = with_fill_bar([bar(ts=0), bar(ts=86400, result="yes", close_time=86400)])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=costs)
    assert trades[0]["entry_price"] == pytest.approx(0.13)


def test_no_side_buys_the_complement():
    bars = with_fill_bar([bar(ts=0, bid=9, ask=11),
                          bar(ts=86400, result="no", close_time=86400)])
    s = spec(side="no", entry={"all": [{"signal": "price", "op": "lt", "value": 0.99}]})
    trades, _ = engine.run(s, bars, starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["entry_price"] == pytest.approx(0.91)   # 100 - bid
    assert trades[0]["outcome"] == 1


def test_rejection_reasons_are_counted():
    """An unexecutable strategy must be visibly unexecutable, not silently
    trade-free."""
    # volume 0 fails the universe filter
    _, rej = engine.run(spec(), [bar(ts=0, volume=0)],
                        starting_bankroll=1000.0, costs=NO_COST)
    assert rej.get("universe_filter", 0) > 0

    # price above the entry threshold fails on signal, not universe
    _, rej2 = engine.run(spec(), [bar(ts=0, bid=89, ask=91, close=90)],
                         starting_bankroll=1000.0, costs=NO_COST)
    assert rej2.get("entry_conditions", 0) > 0


# ── voids: settlement's third outcome ─────────────────────────────────────────

def void_bar(**kw):
    row = bar(**kw)
    row["result"] = "void"
    row["settlement_value"] = kw.pop("value", 0.33)
    return row


def test_a_void_pays_its_settlement_value_not_zero():
    """A cancelled game returns most of the stake. Scoring it as a loss would
    understate every strategy by roughly the void rate — ~13% on soccer."""
    b = bar(ts=86400, close_time=86400)
    b["result"] = "void"
    b["settlement_value"] = 0.40
    bars = with_fill_bar([bar(ts=0), b])
    trades, _ = engine.run(spec(), bars, starting_bankroll=1000.0, costs=NO_COST)
    t = trades[0]
    assert t["voided"] is True
    assert t["exit_price"] == pytest.approx(0.40)
    # entered at the 11c ask, settled at 40c
    assert t["gross_pnl"] == pytest.approx((0.40 - 0.11) * t["contracts"])


def test_a_void_on_the_no_side_pays_the_complement():
    b = bar(ts=86400, close_time=86400, bid=9, ask=11)
    b["result"] = "void"
    b["settlement_value"] = 0.40
    bars = with_fill_bar([bar(ts=0, bid=9, ask=11), b])
    s = spec(side="no", entry={"all": [{"signal": "price", "op": "lt", "value": 0.99}]})
    trades, _ = engine.run(s, bars, starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["exit_price"] == pytest.approx(0.60)


def test_a_void_has_no_binary_outcome():
    """It must not enter Brier, log loss, or calibration: a forecast is not
    wrong because the match was called off."""
    b = bar(ts=86400, close_time=86400)
    b["result"] = "void"
    b["settlement_value"] = 0.33
    trades, _ = engine.run(spec(), with_fill_bar([bar(ts=0), b]),
                           starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["outcome"] is None


def test_a_void_without_a_value_is_treated_as_flat():
    """Assume no move rather than invent a payout."""
    b = bar(ts=86400, close_time=86400)
    b["result"] = "void"
    b["settlement_value"] = None
    trades, _ = engine.run(spec(), with_fill_bar([bar(ts=0), b]),
                           starting_bankroll=1000.0, costs=NO_COST)
    assert trades[0]["gross_pnl"] == pytest.approx(0.0)


def test_settlement_value_is_unreadable_before_close():
    row = bar(ts=0, close_time=86400)
    row["result"] = "void"
    row["settlement_value"] = 0.5
    v = engine.BarView(row)
    assert v.settlement_value is None, "settlement value is as revealing as result"
