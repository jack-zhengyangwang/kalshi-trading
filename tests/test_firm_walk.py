"""The walk-forward runner: desks that wake up every tick, managers that
decide between ticks, benchmarks that never change.

Everything here runs on a temporary history DB and a scripted manager — no
network. The LLM manager's own tests live in test_firm_manager.py.
"""
import json

import pytest

from wc.backtest import data, engine
from wc.firm import manager as mgr_mod
from wc.firm import walk

DAY = 86400
START = 1_800_000_000 - (1_800_000_000 % DAY)      # a UTC midnight


def _db(tmp_path):
    """Six days, three markets a day, each settling the day after it opens."""
    con = data.connect(str(tmp_path / "h.db"))
    for d in range(6):
        for j in range(3):
            t = f"D{d}M{j}"
            open_ts = START + d * DAY
            close = open_ts + DAY
            result = "yes" if (d + j) % 3 else "no"
            data.upsert_market(con, {"ticker": t, "series": "KXT", "title": t,
                                     "sub_title": "Arsenal", "home": "Arsenal",
                                     "away": "Chelsea", "event_ticker": f"E{d}{j}",
                                     "close_time": close, "status": "finalized",
                                     "result": result}, now=open_ts)
            rows = [{"ts": open_ts + h * 3600, "yes_bid": 30 + h, "yes_ask": 32 + h,
                     "open": 31, "high": 31, "low": 31, "close": 31 + h,
                     "volume": 500, "open_interest": 100} for h in range(24)]
            rows.append({"ts": close, "yes_bid": 99, "yes_ask": 100, "open": 99,
                         "high": 99, "low": 99, "close": 99, "volume": 500,
                         "open_interest": 100})
            data.upsert_backfill_candles(con, t, 60, rows)
    return con


def _agent(dollars=20.0):
    return {"name": "buyer", "version": 1, "side": "yes",
            "universe": {"series": ["*"], "min_volume": 1},
            "entry": {"all": [{"signal": "price", "op": "lt", "value": 0.45}]},
            "sizing": {"method": "fixed", "dollars": dollars,
                       "max_bet_dollars": dollars, "max_concurrent_positions": 5},
            "exit": {"any": [{"signal": "hold_to_settlement", "op": "eq", "value": True}]},
            "caps": {"daily_spend_dollars": 1000.0, "per_market_dollars": 100.0,
                     "total_exposure_dollars": 1000.0}}


def _desk(name, manager=None, dollars=20.0):
    c = {"name": name, "view": {"view": "market"}, "bankroll": 500.0,
         "agents": [{"name": "buyer", "spec": _agent(dollars)}],
         "caps": {"daily_spend_dollars": 1000.0, "total_exposure_dollars": 1000.0}}
    if manager:
        c["manager"] = manager
    return c


NO_COST = engine.Costs(fee_rate=0.0, slippage_cents=0.0, max_volume_share=1.0)


def test_one_tick_walk_equals_the_single_pass_backtest(tmp_path):
    con = _db(tmp_path)
    cfg = _desk("fixed")
    bars = data.load_bars(con, source="backfill", interval_min=60)
    from wc.firm import pm as pm_mod, run as run_mod
    single = run_mod.backtest_pm(pm_mod.build(cfg), bars, costs=NO_COST)

    out = walk.run_walk(con, [cfg], tick_days=30, start_ts=START,
                        end_ts=START + 7 * DAY, costs=NO_COST)
    rep = out["desks"]["fixed"]["report"]
    assert rep["n_trades"] == single["n_trades"] > 0
    assert rep["net_pnl"] == pytest.approx(single["net_pnl"])


def test_scripted_manager_decisions_are_applied_and_logged(tmp_path):
    con = _db(tmp_path)
    changed = _desk("managed", dollars=40.0)
    bad = _desk("managed")
    bad["bankroll"] = 9_999.0                      # a manager may not fund itself
    script = [
        mgr_mod.Decision("hold", "first tick, too early to judge", {}),
        mgr_mod.Decision("change", "double the stake", {}, config=changed),
        mgr_mod.Decision("change", "give me money", {}, config=bad),
    ]
    cfgs = [_desk("bench"), _desk("managed", manager={"type": "llm",
                                                     "provider": "anthropic",
                                                     "model": "x"})]
    out = walk.run_walk(con, cfgs, tick_days=2, start_ts=START,
                        end_ts=START + 6 * DAY, costs=NO_COST,
                        managers={"managed": mgr_mod.ScriptedManager(script)})

    m = out["desks"]["managed"]
    log = m["decisions"]
    assert [d["action"] for d in log] == ["hold", "change", "change"]
    assert log[0]["applied"] is False and log[1]["applied"] is True
    assert log[2]["applied"] is False and "bankroll" in log[2]["rejected"]
    assert m["version"] == 2                       # one accepted change
    assert m["config"]["agents"][0]["spec"]["sizing"]["dollars"] == 40.0
    # every entry says when it was taken and what the manager saw
    assert all({"tick", "period", "reasoning", "brief"} <= set(d) for d in log)

    # the benchmark never had a manager and never changed
    b = out["desks"]["bench"]
    assert b["decisions"] == [] and b["version"] == 1
    # both desks have trades and the leaderboard names both
    assert {r["pm"] for r in out["leaderboard"]} == {"bench", "managed"}
    assert m["report"]["n_trades"] > 0


