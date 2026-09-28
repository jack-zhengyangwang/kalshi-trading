"""The firm dashboard: every desk's story on one self-contained page.

Rendered from a walk run (`data/walks/<stamp>/`), never from live data, so the
page reads the same whenever it is opened. No server, no build step, no CDN —
charts are hand-drawn SVG, the only script is the crosshair tooltip, and the
page works on a phone and in the dark.

What it shows, top to bottom:
    the firm      leaderboard (benchmarks flagged), every desk's equity on one
                  axis with its manager's changes marked
    each desk     who runs it, what it founded, every manager decision with
                  its reasoning, the agent roster over time, and every
                  contract each agent bought and how the position ended —
                  settled, sold early, or still open when the data stopped

    python3 -m wc.firm.dashboard                       # latest run -> data/walks/<stamp>/firm.html
    python3 -m wc.firm.dashboard --run data/walks/20260921_2210 --out firm.html
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import json
import os
import sys

from wc import paths
from wc.backtest import metrics

RUNS_DIR = os.path.join(paths.ROOT, "data", "walks")
W, H, PAD = 720, 220, 36

# Categorical slots, fixed per desk so a desk keeps its colour on every chart
# and in every filter. Benchmarks are ink, not colour. Validated in both modes.
SLOTS_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4",
               "#008300", "#4a3aa7", "#e34948"]
SLOTS_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181",
              "#008300", "#9085e9", "#e66767"]


def _e(s):
    return html.escape("" if s is None else str(s))


def _ts(datestr):
    return int(dt.datetime.strptime(datestr, "%Y-%m-%d")
               .replace(tzinfo=dt.timezone.utc).timestamp())


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def _money(v):
    if v is None:
        return "—"
    return f"{'−' if v < 0 else '+' if v > 0 else ''}${abs(v):,.2f}"


def _num(v, places=3):
    return "—" if v is None else f"{v:.{places}f}"


# ── loading ───────────────────────────────────────────────────────────────────

def latest_run():
    runs = sorted(d for d in glob.glob(os.path.join(RUNS_DIR, "*"))
                  if os.path.isdir(d) and os.path.exists(os.path.join(d, "leaderboard.json")))
    return runs[-1] if runs else None


def load_run(run_dir):
    with open(os.path.join(run_dir, "leaderboard.json")) as f:
        meta = json.load(f)
    desks = {}
    for path in sorted(glob.glob(os.path.join(run_dir, "pm-*.json"))):
        with open(path) as f:
            d = json.load(f)
        desks[d["report"]["pm"]] = d
    meta["desks"] = desks
    meta["run_dir"] = run_dir
    meta.setdefault("partial", False)
    # colour follows the desk, by name — and never changes because a filter
    # hid a neighbour
    managed = [r["pm"] for r in meta["leaderboard"] if not r["benchmark"]]
    meta["slot"] = {name: i % len(SLOTS_LIGHT) for i, name in enumerate(sorted(managed))}
    return meta


# ── charts ────────────────────────────────────────────────────────────────────

def _scales(xs, ys):
    x0, x1 = min(xs), max(xs)
    lo, hi = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad
    return (lambda x: PAD + (x - x0) / (x1 - x0) * (W - 2 * PAD),
            lambda y: H - PAD + 8 - (y - lo) / (hi - lo) * (H - 2 * PAD),
            x0, x1, lo, hi)


def equity_multi_svg(series, start_ts, end_ts, bankroll):
    """Every desk on ONE axis, from the same bankroll. `series` is
    [(name, css_class, curve, markers)], markers = [(ts, label)]."""
    pts = [(x, y) for _, _, c, _ in series for x, y in c]
    if not pts:
        return '<p class="empty">No closed trades yet.</p>'
    xs = [start_ts, end_ts] + [x for x, _ in pts]
    ys = [bankroll] + [y for _, y in pts]
    px, py, x0, x1, lo, hi = _scales(xs, ys)
    base = py(bankroll)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
           f'aria-label="Equity of every desk on one axis">',
           f'<line x1="{PAD}" y1="{base:.1f}" x2="{W - PAD}" y2="{base:.1f}" class="ref"/>']
    for name, cls, curve, markers in series:
        full = [(start_ts, bankroll)] + list(curve)
        line = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in full)
        out.append(f'<polyline points="{line}" class="eq {cls}"/>')
        for ts, label in markers:
            out.append(f'<line x1="{px(ts):.1f}" y1="{PAD - 6}" x2="{px(ts):.1f}" '
                       f'y2="{H - PAD + 8}" class="mark {cls}"/>')
        x, y = full[-1]
        if px(x) > W - PAD - 90:                 # no room to the right: label ends at the point
            out.append(f'<text x="{px(x) - 4:.1f}" y="{py(y) - 5:.1f}" class="dl end">{_e(name)}</text>')
        else:
            out.append(f'<text x="{px(x) + 4:.1f}" y="{py(y) + 4:.1f}" class="dl">{_e(name)}</text>')
    out.append(f'<text x="{PAD}" y="{H - 8}" class="ax">{_iso(x0)}</text>'
               f'<text x="{W - PAD}" y="{H - 8}" class="ax end">{_iso(x1)}</text>'
               f'<text x="{PAD}" y="16" class="ax">${hi:,.0f}</text>'
               f'<text x="{PAD}" y="{H - PAD + 4}" class="ax">${lo:,.0f}</text>')
    # the crosshair layer reads this
    data = [{"name": n, "cls": c, "points": [(start_ts, bankroll)] + [(x, round(y, 2)) for x, y in cv]}
            for n, c, cv, _ in series]
    out.append(f'<rect class="hit" x="{PAD}" y="{PAD - 6}" width="{W - 2 * PAD}" '
               f'height="{H - 2 * PAD + 14}" data-x0="{x0}" data-x1="{x1}"/>')
    out.append("</svg>")
    out.append(f'<script type="application/json" class="eqdata">{json.dumps(data)}</script>'
               f'<div class="tip" hidden></div>')
    return "".join(out)


def equity_svg(curve, bankroll, changes, start_ts, end_ts, cls):
    """One desk, with its manager's applied changes as hairlines labelled vN."""
    if not curve:
        return '<p class="empty">No closed trades.</p>'
    xs = [start_ts, end_ts] + [x for x, _ in curve]
    ys = [bankroll] + [y for _, y in curve]
    px, py, x0, x1, lo, hi = _scales(xs, ys)
    full = [(start_ts, bankroll)] + list(curve)
    peak, peaks = full[0][1], []
    for x, y in full:
        peak = max(peak, y)
        peaks.append((x, peak))
    band = (" ".join(f"{px(x):.1f},{py(p):.1f}" for x, p in peaks) + " " +
            " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in reversed(full)))
    line = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in full)
    marks = "".join(
        f'<line x1="{px(ts):.1f}" y1="{PAD - 6}" x2="{px(ts):.1f}" y2="{H - PAD + 8}" class="mark {cls}"/>'
        f'<text x="{px(ts) + 3:.1f}" y="{PAD - 8}" class="vl">v{v}</text>'
        for ts, v in changes)
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Equity with manager changes marked">'
            f'<polygon points="{band}" class="dd"/>'
            f'<line x1="{PAD}" y1="{py(bankroll):.1f}" x2="{W - PAD}" y2="{py(bankroll):.1f}" class="ref"/>'
            f'{marks}<polyline points="{line}" class="eq {cls}"/>'
            f'<text x="{PAD}" y="{H - 8}" class="ax">{_iso(x0)}</text>'
            f'<text x="{W - PAD}" y="{H - 8}" class="ax end">{_iso(x1)}</text>'
            f'<text x="{PAD}" y="16" class="ax">${hi:,.0f}</text>'
            f'<text x="{PAD}" y="{H - PAD + 4}" class="ax">${lo:,.0f}</text></svg>')


