"""Phase 5 — dashboard: a self-contained HTML page from metrics.json.

No server, no build step, no CDN. One file you can open from a laptop or mail
to yourself, rendered from `metrics.json` alone with no live API calls — so a
result is reviewable long after the run, and identical every time it is opened.

Charts are hand-drawn SVG rather than a charting library, because the page has
to work offline and a pinned CDN script is one more thing that can go missing
between generating a report and reading it.

Out-of-sample is what you see by default. In-sample and same-bar fills are
labelled as what they are: reference and diagnostic, never results.

Usage:
    python3 -m wc.backtest.dashboard                       # every metrics.json
    python3 -m wc.backtest.dashboard --out report.html
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import json
import os

from wc import paths

RESULTS_DIR = os.path.join(paths.ROOT, "data", "backtests")
DEFAULT_OUT = os.path.join(RESULTS_DIR, "dashboard.html")

W, H, PAD = 640, 200, 34          # chart geometry


def _iso(ts):
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def _money(v):
    return "—" if v is None else f"${v:+,.2f}"


def _num(v, places=3):
    return "—" if v is None else f"{v:.{places}f}"


def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


# ── charts ────────────────────────────────────────────────────────────────────

def equity_svg(curve):
    """Equity over time, with the drawdown from the running peak shaded."""
    if len(curve) < 2:
        return '<p class="empty">Not enough closed trades to plot an equity curve.</p>'

    xs = [p[0] for p in curve]
    ys = [p[1] for p in curve]
    x0, x1 = min(xs), max(xs)
    lo, hi = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if hi == lo:
        hi = lo + 1

    def px(x):
        return PAD + (x - x0) / (x1 - x0) * (W - 2 * PAD)

    def py(y):
        return H - PAD - (y - lo) / (hi - lo) * (H - 2 * PAD)

    line = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in curve)

    # drawdown band: the running peak, so the shaded gap IS the drawdown
    peak, peaks = ys[0], []
    for x, y in curve:
        peak = max(peak, y)
        peaks.append((x, peak))
    band = (" ".join(f"{px(x):.1f},{py(p):.1f}" for x, p in peaks) + " " +
            " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in reversed(curve)))

    start = curve[0][1]
    zero = py(start) if lo <= start <= hi else None
    return f'''<svg viewBox="0 0 {W} {H}" class="chart" role="img"
     aria-label="Equity curve with drawdown shaded">
  <polygon points="{band}" class="dd"/>
  {f'<line x1="{PAD}" y1="{zero:.1f}" x2="{W - PAD}" y2="{zero:.1f}" class="ref"/>' if zero else ''}
  <polyline points="{line}" class="eq"/>
  <text x="{PAD}" y="{H - 8}" class="ax">{_iso(x0)}</text>
  <text x="{W - PAD}" y="{H - 8}" class="ax end">{_iso(x1)}</text>
  <text x="{PAD}" y="16" class="ax">${hi:,.0f}</text>
  <text x="{PAD}" y="{H - PAD + 12}" class="ax">${lo:,.0f}</text>
</svg>'''


def calibration_svg(bins):
    """Predicted vs actual, with the perfect-calibration diagonal.

    The diagonal is the whole point: without it the plot is decoration. Dot area
    is proportional to sample size, so a bucket holding three trades cannot look
    as convincing as one holding three hundred.
    """
    if not bins:
        return ('<p class="empty">No calibration data — this needs settled '
                'trades that carried a model probability. Run with '
                '<code>--brains</code>.</p>')

    S = 260
    def px(v):
        return PAD + v * (S - 2 * PAD)

    def py(v):
        return S - PAD - v * (S - 2 * PAD)

    biggest = max(b["n"] for b in bins) or 1
    dots = "".join(
        f'<circle cx="{px(b["mean_predicted"]):.1f}" cy="{py(b["actual_rate"]):.1f}" '
        f'r="{3 + 7 * (b["n"] / biggest) ** 0.5:.1f}" class="dot">'
        f'<title>predicted {b["mean_predicted"]:.2f}, actual {b["actual_rate"]:.2f}, '
        f'n={b["n"]}</title></circle>'
        for b in bins)

    return f'''<svg viewBox="0 0 {S} {S}" class="chart calib" role="img"
     aria-label="Calibration plot against the perfect-calibration diagonal">
  <line x1="{px(0)}" y1="{py(0)}" x2="{px(1)}" y2="{py(1)}" class="ref"/>
  <line x1="{PAD}" y1="{PAD}" x2="{PAD}" y2="{S - PAD}" class="ax-line"/>
  <line x1="{PAD}" y1="{S - PAD}" x2="{S - PAD}" y2="{S - PAD}" class="ax-line"/>
  {dots}
  <text x="{PAD}" y="{S - 8}" class="ax">0</text>
  <text x="{S - PAD}" y="{S - 8}" class="ax end">1 — predicted</text>
  <text x="6" y="{PAD}" class="ax">1</text>
  <text x="6" y="{S - PAD}" class="ax">0 actual</text>
</svg>'''


# ── sections ──────────────────────────────────────────────────────────────────

def _stat(label, value, note=""):
    return (f'<div class="stat"><div class="k">{html.escape(label)}</div>'
            f'<div class="v">{html.escape(str(value))}</div>'
            f'{f"<div class=n>{html.escape(note)}</div>" if note else ""}</div>')


def _segments(title, seg):
    if not seg:
        return ""
    rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td class=r>{v['n']}</td>"
        f"<td class='r {'pos' if v['net_pnl'] > 0 else 'neg'}'>{_money(v['net_pnl'])}</td>"
        f"<td class=r>{_pct(v['win_rate'])}</td>"
        f"<td class=r>{_num(v['brier'])}</td></tr>"
        for k, v in seg.items())
    return f'''<h4>{html.escape(title)}</h4>
<table><thead><tr><th>bucket</th><th class=r>n</th><th class=r>net</th>
<th class=r>win rate</th><th class=r>Brier</th></tr></thead>
<tbody>{rows}</tbody></table>'''


def _warnings(report):
    out = []
    for w in report.get("warnings") or []:
        cls = "warn danger" if ("LOOKAHEAD" in w or "LATENCY-SENSITIVE" in w) else "warn"
        out.append(f'<div class="{cls}">{html.escape(w)}</div>')
    return "".join(out)


def _provenance(report):
    """A PM is identified by what it BELIEVES; a bare strategy by its spec."""
    if report.get("view"):
        return "view <code>" + html.escape(str(report["view"])) + "</code>"
    return "spec <code>" + html.escape(str(report.get("spec_path", "—"))) + "</code>"


def _agents_table(report):
    """Per-agent P&L inside a PM's book.

    A desk whose profit comes from one agent while the others bleed is a
    different thing from a desk that is broadly right, and the aggregate hides
    which one you have.
    """
    agents = report.get("agents") or {}
    if not agents:
        return ""
    rows = "".join(
        "<tr><td>" + html.escape(str(n)) + "</td>"
        "<td class=r>$" + format(a.get("allocated", 0), ",.0f") + "</td>"
        "<td class=r>" + format(a.get("n_trades", 0), ",") + "</td>"
        "<td class='r " + ("pos" if (a.get("net_pnl") or 0) > 0 else "neg") + "'>"
        + _money(a.get("net_pnl")) + "</td>"
        "<td><code>" + html.escape(str(a.get("spec", ""))) + "</code></td></tr>"
        for n, a in sorted(agents.items(),
                           key=lambda kv: -(kv[1].get("net_pnl") or 0)))
    return ("<h3>The desk</h3>"
            '<p class="sub">Each agent trades the PM&rsquo;s view with its own '
            "capital. One winner carrying four losers is not the same desk as "
            "four steady ones.</p>"
            "<table><thead><tr><th>agent</th><th class=r>allocated</th>"
            "<th class=r>trades</th><th class=r>net</th><th>spec</th></tr>"
            "</thead><tbody>" + rows + "</tbody></table>")


def render_strategy(report):
    net = report.get("net_pnl")
    sens = report.get("latency_sensitivity")
    sb = report.get("same_bar_fill") or {}
    hold = report.get("holdout") or {}
    ins = report.get("in_sample") or {}

    stats = "".join([
        _stat("net P&L", _money(net), report.get("label", "")),
        _stat("gross", _money(report.get("gross_pnl"))),
        _stat("fees", _money(report.get("fees")), "charged, not estimated"),
        _stat("trades", f"{report.get('n_trades', 0):,}",
              f"{report.get('n_settled', 0):,} settled, "
              f"{report.get('n_liquidated_at_end', 0):,} liquidated"),
        _stat("max drawdown", _money(-abs(report.get("max_drawdown") or 0)),
              _pct(report.get("max_drawdown_pct"))),
        _stat("Sharpe", _num(report.get("sharpe"), 2)),
        _stat("Brier", _num(report.get("brier")), "lower is better; 0.25 = coin flip"),
        _stat("log loss", _num(report.get("log_loss"))),
    ])

    gates = "".join([
        _stat("HELD OUT", _money(hold.get("net_pnl")),
              "touched once — the clean read") if hold else "",
        _stat("in-sample", _money(ins.get("net_pnl")),
              "reference only, not evidence") if ins else "",
        _stat("same-bar fill", _money(sb.get("net_pnl")),
              f"diagnostic{f' — {sens}x' if sens else ''}") if sb else "",
    ])

    rej = report.get("rejections") or {}
    rej_rows = "".join(f"<tr><td>{html.escape(k)}</td><td class=r>{v:,}</td></tr>"
                       for k, v in sorted(rej.items(), key=lambda kv: -kv[1]))

    return f'''<section>
  <h2>{html.escape(report.get("strategy", "unnamed"))}</h2>
  <p class="sub">{html.escape(report.get("label", ""))} ·
     {report.get("bars", 0):,} bars · {_provenance(report)}</p>
  {_warnings(report)}
  <div class="stats">{stats}</div>
  {f'<h3>Cross-checks</h3><div class="stats">{gates}</div>' if gates.strip() else ''}
  {_agents_table(report)}
  <h3>Equity</h3>
  {equity_svg(report.get("equity_curve") or [])}
  <h3>Calibration</h3>
  {calibration_svg(report.get("calibration") or [])}
  <h3>Segments</h3>
  <p class="sub">Edge varies sharply along these axes — a single aggregate
     number hides the effect being traded.</p>
  {_segments("By days to resolution", report.get("by_days_to_resolution"))}
  {_segments("By entry price", report.get("by_price"))}
  {_segments("By series", report.get("by_series"))}
  {f"<h3>Why trades were not placed</h3><table><thead><tr><th>reason</th><th class=r>count</th></tr></thead><tbody>{rej_rows}</tbody></table>" if rej_rows else ""}
  <p class="caveat">{html.escape(report.get("caveat", ""))}</p>
</section>'''


def render_leaderboard(reports):
    """Ranked on out-of-sample net P&L, with the held-out number beside it.

    Ranking on the in-sample number would rank on overfitting.
    """
    if len(reports) < 2:
        return ""
    rows = ""
    for r in sorted(reports, key=lambda x: -(x.get("net_pnl") or 0)):
        hold = (r.get("holdout") or {}).get("net_pnl")
        sens = r.get("latency_sensitivity")
        # Both flags matter on a leaderboard, because the row most likely to be
        # top of it is the one with the most inflated number.
        flags = ""
        if sens and sens > 2.0:
            flags += ' <span class="flag" title="edge is mostly its own trigger price">⚠ latency</span>'
        if r.get("lookahead_risk"):
            flags += ' <span class="flag danger" title="model-fitted lookahead — not promotion evidence">⚠ lookahead</span>'
        vw = r.get("view")
        vtag = ('<br><span class=vw>' + html.escape(str(vw)) + '</span>') if vw else ""
        rows += (f"<tr><td>{html.escape(r.get('strategy', '?'))}{flags}{vtag}</td>"
                 f"<td class='r {'pos' if (r.get('net_pnl') or 0) > 0 else 'neg'}'>"
                 f"{_money(r.get('net_pnl'))}</td>"
                 f"<td class='r {'pos' if (hold or 0) > 0 else 'neg'}'>{_money(hold)}</td>"
                 f"<td class=r>{r.get('n_trades', 0):,}</td>"
                 f"<td class=r>{_num(r.get('brier'))}</td>"
                 f"<td class=r>{_num(sens, 2)}</td></tr>")
    return f'''<section>
  <h2>Leaderboard</h2>
  <p class="sub">Ranked on out-of-sample net P&amp;L. Ranking on the in-sample
     number would be ranking on overfitting. A flagged row is <em>not</em>
     promotion evidence however good the number looks — see
     <code>docs/backtester/07_PROMOTION.md</code>.</p>
  <table><thead><tr><th>strategy / view</th><th class=r>OOS net</th>
  <th class=r>held out</th><th class=r>trades</th><th class=r>Brier</th>
  <th class=r>latency ×</th></tr></thead><tbody>{rows}</tbody></table>
</section>'''


CSS = """
:root{--bg:#fbfaf8;--fg:#1c1b19;--dim:#6b6862;--line:#e4e1db;--card:#fff;
      --pos:#1d7a4c;--neg:#b03328;--warn:#7a5b12;--warnbg:#fdf6e3;
      --danger:#8a2a20;--dangerbg:#fdf0ee;--accent:#2f6f8f}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#16151a;--fg:#eceae6;--dim:#9a968e;--line:#2e2c33;--card:#1e1d23;
  --pos:#5fd39b;--neg:#f0857a;--warn:#e0c068;--warnbg:#2a2415;
  --danger:#f0a094;--dangerbg:#2e1a17;--accent:#7fc4e8}}
:root[data-theme=dark]{--bg:#16151a;--fg:#eceae6;--dim:#9a968e;--line:#2e2c33;
  --card:#1e1d23;--pos:#5fd39b;--neg:#f0857a;--warn:#e0c068;--warnbg:#2a2415;
  --danger:#f0a094;--dangerbg:#2e1a17;--accent:#7fc4e8}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);margin:0;padding:2rem 1.25rem 4rem;
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .25rem}
h2{font-size:1.2rem;margin:0 0 .25rem}
h3{font-size:.95rem;margin:1.75rem 0 .5rem;text-transform:uppercase;
  letter-spacing:.06em;color:var(--dim)}
h4{font-size:.85rem;margin:1.25rem 0 .4rem;color:var(--dim)}
.sub{color:var(--dim);margin:.15rem 0 1rem;font-size:.86rem}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:1.4rem;margin:1.25rem 0}
.stats{display:grid;gap:.75rem;grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
.stat{border:1px solid var(--line);border-radius:8px;padding:.6rem .7rem}
.stat .k{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.stat .v{font-size:1.15rem;font-variant-numeric:tabular-nums;margin-top:.15rem}
.stat .n{font-size:.72rem;color:var(--dim);margin-top:.2rem}
table{width:100%;border-collapse:collapse;font-size:.85rem;
  font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:.75rem;text-transform:uppercase}
td.r,th.r{text-align:right}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.warn{background:var(--warnbg);color:var(--warn);border-left:3px solid currentColor;
  padding:.6rem .8rem;border-radius:0 6px 6px 0;margin:.5rem 0;font-size:.84rem}
.warn.danger{background:var(--dangerbg);color:var(--danger)}
.caveat{color:var(--dim);font-size:.8rem;font-style:italic;margin-top:1.5rem;
  padding-top:.75rem;border-top:1px solid var(--line)}
.chart{width:100%;height:auto;max-width:640px;display:block}
.chart.calib{max-width:300px}
.eq{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}
.dd{fill:var(--neg);opacity:.13}
.ref{stroke:var(--dim);stroke-width:1;stroke-dasharray:4 3;opacity:.7}
.ax-line{stroke:var(--line);stroke-width:1}
.dot{fill:var(--accent);opacity:.75}
.ax{font-size:10px;fill:var(--dim)}
.ax.end{text-anchor:end}
.empty{color:var(--dim);font-size:.86rem;font-style:italic}
.vw{font-size:.72rem;color:var(--dim)}
.flag{font-size:.68rem;color:var(--warn);background:var(--warnbg);
  padding:.1em .4em;border-radius:4px;white-space:nowrap;margin-left:.25rem}
.flag.danger{color:var(--danger);background:var(--dangerbg)}
code{font-size:.85em;background:var(--bg);padding:.1em .35em;border-radius:4px}
.overflow{overflow-x:auto}
footer{color:var(--dim);font-size:.78rem;text-align:center;margin-top:2rem}
"""


def render(reports, generated=None):
    generated = generated or dt.datetime.now(dt.timezone.utc)
    # Sections follow the leaderboard's order, so scrolling matches ranking.
    ranked = sorted(reports, key=lambda r: -(r.get("net_pnl") or 0))
    body = render_leaderboard(reports) + "".join(render_strategy(r) for r in ranked)
    if not reports:
        body = ('<section><p class="empty">No results yet. Run '
                '<code>python3 -m wc.backtest.run &lt;spec&gt;</code> first.</p>'
                '</section>')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest results</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Backtest results</h1>
<p class="sub">Generated {generated.strftime("%Y-%m-%d %H:%M UTC")} from
metrics.json — no live data, so this page reads the same whenever it is opened.</p>
{body}
<footer>Out-of-sample by default. In-sample is reference; same-bar fills are a
diagnostic. Neither is a result.</footer>
</div></body></html>'''


def load_reports(pattern=None):
    pattern = pattern or os.path.join(RESULTS_DIR, "*.json")
    out = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                r = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(r, dict) and "n_trades" in r:
            r.setdefault("strategy", os.path.splitext(os.path.basename(path))[0])
            out.append(r)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render backtest results to HTML.")
    ap.add_argument("--results", help="glob for metrics JSON files")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    reports = load_reports(args.results)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(render(reports))

    print(f"\n  {len(reports)} strateg{'y' if len(reports) == 1 else 'ies'} "
          f"-> {args.out}")
    if not reports:
        print("  (no metrics.json found — run a backtest first)")
    print(f"  open {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