def test_positions_survive_a_manager_change(tmp_path):
    """A change of rules is not a liquidation. What the desk held before the
    tick it still holds after, and settles on its own terms."""
    con = _db(tmp_path)
    changed = _desk("managed", dollars=5.0)
    script = [mgr_mod.Decision("change", "smaller", {}, config=changed)]
    out = walk.run_walk(con, [_desk("managed")], tick_days=1, start_ts=START,
                        end_ts=START + 2 * DAY, costs=NO_COST,
                        managers={"managed": mgr_mod.ScriptedManager(script)})
    trades = out["desks"]["managed"]["trades"]
    # the day-0 markets were bought at $20 before the change and settle on day 1
    day0 = [t for t in trades if t["ticker"].startswith("D0")]
    assert day0 and all(t["cost"] == pytest.approx(20.0, abs=1.0) for t in day0)
    assert all(t["settled"] for t in day0)


def test_brief_carries_what_a_manager_needs(tmp_path):
    con = _db(tmp_path)
    seen = []

    class Spy(mgr_mod.Manager):
        def decide(self, brief):
            seen.append(brief)
            return mgr_mod.Decision("hold", "watching", {})

    walk.run_walk(con, [_desk("bench"), _desk("m", manager={"type": "llm",
                                                             "provider": "groq",
                                                             "model": "x"})],
                  tick_days=2, start_ts=START, end_ts=START + 4 * DAY,
                  costs=NO_COST, managers={"m": Spy()})
    assert len(seen) == 2
    b = seen[1]
    for key in ("desk", "tick", "period", "config", "version", "this_tick",
                "cumulative", "previous_tick", "benchmarks", "open_positions",
                "variance", "decisions"):
        assert key in b, key
    assert "bench" in b["benchmarks"]
    assert b["previous_tick"]["net_pnl"] == seen[0]["this_tick"]["net_pnl"]
    assert b["decisions"][0]["action"] == "hold"
    assert "per_trade_std" in b["variance"] and "noise_band" in b["variance"]
    json.dumps(b)                                  # the brief must be serialisable


# ── founding: a manager designs its own desk before the first bar ─────────────

def _blank(name, provider="openrouter", model="qwen/qwen3.8-27b:free"):
    """A seed desk: the placeholder is baseline-market's config, which trades
    nothing. What the desk becomes is entirely the manager's doing."""
    return {"name": name, "bankroll": 500.0, "founding": True,
            "view": {"view": "market"},
            "agents": [{"name": "edge-taker", "spec": "strategies/agents/edge-taker.json"}],
            "caps": {"daily_spend_dollars": 60.0, "total_exposure_dollars": 300.0},
            "manager": {"type": "llm", "provider": provider, "model": model}}


def test_founding_brief_comes_before_the_first_bar_and_describes_the_arena(tmp_path):
    con = _db(tmp_path)
    seen = []

    class Spy(mgr_mod.Manager):
        def decide(self, brief):
            seen.append(brief)
            return mgr_mod.Decision("hold", "thinking", {})

    walk.run_walk(con, [_desk("bench"), _blank("d")], tick_days=2, start_ts=START,
                  end_ts=START + 2 * DAY, costs=NO_COST, managers={"d": Spy()})
    first = seen[0]
    assert first["founding"] is True and first["tick"] == 0
    assert "this_tick" not in first                    # no results yet
    for key in ("bankroll", "caps", "views", "dsl", "example_agents",
                "universe", "benchmarks", "placeholder"):
        assert key in first, key
    assert "edge-taker" in first["example_agents"]
    assert first["universe"]["markets"] > 0 and first["universe"]["series"]
    # a hold is not a design: three founding attempts, then the ticks begin
    assert [b["tick"] for b in seen[:4]] == [0, 0, 0, 1]
    assert "this_tick" in seen[3]


