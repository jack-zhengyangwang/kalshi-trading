"""The forward collector.

Every cycle the collector misses is history that can never be recovered, so
these tests are mostly about failure modes: a double-fired cron, one broken
series, a market that settles, a clock that moves.
"""
import pytest

from wc.backtest import collect, data


class FakeClient:
    """Stands in for KalshiClientV2. Only the four methods the collector uses."""

    def __init__(self, by_series=None, by_ticker=None, fail_on=()):
        self.by_series = by_series or {}
        self.by_ticker = by_ticker or {}
        self.fail_on = set(fail_on)
        self.series_calls = []
        self.ticker_calls = []

    def list_markets_by_series(self, series, status="open", page=200):
        self.series_calls.append((series, status))
        if series in self.fail_on:
            raise RuntimeError(f"boom: {series}")
        return self.by_series.get(series, [])

    def list_markets_by_tickers(self, tickers, page=200):
        self.ticker_calls.append(list(tickers))
        return [self.by_ticker[t] for t in tickers if t in self.by_ticker]

    @staticmethod
    def quote_cents(m):
        return m.get("yes_bid"), m.get("yes_ask")

    @staticmethod
    def liquidity(m):
        return {"volume": m.get("volume_fp", 0.0), "oi": m.get("open_interest_fp", 0.0),
                "ask_size": 0.0, "bid_size": 0.0}


def mkt(ticker, series="KXEPLGAME", bid=40, ask=44, volume=1000, oi=500,
        close_time="2026-09-10T14:00:00Z", status="active", result=None):
    return {"ticker": ticker, "series_ticker": series, "title": f"{ticker} title",
            "yes_bid": bid, "yes_ask": ask, "volume_fp": volume,
            "open_interest_fp": oi, "close_time": close_time,
            "status": status, "result": result}


@pytest.fixture
def con(tmp_path):
    return data.connect(str(tmp_path / "h.db"))


# ── timestamp bucketing ───────────────────────────────────────────────────────

def test_bucket_floors_to_the_interval():
    assert collect.bucket_ts(1_000_000_123, 300) == 999_999_900
    assert collect.bucket_ts(1_000_000_123, 300) % 300 == 0
    assert collect.bucket_ts(999_999_900, 300) == 999_999_900     # already aligned


def test_double_fired_cron_overwrites_instead_of_duplicating(con):
    """A previously observed droplet failure: cron fires twice seconds apart.
    Both writes must land in the same bucket, or the store gains phantom bars
    and every volume/gap statistic is wrong."""
    client = FakeClient({"KXEPLGAME": [mkt("KXEPLGAME-A")]})
    collect.run_once(con, client, ["KXEPLGAME"], now=1_000_000_100, bucket=300)
    collect.run_once(con, client, ["KXEPLGAME"], now=1_000_000_190, bucket=300)
    assert data.candle_count(con) == 1


def test_next_interval_writes_a_new_bar(con):
    client = FakeClient({"KXEPLGAME": [mkt("KXEPLGAME-A")]})
    collect.run_once(con, client, ["KXEPLGAME"], now=1_000_000_100, bucket=300)
    collect.run_once(con, client, ["KXEPLGAME"], now=1_000_000_500, bucket=300)
    assert data.candle_count(con) == 2


# ── snapshot shape ────────────────────────────────────────────────────────────

def test_snapshot_stores_mid_as_close_and_leaves_ohl_null(con):
    """A snapshot has no range. Fabricating open/high/low would invent price
    action the engine could then 'trade' against."""
    client = FakeClient({"S": [mkt("T1", bid=40, ask=44)]})
    collect.run_once(con, client, ["S"], now=1_000_000_000, bucket=300)
    row = con.execute("SELECT * FROM candles").fetchone()
    assert row["close"] == 42
    assert row["open"] is None and row["high"] is None and row["low"] is None
    assert row["yes_bid"] == 40 and row["yes_ask"] == 44


def test_close_time_is_stored_as_epoch_seconds(con):
    client = FakeClient({"S": [mkt("T1", close_time="2026-09-10T14:00:00Z")]})
    collect.run_once(con, client, ["S"], now=1_000_000_000, bucket=300)
    row = con.execute("SELECT close_time FROM markets").fetchone()
    assert row["close_time"] == 1789048800