# ── sections ──────────────────────────────────────────────────────────────────

def _bar(net, slot_cls, span):
    w = 0 if span == 0 else abs(net) / span * 50
    left = 50 - w if net < 0 else 50
    return (f'<div class="bar"><span class="fill {slot_cls}" '
            f'style="left:{left:.1f}%;width:{w:.1f}%"></span><span class="zero"></span></div>')


def render_leaderboard(run):
    rows = run["leaderboard"]
    span = max((abs(r["net_pnl"]) for r in rows), default=0)
    out = ['<section><h2>The firm</h2>',
           f'<p class="sub">{_e(run["start"])} → {_e(run["end"])}, manager tick every '
           f'{run["tick_days"]:g} day(s). Benchmarks never change; every other desk is what '
           f'its manager made of it.</p>',
           '<table class="lb"><thead><tr><th>desk</th><th>manager</th><th class="n">net</th>'
           '<th></th><th class="n">vs baseline</th><th class="n">changes</th><th class="n">agents</th>'
           '<th class="n">trades</th><th class="n">settled</th><th class="n">brier</th>'
           '<th class="n">max DD</th></tr></thead><tbody>']
    for r in rows:
        d = run["desks"].get(r["pm"], {})
        rep = d.get("report", {})
        cls = "bench" if r["benchmark"] else f"s{run['slot'].get(r['pm'], 0) + 1}"
        mgr = rep.get("manager", "none")
        tag = ' <span class="tag">benchmark</span>' if r["benchmark"] else ""
        sign = "pos" if r["net_pnl"] > 0 else "neg" if r["net_pnl"] < 0 else ""
        out.append(
            f'<tr class="{cls}"><td><a href="#desk-{_e(r["pm"])}"><i class="key {cls}"></i>{_e(r["pm"])}</a>{tag}</td>'
            f'<td class="dim">{_e(mgr)}</td>'
            f'<td class="n {sign}">{_money(r["net_pnl"])}</td>'
            f'<td class="barcell">{_bar(r["net_pnl"], cls, span)}</td>'
            f'<td class="n">{_money(r["vs_baseline"])}</td>'
            f'<td class="n">{r["version"] - 1}</td>'
            f'<td class="n">{len(d.get("config", {}).get("agents", []))}</td>'
            f'<td class="n">{r["n_trades"]}</td><td class="n">{r["n_settled"]}</td>'
            f'<td class="n">{_num(r.get("brier"))}</td>'
            f'<td class="n">{_money(rep.get("max_drawdown"))}</td></tr>')
    out.append("</tbody></table>")

    start_ts, end_ts = _ts(run["start"]), _ts(run["end"])
    series = []
    for r in rows:
        d = run["desks"].get(r["pm"])
        if not d:
            continue
        bank = d["report"]["starting_bankroll"]
        curve = metrics.equity_curve(d["trades"], bank)
        cls = "bench" if r["benchmark"] else f"s{run['slot'].get(r['pm'], 0) + 1}"
        markers = [(_ts(x["period"][1]), f"v{x['version_after']}")
                   for x in d["decisions"] if x.get("applied") and x.get("period")]
        series.append((r["pm"], cls, curve, markers))
    bank = run["desks"][rows[0]["pm"]]["report"]["starting_bankroll"] if rows else 500.0
    out.append('<h3>Equity, every desk, one axis</h3>'
               '<p class="sub">Same start, same bankroll. Ticks on a line are the manager '
               'changing that desk. Hover for every desk at a date.</p>'
               '<div class="plot">' + equity_multi_svg(series, start_ts, end_ts, bank) + '</div>')
    out.append('<div class="legend">' + "".join(
        f'<span><i class="key {cls}"></i>{_e(n)}</span>' for n, cls, _, _ in series) + "</div></section>")
    return "".join(out)