def test_a_founded_desk_runs_the_managers_design(tmp_path):
    con = _db(tmp_path)
    design = _desk("d", dollars=25.0)
    design["view"] = {"view": "momentum", "params": {"lookback": 3}}
    script = [mgr_mod.Decision("change", "my desk: momentum, one buyer", {}, config=design)]
    out = walk.run_walk(con, [_blank("d")], tick_days=2, start_ts=START,
                        end_ts=START + 4 * DAY, costs=NO_COST,
                        managers={"d": mgr_mod.ScriptedManager(script)})
    d = out["desks"]["d"]
    assert d["decisions"][0]["tick"] == 0 and d["decisions"][0]["applied"]
    assert d["config"]["view"]["view"] == "momentum"
    assert d["report"]["view"].startswith("momentum")
    assert d["version"] == 2
    assert d["config"].get("founding") is False


def test_a_manager_that_cannot_found_a_desk_runs_the_placeholder(tmp_path):
    """Three tries, each told why the last was refused; then the desk trades
    nothing, and the log says so. A manager that cannot write a valid desk
    earns zero — that is a fact about the manager."""
    con = _db(tmp_path)
    bad = _desk("d"); bad["view"] = {"view": "crystal-ball"}
    seen = []

    class Stubborn(mgr_mod.Manager):
        def decide(self, brief):
            seen.append(brief)
            return mgr_mod.Decision("change", "trust me", {}, config=bad)

    out = walk.run_walk(con, [_blank("d")], tick_days=2, start_ts=START,
                        end_ts=START + 2 * DAY, costs=NO_COST,
                        managers={"d": Stubborn()})
    d = out["desks"]["d"]
    founding = [x for x in d["decisions"] if x["tick"] == 0]
    assert len(founding) == 3 and all(not x["applied"] for x in founding)
    assert "crystal-ball" in founding[0]["rejected"]
    assert seen[1]["previous_attempt_rejected"]        # told what went wrong
    assert d["config"]["view"] == {"view": "market"} and d["version"] == 1
    assert d["report"]["n_trades"] == 0


def test_the_desk_description_is_the_managers_own_words(tmp_path):
    """After founding, the desk describes itself in the manager's reasoning,
    and every later change appends its reasoning — the config carries the
    desk's story in the manager's words, not the seed's boilerplate."""
    con = _db(tmp_path)
    design = _desk("d", dollars=25.0)
    later = _desk("d", dollars=10.0)
    script = [mgr_mod.Decision("change", "Founding: I back home teams because the book underrates them.", {}, config=design),
              mgr_mod.Decision("hold", "noise", {}),
              mgr_mod.Decision("change", "Halving size: the drawdown says my sizing was wrong, not my view.", {}, config=later)]
    out = walk.run_walk(con, [_blank("d")], tick_days=2, start_ts=START,
                        end_ts=START + 4 * DAY, costs=NO_COST,
                        managers={"d": mgr_mod.ScriptedManager(script)})
    desc = out["desks"]["d"]["config"]["description"]
    assert desc.startswith("Founding: I back home teams")
    assert "Halving size" in desc and "noise" not in desc
    assert "v3" in desc                                     # each change is stamped


def test_walk_defaults_to_the_range_of_the_source_it_replays(tmp_path):
    """No --start/--end means the whole archive of the chosen SOURCE. The
    collector table starting weeks later must not shorten a backfill walk."""
    con = _db(tmp_path)                                    # backfill: 6 days from START
    late = START + 5 * DAY
    data.upsert_candles(con, "D5M0", [{"ts": late, "yes_bid": 30, "yes_ask": 32, "open": 31,
                                       "high": 31, "low": 31, "close": 31, "volume": 5,
                                       "open_interest": 1}])
    out = walk.run_walk(con, [_desk("bench")], tick_days=2, costs=NO_COST, source="backfill")
    assert out["start"] == walk._date(START)
    assert out["desks"]["bench"]["report"]["n_trades"] > 0


def test_positions_settle_during_the_walk_not_only_at_the_end(tmp_path):
    """Day-0 markets close on day 1. A manager reading tick 2's brief must see
    them settled — realized P&L and Brier — not a book that never resolves
    until the data runs out. (The archive's last bar sits just inside
    close_time, so the per-bar settlement test alone never fires.)"""
    con = _db(tmp_path)
    seen = []

    class Spy(mgr_mod.Manager):
        def decide(self, brief):
            seen.append(brief)
            return mgr_mod.Decision("hold", "watching", {})

    walk.run_walk(con, [_desk("m", manager={"type": "llm", "provider": "groq", "model": "x"})],
                  tick_days=1, start_ts=START, end_ts=START + 4 * DAY, costs=NO_COST,
                  managers={"m": Spy()})
    ticks = [b for b in seen if not b.get("founding")]
    assert ticks[0]["this_tick"]["positions_opened"] == 3       # bought on day 0
    assert ticks[0]["this_tick"]["positions_settled"] == 0
    assert ticks[1]["this_tick"]["positions_settled"] == 3       # resolved on day 1
    assert ticks[1]["this_tick"]["brier"] is not None
    assert ticks[1]["cumulative"]["n_settled"] == 3
    # the headline is explicit about opened vs closed, and the open book is summarised
    for k in ("positions_opened", "positions_closed", "positions_settled",
              "open_positions", "open_cost", "unrealized"):
        assert k in ticks[0]["this_tick"], k


