"""The firm dashboard: one self-contained HTML page from a walk run."""
import json
import os

from wc.firm import dashboard, manager as mgr_mod, walk
from tests.test_firm_walk import _db, _desk, _blank, START, DAY, NO_COST


def _run(tmp_path):
    con = _db(tmp_path)
    design = _desk("d", dollars=25.0)
    design["view"] = {"view": "momentum", "params": {"lookback": 3}}
    design["agents"].append({"name": "second", "spec": design["agents"][0]["spec"]})
    later = json.loads(json.dumps(design))
    later["agents"] = later["agents"][:1]                 # prune 'second'
    script = [mgr_mod.Decision("change", "founding: momentum, two buyers", {}, config=design),
              mgr_mod.Decision("hold", "inside the noise band", {"variance_or_structure": "variance"}),
              mgr_mod.Decision("change", "second agent bleeds; prune it", {"variance_or_structure": "structure"}, config=later)]
    out = walk.run_walk(con, [_desk("bench"), _blank("d")], tick_days=2, start_ts=START,
                        end_ts=START + 6 * DAY, costs=NO_COST,
                        managers={"d": mgr_mod.ScriptedManager(script)})
    run_dir = tmp_path / "run"
    os.makedirs(run_dir)
    for name, d in out["desks"].items():
        with open(run_dir / f"pm-{name}.json", "w") as f:
            json.dump(d, f, default=str)
    with open(run_dir / "leaderboard.json", "w") as f:
        json.dump({k: out[k] for k in ("leaderboard", "tick_days", "start", "end")}, f)
    return run_dir


def test_dashboard_renders_the_whole_story(tmp_path):
    run_dir = _run(tmp_path)
    run = dashboard.load_run(str(run_dir))
    html = dashboard.render(run)

    assert "<svg" in html and "<script" not in html.split("</head>")[0]
    # leaderboard names every desk and marks the benchmark
    assert "bench" in html and 'class="bench"' in html
    # the desk's story: founding, every decision, the roster changes, the fills
    assert "founding: momentum, two buyers" in html
    assert "inside the noise band" in html
    assert "second agent bleeds; prune it" in html
    assert "retired" in html and "added" in html or "winding down" in html
    assert "settled" in html                      # how each position ended
    assert "v2" in html and "v3" in html          # versions on the timeline
    # every value is reachable in a table, not only in a chart
    assert html.count("<table") >= 4
    # theme tokens declared for both modes, body has an explicit background
    assert "prefers-color-scheme: dark" in html and 'data-theme="dark"' in html
    assert "background" in html


def test_main_writes_the_file(tmp_path):
    run_dir = _run(tmp_path)
    out = tmp_path / "firm.html"
    assert dashboard.main(["--run", str(run_dir), "--out", str(out)]) == 0
    assert out.exists() and out.stat().st_size > 5000


def test_each_agent_opens_into_its_own_ledger_and_attribution(tmp_path):
    """The roster says an agent lost money; clicking it must say where from —
    fees versus outcomes, which leagues, which price bands, its worst trades,
    and every contract it bought."""
    run = dashboard.load_run(str(_run(tmp_path)))
    html = dashboard.render(run)

    # one expandable block per agent, not a flat row
    assert html.count('<details class="agent"') >= 2
    assert 'id="agent-d-buyer"' in html
    # attribution: the three numbers that separate a bad view from a costly one
    for label in ("gross", "fees", "won", "lost", "sold early",
                  "by league", "by entry price", "worst trades"):
        assert label in html, label
    # and its own contract ledger, with tickers
    assert html.count("D0M0") >= 1


def test_an_agent_with_no_trades_says_so(tmp_path):
    run = dashboard.load_run(str(_run(tmp_path)))
    html = dashboard.render(run)
    assert "never traded" in html


def test_a_partial_run_refreshes_itself_and_a_finished_one_does_not(tmp_path):
    """Mid-run the page must not be a snapshot the reader mistakes for the
    result; finished, it must be stable so a saved report reads the same
    whenever it is opened."""
    run_dir = _run(tmp_path)
    run = dashboard.load_run(str(run_dir))
    assert run["partial"] is False
    done = dashboard.render(run)
    assert "http-equiv" not in done and "in progress" not in done

    meta = json.load(open(run_dir / "leaderboard.json"))
    meta["partial"] = True
    json.dump(meta, open(run_dir / "leaderboard.json", "w"))
    live = dashboard.render(dashboard.load_run(str(run_dir)))
    assert 'http-equiv="refresh"' in live and "in progress" in live


def test_watch_rerenders_until_the_run_is_complete(tmp_path):
    run_dir = _run(tmp_path)
    meta = json.load(open(run_dir / "leaderboard.json"))
    meta["partial"] = True
    json.dump(meta, open(run_dir / "leaderboard.json", "w"))
    out = tmp_path / "firm.html"

    renders = []

    def flip():
        """Second time round, the run has finished — watch must stop."""
        renders.append(1)
        if len(renders) == 2:
            m = json.load(open(run_dir / "leaderboard.json"))
            m["partial"] = False
            json.dump(m, open(run_dir / "leaderboard.json", "w"))

    n = dashboard.watch(str(run_dir), str(out), interval=0, on_render=flip)
    # two live renders, then one more once the run reports itself finished:
    # the last page written is never one still claiming to be in progress
    assert n == 3 and out.exists()
    assert 'http-equiv="refresh"' not in open(out).read()