def test_unparseable_close_time_does_not_drop_the_market(con):
    client = FakeClient({"S": [mkt("T1", close_time="not a date")]})
    collect.run_once(con, client, ["S"], now=1_000_000_000, bucket=300)
    assert data.market_count(con) == 1
    assert con.execute("SELECT close_time FROM markets").fetchone()["close_time"] is None


def test_empty_book_is_recorded_not_skipped(con):
    """Illiquid legs must still be recorded — that is what defeats survivorship
    bias. The engine rejects them at trade time as 'no_price'."""
    client = FakeClient({"S": [mkt("T1", bid=None, ask=None)]})
    collect.run_once(con, client, ["S"], now=1_000_000_000, bucket=300)
    assert data.candle_count(con) == 1
    assert con.execute("SELECT close FROM candles").fetchone()["close"] is None


def test_first_seen_survives_later_cycles(con):
    client = FakeClient({"S": [mkt("T1")]})
    collect.run_once(con, client, ["S"], now=1_000_000_000, bucket=300)
    collect.run_once(con, client, ["S"], now=1_000_900_000, bucket=300)
    row = con.execute("SELECT first_seen, last_seen FROM markets").fetchone()
    assert row["first_seen"] == 999_999_900
    assert row["last_seen"] > row["first_seen"]


# ── resilience ────────────────────────────────────────────────────────────────

def test_one_broken_series_does_not_kill_the_cycle(con):
    """A cycle that dies partway is a permanent hole in the history."""
    client = FakeClient(
        {"GOOD": [mkt("G1", series="GOOD")], "BAD": []}, fail_on={"BAD"})
    rep = collect.run_once(con, client, ["BAD", "GOOD"], now=1_000_000_000)
    assert data.candle_count(con) == 1
    assert len(rep["failures"]) == 1
    assert rep["failures"][0]["series"] == "BAD"


def test_dry_run_writes_nothing(con):
    client = FakeClient({"S": [mkt("T1")]})
    rep = collect.run_once(con, client, ["S"], now=1_000_000_000, dry_run=True)
    assert rep["candles_written"] == 1
    assert data.candle_count(con) == 0


# ── settlement sweep ──────────────────────────────────────────────────────────

PAST = "2001-09-09T01:00:00Z"          # epoch 999_997_200, before the test clock
FUTURE = "2026-09-10T14:00:00Z"


def test_settlement_writes_the_outcome(con):
    """Without this the store is prices with no truth, and PnL, Brier, and
    calibration are all uncomputable."""
    client = FakeClient(
        {"S": [mkt("T1", close_time=PAST)]},
        by_ticker={"T1": mkt("T1", close_time=PAST, status="settled", result="yes")})
    collect.run_once(con, client, ["S"], now=1_000_000_000)
    assert con.execute("SELECT result FROM markets").fetchone()["result"] == "yes"


def test_unsettled_market_is_left_alone(con):
    client = FakeClient({"S": [mkt("T1", close_time=PAST)]},
                        by_ticker={"T1": mkt("T1", close_time=PAST, status="active")})
    collect.run_once(con, client, ["S"], now=1_000_000_000)
    assert con.execute("SELECT result FROM markets").fetchone()["result"] is None


def test_still_open_markets_are_never_asked_about(con):
    """A market cannot settle before it closes. The unresolved set grows every
    cycle, so an unbounded sweep would eat more of the rate limit each day
    until it crowded out the snapshots themselves."""
    client = FakeClient({"S": [mkt("OPEN", close_time=FUTURE)]})
    collect.run_once(con, client, ["S"], now=1_000_000_000)
    assert client.ticker_calls == []


def test_settlement_backlog_is_capped_per_cycle(con):
    """A backlog must drain over several cycles rather than starve the
    snapshots, which cannot wait."""
    markets = [mkt(f"T{i}", close_time=PAST) for i in range(50)]
    client = FakeClient({"S": markets})
    collect.run_once(con, client, ["S"], now=1_000_000_000)
    client.ticker_calls.clear()
    collect.settle_open_markets(con, client, now=1_000_000_000,
                                batch=10, max_batches=2)
    assert sum(len(c) for c in client.ticker_calls) == 20