def _how_ended(t):
    if t.get("voided"):
        return "voided"
    if t.get("liquidated_at_end"):
        return "open at end"
    if t.get("settled"):
        return "settled"
    return "sold early"


def _agents_of(cfg):
    return [a.get("name") for a in (cfg or {}).get("agents", [])]


def _bucket(p):
    """Entry-price band. Edge lives on this axis, so a loss that sits in one
    band is a different problem from one spread across all of them."""
    for lo, hi in ((0, .15), (.15, .35), (.35, .55), (.55, .75), (.75, 1.01)):
        if lo <= p < hi:
            return f"{int(lo * 100)}–{int(hi * 100)}c"
    return "—"


def _attribution(trades):
    """Where an agent's P&L actually came from.

    Three questions in order: how much of the loss is FEES rather than being
    wrong; was it wrong at settlement or did it sell out early; and is the
    damage concentrated in one league or one price band, or spread evenly —
    concentrated is a fixable rule, spread is a bad view.
    """
    gross = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    won = [t for t in trades if t.get("settled") and t["net_pnl"] > 0]
    lost = [t for t in trades if t.get("settled") and t["net_pnl"] <= 0]
    early = [t for t in trades if not t.get("settled") and not t.get("liquidated_at_end")]
    open_end = [t for t in trades if t.get("liquidated_at_end")]

    def group(key):
        out = {}
        for t in trades:
            k = key(t)
            g = out.setdefault(k, {"n": 0, "net": 0.0})
            g["n"] += 1
            g["net"] += t["net_pnl"]
        return sorted(out.items(), key=lambda kv: kv[1]["net"])

    return {"gross": gross, "fees": fees, "net": gross - fees,
            "won": won, "lost": lost, "early": early, "open_end": open_end,
            "by_series": group(lambda t: t.get("series") or "—")[:6],
            "by_bucket": group(lambda t: _bucket(t["entry_price"])),
            "worst": sorted(trades, key=lambda t: t["net_pnl"])[:5]}


def _trade_rows(trades):
    out = []
    for t in sorted(trades, key=lambda t: t["entry_ts"]):
        s = "pos" if t["net_pnl"] > 0 else "neg" if t["net_pnl"] < 0 else ""
        out.append(f'<tr><td class="mono">{_e(t["ticker"])}</td>'
                   f'<td>{_e(t["side"]).upper()}</td><td class="n">{t["contracts"]}</td>'
                   f'<td class="n">{t["entry_price"]:.2f} <span class="dim">{_iso(t["entry_ts"])}</span></td>'
                   f'<td class="n">{t["exit_price"]:.2f} <span class="dim">{_iso(t["exit_ts"])}</span></td>'
                   f'<td>{_how_ended(t)}</td><td class="n">{_money(-t["fees"]) if t["fees"] else "—"}</td>'
                   f'<td class="n {s}">{_money(t["net_pnl"])}</td></tr>')
    return "".join(out)