# ── memory: a week verbatim, everything older compressed ──────────────────────

def test_the_brief_keeps_a_week_verbatim_and_digests_the_rest(tmp_path):
    """The brief must not grow without bound: a tick-40 call would otherwise
    pay for 39 previous decisions. The last week is what a manager needs to
    avoid repeating an experiment it just ran; older ticks are one line each,
    and what matters from further back is whatever the manager itself chose
    to write down."""
    con = _db(tmp_path)
    seen = []

    class Spy(mgr_mod.Manager):
        def decide(self, brief):
            seen.append(brief)
            n = brief["tick"]
            return mgr_mod.Decision("hold", f"reasoning for tick {n}" + " padding" * 50, {},
                                    notes=f"standing note after tick {n}")

    walk.run_walk(con, [_desk("bench"), _blank("d")], tick_days=1, start_ts=START,
                  end_ts=START + 6 * DAY, costs=NO_COST, managers={"d": Spy()},
                  memory_days=3)
    last = seen[-1]
    # only decisions inside the window are quoted in full
    quoted = [x["tick"] for x in last["decisions"]]
    assert quoted and min(quoted) >= last["tick"] - 3
    assert all("reasoning" in x for x in last["decisions"])
    # everything older is one line each, with no prose
    older = last["earlier"]["ticks"]
    assert older and max(x["tick"] for x in older) < min(quoted)
    assert all(set(x) == {"tick", "action", "changed", "applied", "rejected", "net"}
               for x in older)
    assert last["earlier"]["summary"]["decisions"] == len(older)
    # the manager's own standing note survives from the start, verbatim
    assert last["notes"] == f"standing note after tick {last['tick'] - 1}"
    # and the brief stops growing
    sizes = [len(json.dumps(b, default=str)) for b in seen if not b.get("founding")]
    assert max(sizes[3:]) < 2 * min(sizes[3:])


def test_output_is_written_after_every_tick(tmp_path):
    """A crash at tick 45 must not cost 45 ticks of work, and the dashboard
    must be readable mid-run. Every tick rewrites the run directory."""
    con = _db(tmp_path)
    out_dir = tmp_path / "run"
    seen = []

    class Watcher(mgr_mod.Manager):
        def decide(self, brief):
            if brief.get("founding"):
                return mgr_mod.Decision("change", "go", {}, config=_desk("d"))
            # what is on disk at the moment this tick's manager runs
            board = out_dir / "leaderboard.json"
            seen.append(json.load(open(board))["leaderboard"] if board.exists() else None)
            return mgr_mod.Decision("hold", "watching", {})

    walk.run_walk(con, [_blank("d")], tick_days=2, start_ts=START, end_ts=START + 6 * DAY,
                  costs=NO_COST, managers={"d": Watcher()}, out_dir=str(out_dir))
    # each tick's results are on disk before that tick's manager is called,
    # so a crash anywhere loses at most the decision, never the trading
    assert seen[0] and seen[0][0]["pm"] == "d"
    assert seen[-1][0]["n_trades"] >= seen[0][0]["n_trades"]
    assert json.load(open(out_dir / "pm-d.json"))["ticks"]
    assert json.load(open(out_dir / "leaderboard.json"))["partial"] is False


def test_a_rejected_change_is_explained_on_the_next_tick(tmp_path):
    """A manager whose config was refused must be told why, or it repeats the
    same invalid change forever — one desk burned forty ticks renaming itself."""
    con = _db(tmp_path)
    bad = _desk("wrong-name")
    seen = []

    class Stubborn(mgr_mod.Manager):
        def decide(self, brief):
            seen.append(brief)
            if brief.get("founding"):
                return mgr_mod.Decision("change", "go", {}, config=_desk("d"))
            return mgr_mod.Decision("change", "rename", {}, config=bad)

    walk.run_walk(con, [_blank("d")], tick_days=2, start_ts=START, end_ts=START + 6 * DAY,
                  costs=NO_COST, managers={"d": Stubborn()})
    later = [b for b in seen if not b.get("founding")][1:]
    assert later and all("name may not change" in (b["last_change_rejected"] or "")
                         for b in later)