def test_settlement_checks_longest_closed_first(con):
    """Oldest first, so a backlog drains in the order most likely to resolve."""
    client = FakeClient({"S": [mkt("NEW", close_time="2001-09-09T00:59:00Z"),
                               mkt("OLD", close_time="1999-01-01T00:00:00Z")]})
    collect.run_once(con, client, ["S"], now=1_000_000_000)
    assert client.ticker_calls[0][0] == "OLD"


def test_resolved_markets_are_not_re_fetched(con):
    """A result never changes. Re-reading settled markets forever is pure API
    waste, and the API budget is the binding constraint on how much we collect."""
    client = FakeClient(
        {"S": [mkt("T1", close_time=PAST)]},
        by_ticker={"T1": mkt("T1", close_time=PAST, status="settled", result="no")})
    collect.run_once(con, client, ["S"], now=1_000_000_000)
    before = len(client.ticker_calls)
    collect.run_once(con, client, ["S"], now=1_000_000_400)
    assert len(client.ticker_calls) == before      # nothing left unresolved


def test_settlement_failure_does_not_lose_the_snapshot(con):
    class Flaky(FakeClient):
        def list_markets_by_tickers(self, tickers, page=200):
            raise RuntimeError("settle API down")

    client = Flaky({"S": [mkt("T1", close_time=PAST)]})
    rep = collect.run_once(con, client, ["S"], now=1_000_000_000)
    assert data.candle_count(con) == 1             # the price snapshot survived
    assert rep["newly_settled"] == 0


# ── the collected data is actually loadable by the engine ─────────────────────

def test_collected_rows_load_as_bars(con):
    client = FakeClient({"S": [mkt("T1"), mkt("T2")]})
    collect.run_once(con, client, ["S"], now=1_000_000_000, bucket=300)
    collect.run_once(con, client, ["S"], now=1_000_000_300, bucket=300)
    bars = data.load_bars(con, source="collector")
    assert len(bars) == 4
    assert [b["ts"] for b in bars] == sorted(b["ts"] for b in bars)
    assert bars[0]["series"] and bars[0]["close_time"]


# ── universe narrowing ────────────────────────────────────────────────────────

def test_suffix_narrowing_is_not_optional_in_practice(monkeypatch):
    """Kalshi lists ~1400 soccer series. Probing all of them takes longer than
    the cron interval, so cycles would overlap and stack forever."""
    import wc.scanner as scanner
    monkeypatch.setattr(scanner, "discover_soccer_series",
                        lambda c, refresh=False, ttl=3600:
                        ["KXEPLGAME", "KXEPLTOTAL", "KXUCLGAME", "KXUCLBTTS"])
    assert collect.soccer_series(None, suffixes=["GAME"]) == ["KXEPLGAME", "KXUCLGAME"]


def test_no_suffixes_returns_everything(monkeypatch):
    import wc.scanner as scanner
    monkeypatch.setattr(scanner, "discover_soccer_series",
                        lambda c, refresh=False, ttl=3600: ["B", "A"])
    assert collect.soccer_series(None, suffixes=None) == ["A", "B"]


def test_shipped_config_is_loadable_and_sane():
    cfg = collect.load_config()
    assert cfg["suffixes"], "an empty universe collects nothing"
    assert cfg["interval_seconds"] > 0


def test_series_label_comes_from_the_query_not_the_ticker(con):
    """Kalshi leaves series_ticker null on some endpoints; inferring it from the
    ticker prefix would mislabel any series whose naming breaks the pattern."""
    m = mkt("WEIRD_TICKER_NO_PREFIX", series=None)
    m["series_ticker"] = None
    client = FakeClient({"KXEPLGAME": [m]})
    collect.run_once(con, client, ["KXEPLGAME"], now=1_000_000_000)
    assert con.execute("SELECT series FROM markets").fetchone()["series"] == "KXEPLGAME"


# ── fixture extraction ────────────────────────────────────────────────────────

RULES = ("The following market refers to the Fulham vs Manchester United "
         "professional EPL soccer game originally scheduled for Sep 20, 2026.")


def test_fixture_is_extracted_from_the_rules_prose():
    """Kalshi exposes no home/away fields. The rules text is the only reliable
    source, and it disappears when a market closes — so it is resolved once, at
    collection time, and stored."""
    h, a = collect.parse_fixture({"rules_secondary": RULES})
    assert (h, a) == ("Fulham", "Manchester United")