def _group_table(title, rows, unit=""):
    body = "".join(
        f'<tr><td>{_e(k)}{unit}</td><td class="n">{v["n"]}</td>'
        f'<td class="n {"pos" if v["net"] > 0 else "neg" if v["net"] < 0 else ""}">'
        f'{_money(v["net"])}</td></tr>' for k, v in rows)
    return (f'<div class="grp"><h5>{title}</h5><table><thead><tr><th></th>'
            f'<th class="n">trades</th><th class="n">net</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def render_agents(d):
    """Who traded on this desk, when they joined and left — and, on click,
    where each one's money went."""
    first = d["decisions"][0] if d["decisions"] and d["decisions"][0].get("applied") \
        and d["decisions"][0].get("founding") else None
    rows = {}
    seed = first["config"] if first else d["config"]
    for name in _agents_of(seed):
        rows[name] = {"added": f"v{first['version_after']}" if first else "v1", "retired": "—"}
    for x in d["decisions"]:
        if not x.get("applied") or x.get("founding"):
            continue
        for c in x.get("changed", []):
            if c.startswith("agent ") and c.endswith(" added"):
                rows.setdefault(c[6:-6], {"added": "—", "retired": "—"})["added"] = f"v{x['version_after']}"
            elif c.startswith("agent ") and "retired" in c:
                nm = c[6:].split(" retired")[0]
                rows.setdefault(nm, {"added": "—", "retired": "—"})["retired"] = f"v{x['version_after']}"
    per = d["report"].get("agents", {})
    for name in per:
        rows.setdefault(name, {"added": "v1", "retired": "—"})

    specs = {}
    for a in d["config"].get("agents", []):
        sp = a.get("spec")
        if isinstance(sp, str):
            try:
                from wc.backtest import spec as spec_mod
                path = sp if os.path.isabs(sp) else os.path.join(paths.ROOT, sp)
                sp = spec_mod.load(path)
            except Exception:
                sp = {}
        specs[a.get("name") or (sp or {}).get("name")] = sp

    desk = d["report"]["pm"]
    out = ['<h3>Agents</h3><p class="sub">Click an agent for every contract it '
           'bought and where its P&amp;L came from.</p>',
           '<div class="agents"><div class="ahead"><span>agent</span><span>joined</span>'
           '<span>retired</span><span class="n">trades</span><span class="n">net</span>'
           '<span>side</span><span>sizing</span></div>']
    for name, r in rows.items():
        mine = [t for t in d["trades"] if t.get("agent") == name]
        net = sum(t["net_pnl"] for t in mine)
        sign = "pos" if net > 0 else "neg" if net < 0 else ""
        s = specs.get(name) if isinstance(specs.get(name), dict) else {}
        head = (f'<summary><span class="an">{_e(name)}</span><span>{r["added"]}</span>'
                f'<span>{r["retired"]}</span><span class="n">{len(mine)}</span>'
                f'<span class="n {sign}">{_money(net)}</span>'
                f'<span>{_e(s.get("side", "—"))}</span>'
                f'<span>{_e((s.get("sizing") or {}).get("method", "—"))}</span></summary>')
        if not mine:
            body = ('<div class="abody"><p class="empty">This agent never traded. Its '
                    'entry conditions were never met on the markets this desk saw.</p></div>')
        else:
            a = _attribution(mine)
            fee_share = (a["fees"] / abs(a["net"]) * 100) if a["net"] else 0
            body = (
                '<div class="abody"><div class="stats">'
                f'<div class="stat"><span class="v">{_money(a["gross"])}</span><span class="l">gross</span></div>'
                f'<div class="stat"><span class="v">{_money(-a["fees"])}</span>'
                f'<span class="l">fees{f" · {fee_share:.0f}% of the loss" if a["net"] < 0 else ""}</span></div>'
                f'<div class="stat"><span class="v {sign}">{_money(a["net"])}</span><span class="l">net</span></div>'
                f'<div class="stat"><span class="v">{len(a["won"])} / {len(a["lost"])}</span>'
                f'<span class="l">won / lost at settlement</span></div>'
                f'<div class="stat"><span class="v">{len(a["early"])}</span>'
                f'<span class="l">sold early</span></div>'
                f'<div class="stat"><span class="v">{len(a["open_end"])}</span>'
                f'<span class="l">open at end</span></div></div>'
                '<div class="grps">'
                + _group_table("by league", a["by_series"])
                + _group_table("by entry price", a["by_bucket"])
                + '</div>'
                '<h5>worst trades</h5><table class="tl"><thead><tr><th>contract</th><th>side</th>'
                '<th class="n">qty</th><th class="n">entry</th><th class="n">exit</th>'
                '<th>ended</th><th class="n">fees</th><th class="n">net</th></tr></thead>'
                f'<tbody>{_trade_rows(a["worst"])}</tbody></table>'
                '<details class="all"><summary>every contract</summary>'
                '<table class="tl"><thead><tr><th>contract</th><th>side</th>'
                '<th class="n">qty</th><th class="n">entry</th><th class="n">exit</th>'
                '<th>ended</th><th class="n">fees</th><th class="n">net</th></tr></thead>'
                f'<tbody>{_trade_rows(mine)}</tbody></table></details></div>')
        out.append(f'<details class="agent" id="agent-{_e(desk)}-{_e(name)}">{head}{body}</details>')
    out.append("</div>")
    return "".join(out)


def render_decisions(d):
    out = ['<h3>Manager decisions</h3>',
           '<table class="dec"><thead><tr><th>tick</th><th>period</th><th class="n">tick net</th>'
           '<th class="n">vs baseline</th><th class="n">noise</th><th>read</th><th>decision</th>'
           '<th>what changed</th></tr></thead><tbody>']
    for x in d["decisions"]:
        b = x.get("brief") or {}
        this = b.get("this_tick") or {}
        var = b.get("variance") or {}
        bench = (b.get("benchmarks") or {}).get("baseline-market") or {}
        vs = ((this.get("net_pnl") or 0) - (bench.get("this_tick_net") or 0)) if this else None
        a = x.get("assessment") or {}
        read = a.get("variance_or_structure") or "—"
        if x.get("founding"):
            period = "founding" + (f" (attempt {x['attempt']})" if x.get("attempt", 1) > 1 else "")
        else:
            period = f'{x["period"][0]} → {x["period"][1]}' if x.get("period") else "—"
        if x.get("applied"):
            verdict = f'<b class="chg">change → v{x["version_after"]}</b>'
        elif x.get("rejected"):
            verdict = f'<b class="rej">rejected</b> <span class="dim">{_e(x["rejected"])[:160]}</span>'
        else:
            verdict = "hold"
        changed = ", ".join(x.get("changed") or []) or "—"
        noise = var.get("this_tick_in_noise_units")
        diag = _e(a.get("diagnosis") or "")
        out.append(
            f'<tr><td>{x["tick"]}</td><td>{_e(period)}</td>'
            f'<td class="n">{_money(this.get("net_pnl")) if this else "—"}</td>'
            f'<td class="n">{_money(vs) if vs is not None else "—"}</td>'
            f'<td class="n">{(_num(noise, 2) + "σ") if noise is not None else "—"}</td>'
            f'<td>{_e(read)}</td><td>{verdict}</td><td>{_e(changed)}</td></tr>'
            f'<tr class="why"><td></td><td colspan="7"><details><summary>reasoning</summary>'
            f'{("<p class=dim>" + diag + "</p>") if diag else ""}<p>{_e(x.get("reasoning"))}</p></details></td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def render_fills(d):
    """Every contract each agent bought, grouped by the tick it was bought in,
    and how the position ended."""
    ticks = d.get("ticks") or []
    bounds = [(t["tick"], _ts(t["period"][0]), max(_ts(t["period"][1]), _ts(t["period"][0]) + 86400))
              for t in ticks if t.get("period")]
    groups = {}
    for t in sorted(d["trades"], key=lambda t: t["entry_ts"]):
        key = next((i for i, a, b in bounds if a <= t["entry_ts"] < b), None)
        groups.setdefault(key, []).append(t)
    if not d["trades"]:
        return '<h3>Contracts</h3><p class="empty">No fills.</p>'
    out = ['<h3>Contracts</h3><p class="sub">What each agent bought, and whether the position '
           'was held to settlement, sold early, or still open when the data stopped.</p>']
    for key in sorted(groups, key=lambda k: (k is None, k or 0)):
        ts = groups[key]
        label = f"tick {key}" if key is not None else "outside any tick"
        per = next((f'{x["period"][0]} → {x["period"][1]}' for x in ticks if x["tick"] == key), "")
        net = sum(t["net_pnl"] for t in ts)
        sign = "pos" if net > 0 else "neg" if net < 0 else ""
        out.append(f'<details class="fills"><summary><b>{_e(label)}</b> <span class="dim">{_e(per)}</span> '
                   f'· {len(ts)} fills · <span class="{sign}">{_money(net)}</span></summary>'
                   '<table><thead><tr><th>agent</th><th>contract</th><th>side</th><th class="n">qty</th>'
                   '<th class="n">entry</th><th class="n">exit</th><th>ended</th><th class="n">fees</th>'
                   '<th class="n">net</th></tr></thead><tbody>')
        for t in ts:
            s = "pos" if t["net_pnl"] > 0 else "neg" if t["net_pnl"] < 0 else ""
            out.append(f'<tr><td>{_e(t.get("agent"))}</td><td class="mono">{_e(t["ticker"])}</td>'
                       f'<td>{_e(t["side"]).upper()}</td><td class="n">{t["contracts"]}</td>'
                       f'<td class="n">{t["entry_price"]:.2f} <span class="dim">{_iso(t["entry_ts"])}</span></td>'
                       f'<td class="n">{t["exit_price"]:.2f} <span class="dim">{_iso(t["exit_ts"])}</span></td>'
                       f'<td>{_how_ended(t)}</td><td class="n">{_money(-t["fees"]) if t["fees"] else "—"}</td>'
                       f'<td class="n {s}">{_money(t["net_pnl"])}</td></tr>')
        out.append("</tbody></table></details>")
    return "".join(out)


def render_desk(run, name):
    d = run["desks"][name]
    rep = d["report"]
    bench = rep.get("is_benchmark")
    cls = "bench" if bench else f"s{run['slot'].get(name, 0) + 1}"
    cfg = d["config"]
    view = cfg.get("view", {})
    founding = next((x for x in d["decisions"] if x.get("founding") and x.get("applied")), None)
    not_founded = any(x.get("founding") for x in d["decisions"]) and founding is None
    start_ts, end_ts = _ts(run["start"]), _ts(run["end"])
    curve = metrics.equity_curve(d["trades"], rep["starting_bankroll"])
    changes = [(_ts(x["period"][1]), x["version_after"]) for x in d["decisions"]
               if x.get("applied") and x.get("period")]
    sign = "pos" if rep["net_pnl"] > 0 else "neg" if rep["net_pnl"] < 0 else ""
    params = (" " + _e(json.dumps(view.get("params")))) if view.get("params") else ""

    out = [f'<section id="desk-{_e(name)}" class="desk {cls}">',
           f'<h2><i class="key {cls}"></i>{_e(name)}'
           f'{" <span class=tag>benchmark</span>" if bench else ""}</h2>',
           f'<p class="sub">{_e(rep.get("manager", "none"))} · view <b>{_e(view.get("view"))}</b>{params}'
           f' · v{d["version"]} · {len(cfg.get("agents", []))} agent(s)</p>',
           '<div class="stats">',
           f'<div class="stat"><span class="v {sign}">{_money(rep["net_pnl"])}</span><span class="l">net</span></div>',
           f'<div class="stat"><span class="v">{_money(rep["gross_pnl"])}</span><span class="l">gross</span></div>',
           f'<div class="stat"><span class="v">{_money(-rep["fees"])}</span><span class="l">fees</span></div>',
           f'<div class="stat"><span class="v">{rep["n_trades"]}</span><span class="l">trades · {rep["n_settled"]} settled</span></div>',
           f'<div class="stat"><span class="v">{_num(rep.get("brier"))}</span><span class="l">brier</span></div>',
           f'<div class="stat"><span class="v">{_money(rep.get("max_drawdown"))}</span><span class="l">max drawdown</span></div>',
           '</div>']
    if rep.get("lookahead_risk"):
        out.append(f'<p class="warn">⚠ {_e(rep["lookahead_risk"])} — ranking only, not evidence.</p>')
    if founding:
        out.append('<h3>Founding</h3><blockquote>' + _e(founding.get("reasoning")) + '</blockquote>')
    elif not_founded:
        n = sum(1 for x in d["decisions"] if x.get("founding"))
        out.append(f'<p class="warn">⚠ The manager could not produce a runnable desk in {n} '
                   'attempts; it ran the placeholder.</p>')
    out.append('<div class="plot">' + equity_svg(curve, rep["starting_bankroll"], changes,
                                                  start_ts, end_ts, cls) + '</div>')
    if not bench:
        out.append(render_decisions(d))
    out.append(render_agents(d))
    out.append(render_fills(d))
    out.append("</section>")
    return "".join(out)


# ── page ──────────────────────────────────────────────────────────────────────

CSS = """
:root{color-scheme:light;
 --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
 --pos:#006300;--neg:#d03b3b;--warnbg:#fff7e6;--warn:#7a5b12;--tipbg:#fcfcfb;
 --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;--s8:#e34948;
 --bench:#898781}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
 --pos:#0ca30c;--neg:#e66767;--warnbg:#2a2410;--warn:#fab219;--tipbg:#1a1a19;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
 --pos:#0ca30c;--neg:#e66767;--warnbg:#2a2410;--warn:#fab219;--tipbg:#1a1a19;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
h3{font-size:14px;margin:22px 0 6px;color:var(--ink2)}
.sub{color:var(--ink2);margin:0 0 12px}.dim{color:var(--muted)}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 18px 14px;margin:0 0 18px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--border);border-radius:10px;padding:1px 7px;margin-left:6px;font-weight:400}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle;background:var(--bench)}
.key.s1{background:var(--s1)}.key.s2{background:var(--s2)}.key.s3{background:var(--s3)}.key.s4{background:var(--s4)}
.key.s5{background:var(--s5)}.key.s6{background:var(--s6)}.key.s7{background:var(--s7)}.key.s8{background:var(--s8)}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:6px 0 0;font-size:13px;color:var(--ink2)}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
.barcell{width:160px;padding:6px 4px}.bar{position:relative;height:10px}
.bar .zero{position:absolute;left:50%;top:-2px;width:1px;height:14px;background:var(--axis)}
.bar .fill{position:absolute;top:2px;height:6px;border-radius:3px;background:var(--bench)}
.fill.s1{background:var(--s1)}.fill.s2{background:var(--s2)}.fill.s3{background:var(--s3)}.fill.s4{background:var(--s4)}.fill.s5{background:var(--s5)}
.plot{position:relative;margin:8px 0}
.chart{width:100%;height:auto;display:block}
.chart .eq{fill:none;stroke:var(--bench);stroke-width:2;stroke-linejoin:round}
.chart .eq.s1{stroke:var(--s1)}.chart .eq.s2{stroke:var(--s2)}.chart .eq.s3{stroke:var(--s3)}.chart .eq.s4{stroke:var(--s4)}.chart .eq.s5{stroke:var(--s5)}
.chart .eq.bench{opacity:.7}
.chart .dd{fill:var(--neg);opacity:.08}.chart .ref{stroke:var(--axis);stroke-width:1}
.chart .mark{stroke:var(--bench);stroke-width:1;opacity:.55}
.chart .mark.s1{stroke:var(--s1)}.chart .mark.s2{stroke:var(--s2)}.chart .mark.s3{stroke:var(--s3)}.chart .mark.s4{stroke:var(--s4)}.chart .mark.s5{stroke:var(--s5)}
.chart .ax{font-size:11px;fill:var(--muted)}.chart .ax.end{text-anchor:end}.chart .dl{font-size:11px;fill:var(--ink2)}.chart .dl.end{text-anchor:end}
.chart .vl{font-size:10px;fill:var(--muted)}
.chart .hit{fill:transparent;cursor:crosshair}.chart .xh{stroke:var(--ink2);stroke-width:1;pointer-events:none}
.tip{position:absolute;pointer-events:none;background:var(--tipbg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);min-width:150px;z-index:2}
.tip b{font-variant-numeric:tabular-nums}.tip div{display:flex;justify-content:space-between;gap:12px}.tip .d{color:var(--muted);margin-bottom:2px}
.stats{display:flex;flex-wrap:wrap;gap:8px 22px;margin:8px 0 4px}
.stat{display:flex;flex-direction:column}.stat .v{font-size:20px;font-weight:600}.stat .l{font-size:11px;color:var(--muted)}
blockquote{margin:6px 0;padding:8px 12px;border-left:3px solid var(--grid);color:var(--ink2);white-space:pre-wrap}
.warn{background:var(--warnbg);color:var(--warn);padding:8px 12px;border-radius:6px}
.dec tr.why td{padding-top:0}
details summary{cursor:pointer;color:var(--ink2);font-size:12px}details p{margin:6px 0;white-space:pre-wrap}
details.fills{margin:8px 0}details.fills summary{font-size:13px;color:var(--ink)}details.fills table{margin-top:6px}
.agents{font-size:13px}
.ahead,.agent>summary{display:grid;grid-template-columns:2fr .7fr .7fr .6fr .8fr .6fr .8fr;gap:8px;padding:6px 8px;align-items:center}
.ahead{color:var(--muted);font-size:12px;border-bottom:1px solid var(--grid)}
.agent{border-bottom:1px solid var(--grid)}
.agent>summary{cursor:pointer;list-style:none}
.agent>summary::-webkit-details-marker{display:none}
.agent>summary:hover{background:var(--page)}
.agent>summary .an::before{content:"▸\00a0";color:var(--muted)}
.agent[open]>summary .an::before{content:"▾\00a0";color:var(--muted)}
.agent .n{text-align:right;font-variant-numeric:tabular-nums}
.abody{padding:4px 8px 14px;background:var(--page);border-radius:8px;margin:0 0 8px}
.abody .stats{gap:8px 20px}.abody .stat .v{font-size:16px}
.grps{display:flex;flex-wrap:wrap;gap:8px 24px;margin:10px 0}
.grp{min-width:220px;flex:1}.grp h5,.abody h5{font-size:12px;color:var(--muted);margin:10px 0 2px;font-weight:500}
.tl{font-size:12px}
details.all{margin-top:8px}
.live{color:var(--warn);background:var(--warnbg);padding:8px 12px;border-radius:6px;margin:0 0 12px}
.chg{color:var(--pos)}.rej{color:var(--neg)}.empty{color:var(--muted)}
footer{color:var(--muted);font-size:12px;margin-top:12px}
@media (max-width:600px){.barcell{display:none}.stat .v{font-size:17px}}
"""

JS = """
(function(){
  document.querySelectorAll('.plot').forEach(function(plot){
    var svg=plot.querySelector('svg'), hit=plot.querySelector('.hit'),
        dataEl=plot.querySelector('.eqdata'), tip=plot.querySelector('.tip');
    if(!svg||!hit||!dataEl||!tip) return;
    var series=JSON.parse(dataEl.textContent), x0=+hit.dataset.x0, x1=+hit.dataset.x1;
    var W=svg.viewBox.baseVal.width, PAD=parseFloat(hit.getAttribute('x'));
    var xh=document.createElementNS('http://www.w3.org/2000/svg','line');
    xh.setAttribute('class','xh'); xh.setAttribute('y1',hit.getAttribute('y'));
    xh.setAttribute('y2',+hit.getAttribute('y')+ +hit.getAttribute('height')); xh.style.display='none';
    svg.insertBefore(xh,hit);
    var start=series.length?series[0].points[0][1]:0;
    function at(pts,x){var v=pts[0][1];for(var i=0;i<pts.length;i++){if(pts[i][0]<=x)v=pts[i][1];else break;}return v;}
    function money(v){return (v<0?'\\u2212':v>0?'+':'')+'$'+Math.abs(v).toFixed(2);}
    function move(ev){
      var r=svg.getBoundingClientRect(), fx=(ev.clientX-r.left)/r.width*W;
      var x=x0+(fx-PAD)/(W-2*PAD)*(x1-x0); x=Math.max(x0,Math.min(x1,x));
      var px=PAD+(x-x0)/(x1-x0)*(W-2*PAD);
      xh.setAttribute('x1',px); xh.setAttribute('x2',px); xh.style.display='';
      while(tip.firstChild) tip.removeChild(tip.firstChild);
      var d=document.createElement('div'); d.className='d';
      d.textContent=new Date(x*1000).toISOString().slice(0,10); tip.appendChild(d);
      series.map(function(s){return [s,at(s.points,x)];}).sort(function(a,b){return b[1]-a[1];})
        .forEach(function(p){var row=document.createElement('div'),k=document.createElement('i'),n=document.createElement('span'),v=document.createElement('b');
          k.className='key '+p[0].cls; n.appendChild(k); n.appendChild(document.createTextNode(p[0].name));
          v.textContent=money(p[1]-start); row.appendChild(n); row.appendChild(v); tip.appendChild(row);});
      tip.hidden=false; var left=(ev.clientX-r.left)+14; if(left+170>r.width) left=(ev.clientX-r.left)-184;
      tip.style.left=left+'px'; tip.style.top=Math.max(0,(ev.clientY-r.top)-10)+'px';
    }
    hit.addEventListener('pointermove',move);
    hit.addEventListener('pointerleave',function(){tip.hidden=true; xh.style.display='none';});
  });
})();
"""


def render(run, generated=None):
    generated = generated or dt.datetime.now(dt.timezone.utc)
    order = [r["pm"] for r in run["leaderboard"] if r["pm"] in run["desks"]]
    body = render_leaderboard(run) + "".join(render_desk(run, n) for n in order)
    where = run.get("run_dir", "")
    if where and os.path.abspath(where).startswith(paths.ROOT):
        where = os.path.relpath(where, paths.ROOT)
    # A run still going reloads itself; a finished one never does, so a saved
    # report reads the same whenever it is opened and is never mistaken for
    # a snapshot of something that has since moved on.
    partial = bool(run.get("partial"))
    refresh = ('<meta http-equiv="refresh" content="30">' if partial else "")
    stamp = (f'<p class="live">● Run in progress — this page reloads every 30s. '
             f'Last written {generated.strftime("%H:%M UTC")}; the desks below have '
             f'traded up to here, and later ticks are still to come.</p>' if partial else
             f'<p class="sub">Generated {generated.strftime("%Y-%m-%d %H:%M UTC")} from '
             f'<code>{_e(where)}</code> — no live data, so this page reads the same '
             f'whenever it is opened.</p>')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">{refresh}
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>The firm</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>The firm</h1>
{stamp}
{body}
<footer>Every number here is also in the tables; the charts only add shape. A desk carrying
lookahead is ranking evidence only. Paper money throughout.</footer>
</div><script>{JS}</script></body></html>'''


def watch(run_dir, out, interval=30, on_render=None):
    """Re-render until the run says it is finished. Returns the number of
    renders. The final pass is the one with no refresh header, so a tab left
    open lands on the finished report rather than a page that keeps polling."""
    import time
    n = 0

    def write(run):
        nonlocal n
        with open(out, "w") as f:
            f.write(render(run))
        n += 1

    while True:
        run = load_run(run_dir)
        write(run)
        if on_render:
            on_render()
        fresh = load_run(run_dir)
        if not fresh.get("partial"):
            # The last page written is always the finished report, never one
            # still claiming to be in progress.
            if run.get("partial"):
                write(fresh)
            return n
        if interval:
            time.sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a walk run to one HTML page.")
    ap.add_argument("--run", help="walk run directory (default: the latest under data/walks/)")
    ap.add_argument("--out", help="output HTML (default: <run>/firm.html)")
    ap.add_argument("--watch", action="store_true",
                    help="re-render every 30s until the run finishes; the page "
                         "reloads itself while it is in progress")
    args = ap.parse_args(argv)

    run_dir = args.run or latest_run()
    if not run_dir or not os.path.exists(os.path.join(run_dir, "leaderboard.json")):
        print("[dashboard] no walk run found — run python3 -m wc.firm.walk first", file=sys.stderr)
        return 2
    run = load_run(run_dir)
    out = args.out or os.path.join(run_dir, "firm.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if args.watch:
        print(f"  watching {run_dir} -> {out}\n  open {out}   (the page reloads itself)\n")
        n = watch(run_dir, out)
        print(f"  run finished; {n} renders. Final report at {out}")
        return 0
    with open(out, "w") as f:
        f.write(render(run))
    state = "IN PROGRESS" if run.get("partial") else "complete"
    print(f"\n  {len(run['desks'])} desks ({state}) -> {out}\n  open {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
