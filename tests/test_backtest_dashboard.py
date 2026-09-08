"""The dashboard.

Presentation, but not cosmetic: the page decides which number a person reads
first. These tests are mostly about the honest number being the one on screen
and the flattering ones being labelled.
"""
import json

import pytest

from wc.backtest import dashboard, metrics


def report(**over):
    r = {
        "strategy": "s1", "label": "out-of-sample (4 windows)",
        "spec_path": "strategies/s1.json", "bars": 100,
        "n_trades": 10, "n_settled": 10, "n_liquidated_at_end": 0,
        "gross_pnl": 12.0, "fees": 2.0, "net_pnl": 10.0,
        "max_drawdown": 3.0, "max_drawdown_pct": 0.03,
        "sharpe": 1.2, "sortino": 1.4, "win_rate": 0.6,
        "brier": 0.19, "log_loss": 0.55,
        "calibration": [{"bin_low": 0.0, "bin_high": 0.1, "n": 5,
                         "mean_predicted": 0.05, "actual_rate": 0.04}],
        "equity_curve": [[0, 1000.0], [86400, 1004.0], [172800, 1010.0]],
        "by_days_to_resolution": {"7-30": {"n": 10, "net_pnl": 10.0,
                                           "win_rate": 0.6, "brier": 0.19}},
        "by_price": {}, "by_series": {},
        "rejections": {"universe_filter": 42},
        "caveat": "Fills are modelled at candlestick resolution. UPPER BOUND.",
        "warnings": [],
    }
    r.update(over)
    return r


# ── it renders from metrics.json alone ────────────────────────────────────────

def test_page_is_self_contained():
    """No server, no CDN, no live call — a result must be reviewable months
    later and identical every time it is opened."""
    h = dashboard.render([report()])
    assert "<script" not in h
    assert "http://" not in h and "https://" not in h
    assert "<style>" in h


def test_empty_results_say_so_instead_of_rendering_nothing():
    h = dashboard.render([])
    assert "No results yet" in h


def test_survives_a_report_missing_optional_sections():
    """An older or partial metrics.json must not crash the page."""
    h = dashboard.render([{"strategy": "bare", "n_trades": 0}])
    assert "bare" in h


# ── the honest number is the visible one ──────────────────────────────────────

def test_in_sample_and_same_bar_are_labelled_not_headlined():
    h = dashboard.render([report(
        in_sample={"net_pnl": 99.0, "label": "in-sample"},
        same_bar_fill={"net_pnl": 88.0, "label": "same-bar"},
        latency_sensitivity=8.8)])
    assert "reference only, not evidence" in h
    assert "diagnostic" in h
    assert "Neither is a result" in h


def test_holdout_is_called_the_clean_read():
    h = dashboard.render([report(holdout={"net_pnl": 5.0, "label": "HELD-OUT"})])
    assert "HELD OUT" in h and "touched once" in h


def test_warnings_are_rendered_prominently():
    h = dashboard.render([report(warnings=[
        "LATENCY-SENSITIVE: this edge is an execution artefact."])])
    assert "warn danger" in h
    assert "execution artefact" in h


# ── the leaderboard ───────────────────────────────────────────────────────────

def test_leaderboard_ranks_on_out_of_sample():
    h = dashboard.render([report(strategy="zzz-weak", net_pnl=1.0),
                          report(strategy="aaa-strong", net_pnl=50.0)])
    assert h.index("aaa-strong") < h.index("zzz-weak")


def test_sections_follow_the_leaderboard_order():
    """Scrolling should match ranking, not the order files happened to load."""
    h = dashboard.render([report(strategy="zzz-weak", net_pnl=1.0),
                          report(strategy="aaa-strong", net_pnl=50.0)])
    assert h.rindex("aaa-strong") < h.rindex("zzz-weak")


def test_leaderboard_flags_a_latency_artefact():
    h = dashboard.render([report(strategy="a", latency_sensitivity=4.0),
                          report(strategy="b")])
    assert "⚠ latency" in h


def test_leaderboard_flags_lookahead():
    """The row most likely to top a leaderboard is the one with the most
    inflated number, so the flag has to be on the leaderboard itself."""
    h = dashboard.render([report(strategy="a", net_pnl=999.0,
                                 lookahead_risk="brains carry present-day Elo"),
                          report(strategy="b")])
    assert "⚠ lookahead" in h
    assert "not</em>\n     promotion evidence" in h or "promotion evidence" in h


def test_no_leaderboard_for_a_single_strategy():
    assert "Leaderboard" not in dashboard.render([report()])


# ── charts ────────────────────────────────────────────────────────────────────

def test_calibration_plot_has_the_reference_diagonal():
    """Without the diagonal the plot is decoration."""
    h = dashboard.render([report()])
    assert 'class="ref"' in h
    assert 'class="dot"' in h


def test_calibration_absent_says_why_and_how_to_fix_it():
    h = dashboard.render([report(calibration=[])])
    assert "No calibration data" in h and "--brains" in h


def test_equity_curve_needs_two_points():
    h = dashboard.render([report(equity_curve=[[0, 1000.0]])])
    assert "Not enough closed trades" in h


def test_equity_chart_shades_the_drawdown():
    h = dashboard.render([report(equity_curve=[[0, 1000.0], [1, 1100.0],
                                               [2, 900.0], [3, 1050.0]])])
    assert 'class="dd"' in h and 'class="eq"' in h


def test_a_flat_curve_does_not_divide_by_zero():
    h = dashboard.render([report(equity_curve=[[0, 1000.0], [86400, 1000.0]])])
    assert 'class="eq"' in h


# ── safety ────────────────────────────────────────────────────────────────────

def test_strategy_names_are_escaped():
    h = dashboard.render([report(strategy="<script>alert(1)</script>")])
    assert "<script>alert" not in h
    assert "&lt;script&gt;" in h


def test_theme_is_defined_for_light_and_dark():
    h = dashboard.render([report()])
    assert "prefers-color-scheme:dark" in h
    assert "data-theme=dark" in h


# ── loading ───────────────────────────────────────────────────────────────────

def test_load_reports_skips_non_metrics_json(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps(report()))
    (tmp_path / "other.json").write_text(json.dumps({"unrelated": True}))
    (tmp_path / "broken.json").write_text("{not json")
    found = dashboard.load_reports(str(tmp_path / "*.json"))
    assert [r["strategy"] for r in found] == ["s1"]


def test_cli_writes_a_file(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps(report()))
    out = tmp_path / "d.html"
    assert dashboard.main(["--results", str(tmp_path / "r.json"),
                           "--out", str(out)]) == 0
    assert "<!doctype html>" in out.read_text()


def test_real_metrics_output_renders(tmp_path):
    """Guards against metrics.summarize and the dashboard drifting apart."""
    trades = [{"ticker": "M", "series": "S", "side": "yes", "contracts": 10,
               "entry_ts": 0, "entry_price": 0.4, "exit_ts": 86400,
               "exit_price": 1.0, "cost": 4.0, "gross_pnl": 6.0, "fees": 0.2,
               "net_pnl": 5.8, "return_pct": 1.45,
               "entry_days_to_resolution": 10.0, "model_prob": 0.5,
               "outcome": 1, "settled": True}]
    r = metrics.summarize(trades, 1000.0, {"universe_filter": 3})
    r["strategy"] = "real"
    h = dashboard.render([r])
    assert "real" in h and "UPPER BOUND" in h