def test_fixture_reads_the_primary_rules_too():
    h, a = collect.parse_fixture(
        {"rules_primary": "If Tie is the result of the Valencia vs Real Sociedad "
                          "professional soccer game, then Yes."})
    assert (h, a) == ("Valencia", "Real Sociedad")


def test_no_fixture_is_none_not_a_guess():
    assert collect.parse_fixture({"rules_primary": "Will it rain?"}) == (None, None)
    assert collect.parse_fixture({}) == (None, None)


def test_runaway_match_is_rejected():
    """A greedy match across a whole paragraph would store a sentence as a team
    name and then silently fail to resolve an Elo rating for it."""
    long_text = "refers to the " + ("x" * 200) + " vs " + ("y" * 200) + " professional"
    assert collect.parse_fixture({"rules_primary": long_text}) == (None, None)


def test_fixture_and_sub_title_are_persisted(con):
    m = mkt("KXEPLGAME-26SEP20FULMUN-FUL")
    m["rules_secondary"] = RULES
    m["yes_sub_title"] = "Fulham"
    m["event_ticker"] = "KXEPLGAME-26SEP20FULMUN"
    client = FakeClient({"KXEPLGAME": [m]})
    collect.run_once(con, client, ["KXEPLGAME"], now=1_000_000_000)
    r = con.execute("SELECT home, away, sub_title, event_ticker FROM markets").fetchone()
    assert (r["home"], r["away"]) == ("Fulham", "Manchester United")
    assert r["sub_title"] == "Fulham"
    assert r["event_ticker"] == "KXEPLGAME-26SEP20FULMUN"


def test_bars_carry_the_fixture_through_to_the_pricer(con):
    m = mkt("KXEPLGAME-26SEP20FULMUN-FUL")
    m["rules_secondary"] = RULES
    m["yes_sub_title"] = "Fulham"
    client = FakeClient({"KXEPLGAME": [m]})
    collect.run_once(con, client, ["KXEPLGAME"], now=1_000_000_000)
    bar = data.load_bars(con, source="collector")[0]
    assert bar["home"] == "Fulham" and bar["sub_title"] == "Fulham"


# ── voids: the third outcome ──────────────────────────────────────────────────

def settled(result, value, status="finalized"):
    return {"status": status, "result": result, "settlement_value_dollars": value}


def test_yes_and_no_carry_their_payout():
    assert collect.settlement_outcome(settled("yes", "1.0000")) == ("yes", 1.0)
    assert collect.settlement_outcome(settled("no", "0.0000")) == ("no", 0.0)


def test_scalar_is_recorded_as_a_void_with_its_fair_price():
    """~13% of settled soccer markets are cancelled or postponed games. Kalshi
    settles every leg at a fair price summing to 1.00 and reports
    result='scalar'. Skipping those re-sweeps them forever."""
    assert collect.settlement_outcome(settled("scalar", "0.5900")) == ("void", 0.59)


def test_a_void_without_a_payout_is_left_unresolved():
    """Recording it as void with an unknown payout would silently invent a
    P&L of zero."""
    assert collect.settlement_outcome(settled("scalar", "")) == (None, None)
    assert collect.settlement_outcome(settled("scalar", None)) == (None, None)


def test_an_unfinished_market_stays_in_the_queue():
    assert collect.settlement_outcome(
        {"status": "active", "result": "", "settlement_value_dollars": None}) == (None, None)


def test_void_is_persisted_and_stops_being_reswept(con):
    m = mkt("T1", close_time=PAST)
    client = FakeClient({"S": [m]}, by_ticker={
        "T1": dict(m, status="finalized", result="scalar",
                   settlement_value_dollars="0.3300")})
    collect.run_once(con, client, ["S"], now=1_000_000_000)
    r = con.execute("SELECT result, settlement_value FROM markets").fetchone()
    assert r["result"] == "void"
    assert r["settlement_value"] == pytest.approx(0.33)

    client.ticker_calls.clear()
    collect.run_once(con, client, ["S"], now=1_000_000_400)
    assert client.ticker_calls == [], "a recorded void must not be re-swept"
