"""Walk-forward: desks that wake up every tick, managers that decide between.

The single-pass backtest (wc/firm/run.py) replays everything at once and the
desk never changes. This runner replays the same engine in TICKS — two days
in backtest, a week in the arena — and between ticks hands each desk's
manager a brief and applies what the manager decides. Positions, bankroll and
the journal carry across ticks; only the definition changes.

    for tick in windows(start, end, tick_days):
        bars = load(tick)                     # one tick in memory, never more
        for desk in benchmarks + managed:
            desk.view.price(bars) -> session.run(bars)
            brief = what happened this tick, cumulatively, vs benchmarks,
                    vs last tick, with a noise band, and the decision log
            decision = desk.manager.decide(brief)    # HoldManager for benchmarks
            apply(decision) or reject-and-log
    finalize -> report per desk, leaderboard, every decision

Loading a tick at a time is what makes the whole archive fit on the droplet.

Two invariants the runner enforces on a manager's config, because the manager
is otherwise unrestricted: the name and the bankroll do not change. Capital is
per agent; when the roster changes, free cash is re-split equally among the
agents that remain and positions stay with the agent that opened them.

Usage:
    python3 -m wc.firm.walk --tick-days 2 --start 2026-07-10
    python3 -m wc.firm.walk --tick-days 7 --pm elo-desk draw-desk --no-manager
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import time

from wc import paths
from wc.backtest import data, engine, metrics
from wc.firm import journal as journal_mod
from wc.firm import manager as mgr_mod
from wc.firm import pm as pm_mod

DAY = 86400
RESULTS_DIR = os.path.join(paths.ROOT, "data", "walks")


def _ts(datestr):
    return int(dt.datetime.strptime(datestr, "%Y-%m-%d")
               .replace(tzinfo=dt.timezone.utc).timestamp())


def _date(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def _disabled_entry():
    """An entry no bar satisfies: how a retired agent winds down instead of
    being liquidated — its positions still exit by its own rules."""
    return {"all": [{"signal": "price", "op": "lt", "value": 0.0}]}


class Desk:
    """One desk across the whole walk: its definition, its engine session, its
    manager, and everything the manager decided."""

    def __init__(self, cfg, costs, manager=None, memory_days=7):
        self.pm = pm_mod.build(cfg)
        self.cfg = self.pm.cfg
        self.name = self.pm.name
        self.costs = costs
        self.manager = manager if manager is not None \
            else mgr_mod.build(self.pm.manager_spec)
        self.is_benchmark = isinstance(self.manager, mgr_mod.HoldManager)
        self.view = self.pm.view
        self.journal = journal_mod.Journal(self.name) if self.pm.learns else None
        self.books = {a.name: engine.Book(a.name, a.spec, a.bankroll,
                                          journal=self.journal)
                      for a in self.pm.agents}
        self.session = engine.Session(list(self.books.values()), costs=costs,
                                      pm_caps=self.pm.caps)
        self.session.view_forget = lambda keep: self.view.forget(keep)
        self.decisions = []
        self.ticks = []                 # per-tick summaries, in order
        self._all_trades = []
        # How much of the log the manager sees in full. Everything older is
        # one line each, plus whatever the manager wrote into `notes`.
        self.memory_days = float(memory_days)
        self.tick_days = 2.0                      # set by run_walk
        self.notes = None
        self.last_rejected = None

    # ── one tick ─────────────────────────────────────────────────────────

    def run_tick(self, bars, t1=None):
        probs = self.view.price(bars)
        self.session.run(bars, model_probs=probs)
        if t1 is not None:
            self.session.end_tick(t1 - 1)    # settle, release, forget
        new = self.session.new_trades()
        self._all_trades.extend(new)
        self._last_entries = self.session.new_entries()
        return new

    def equity(self):
        cash = sum(b.bankroll for b in self.books.values())
        held = sum(p.cost for b in self.books.values() for p in b.positions.values())
        return cash, held

    def tick_summary(self, i, t0, t1, new):
        rep = metrics.summarize(new, self.pm.bankroll, label=f"tick {i}")
        keep = ("n_trades", "n_settled", "gross_pnl", "fees", "net_pnl",
                "win_rate", "brier", "max_drawdown")
        out = {k: rep.get(k) for k in keep}
        out["agents"] = {}
        for name in self.books:
            own = [t for t in new if t.get("agent") == name]
            out["agents"][name] = {"n_trades": len(own),
                                   "net_pnl": round(sum(t["net_pnl"] for t in own), 4)}
        out["tick"], out["period"] = i, [_date(t0), _date(t1)]
        # Opened vs closed, said explicitly: `n_trades` counts positions CLOSED
        # this tick. A tick that opened twenty and closed none is busy, not idle.
        entries = getattr(self, "_last_entries", [])
        out["positions_opened"] = len(entries)
        out["opened_cost"] = round(sum(e["cost"] for e in entries), 2)
        out["positions_closed"] = len(new)
        out["positions_settled"] = out["n_settled"]
        out["positions_sold_early"] = sum(1 for t in new if not t.get("settled"))
        opens = self.session.open_positions()
        out["open_positions"] = len(opens)
        out["open_cost"] = round(sum(p["cost"] for p in opens), 2)
        out["unrealized"] = round(sum(p["unrealized"] or 0 for p in opens), 2)
        cash, held = self.equity()
        out["cash"], out["in_positions"] = round(cash, 2), round(held, 2)
        out["version"] = self.pm.version
        return out

    def cumulative(self):
        rep = metrics.summarize(self._all_trades, self.pm.bankroll, label="to date")
        keep = ("n_trades", "n_settled", "gross_pnl", "fees", "net_pnl",
                "return_pct", "win_rate", "brier", "max_drawdown",
                "max_drawdown_pct", "sharpe")
        return {k: rep.get(k) for k in keep}

    def variance(self, new):
        nets = [t["net_pnl"] for t in self._all_trades]
        std = statistics.pstdev(nets) if len(nets) > 1 else None
        n = len(new)
        band = (std * math.sqrt(n)) if (std is not None and n) else None
        net = sum(t["net_pnl"] for t in new)
        return {"per_trade_std": None if std is None else round(std, 4),
                "n_this_tick": n,
                "noise_band": None if band is None else round(band, 4),
                "this_tick_in_noise_units": (None if not band else round(net / band, 3)),
                "note": "a tick inside ±1 noise band is what luck alone produces"}

    # ── founding: the manager designs the desk before the first bar ──────

    FOUNDING_ATTEMPTS = 3

    def found(self, universe, benchmarks):
        """Ask the manager to design the desk. Up to FOUNDING_ATTEMPTS tries,
        each told why the last was refused; if none is runnable the desk keeps
        its placeholder and the log says so."""
        material = mgr_mod.founding_material()
        rejected = None
        for attempt in range(1, self.FOUNDING_ATTEMPTS + 1):
            brief = {
                "desk": self.name, "tick": 0, "founding": True, "attempt": attempt,
                "bankroll": self.pm.bankroll, "caps": self.pm.caps,
                "manager": self.manager.describe(),
                "placeholder": self.cfg,
                "universe": universe, "benchmarks": benchmarks,
                "previous_attempt_rejected": rejected,
                "notes": self.notes,
                **material,
            }
            entry = {"tick": 0, "period": None, "ts": int(time.time()),
                     "founding": True, "attempt": attempt,
                     "version_before": self.pm.version,
                     "manager": self.manager.describe(), "brief": None}
            try:
                decision = self.manager.decide(brief)
            except Exception as e:
                decision = mgr_mod.Decision("hold", f"manager failed: {e}",
                                            {"rejected": f"manager failed: {e}"})
            entry.update({"action": decision.action, "reasoning": decision.reasoning,
                          "assessment": decision.assessment, "applied": False,
                          "rejected": decision.assessment.get("rejected"),
                          "changed": []})
            if decision.notes is not None:
                self.notes = decision.notes
            entry["notes"] = self.notes
            if decision.action == "change":
                try:
                    entry["changed"] = self.apply(decision)
                    entry["applied"] = True
                    self.cfg["founding"] = False
                    self.pm.cfg["founding"] = False
                    entry["config"] = self.cfg
                except (mgr_mod.ManagerError, pm_mod.PMError, TypeError) as e:
                    entry["rejected"] = str(e)
                    entry["proposed"] = decision.config
            elif not entry["rejected"]:
                entry["rejected"] = "a founding decision must be a change"
            entry["version_after"] = self.pm.version
            self.decisions.append(entry)
            if entry["applied"]:
                return entry
            rejected = entry["rejected"]
        return self.decisions[-1]

    # ── the manager ──────────────────────────────────────────────────────

    def brief(self, i, t0, t1, new, benchmarks):
        this = self.tick_summary(i, t0, t1, new)
        return {
            "desk": self.name, "tick": i, "period": [_date(t0), _date(t1)],
            "version": self.pm.version,
            "config": self.cfg,
            "manager": self.manager.describe(),
            "this_tick": this,
            "previous_tick": self.ticks[-1] if self.ticks else None,
            "cumulative": self.cumulative(),
            "benchmarks": benchmarks,
            "open_positions": self.session.open_positions(),
            "journal": self.journal.summary() if self.journal is not None else None,
            "variance": self.variance(new),
            "notes": self.notes,
            # Why the last change was refused. Without this a manager repeats
            # the same invalid config forever — one desk spent forty ticks
            # trying to rename itself.
            "last_change_rejected": self.last_rejected,
            **self.memory(i),
        }

    def memory(self, tick):
        """The decision log, bounded. The last `memory_days` of ticks in full;
        everything older as one line each, with the prose dropped.

        Without this the brief grows every tick and a run pays quadratically
        for its own history — the single largest cost in a long walk, and the
        one thing that could overflow a manager's context window.
        """
        per_tick = max(1, round(self.tick_days))
        window = max(1, int(self.memory_days / per_tick))
        recent, earlier = [], []
        for d in self.decisions:
            if d["tick"] > tick - window:
                recent.append({k: d[k] for k in ("tick", "action", "reasoning",
                                                 "assessment", "applied",
                                                 "rejected", "changed")})
            else:
                earlier.append({
                    "tick": d["tick"], "action": d["action"],
                    "changed": ", ".join(d.get("changed") or []) or None,
                    "applied": d.get("applied"),
                    "rejected": (d.get("rejected") or "")[:80] or None,
                    "net": ((d.get("brief") or {}).get("this_tick") or {}).get("net_pnl")})
        # The founding decision scrolls out like any other. What the desk set
        # out to do, and why, is the manager's to keep in `notes` — the brief
        # does not hold beliefs on its behalf.
        digest = {"ticks": earlier,
                  "summary": {"decisions": len(earlier),
                              "changes_applied": sum(1 for x in earlier if x["applied"]),
                              "rejected": sum(1 for x in earlier if x["rejected"]),
                              "net": round(sum(x["net"] or 0 for x in earlier), 2),
                              "note": ("older than the quoted window; prose dropped. "
                                       "What matters from here is in `notes`.")}}
        return {"decisions": recent, "earlier": digest}

    def apply(self, decision):
        """Validate and apply a manager's config. Raises ManagerError with the
        reason when it cannot be run; the caller logs either way."""
        new = decision.config
        if new.get("name") != self.name:
            raise mgr_mod.ManagerError("the desk's name may not change")
        if new.get("bankroll") != self.pm.bankroll:
            raise mgr_mod.ManagerError(
                f"bankroll must stay {self.pm.bankroll}; a manager cannot fund itself")
        new = json.loads(json.dumps(new))
        new["version"] = self.pm.version + 1
        new.setdefault("manager", self.cfg.get("manager", {"type": "none"}))
        candidate = pm_mod.build(new, where="manager decision")   # validates everything

        changed = []
        # the view: same method -> reconfigure and keep what it learned;
        # a different method starts cold, and the log says so.
        old_v, new_v = self.cfg["view"], new["view"]
        if new_v.get("view") == old_v.get("view"):
            if (new_v.get("params") or {}) != (old_v.get("params") or {}):
                self.view.reconfigure(new_v.get("params") or {})
                changed.append("view.params")
        else:
            self.view = candidate.view
            changed.append(f"view {old_v.get('view')} -> {new_v.get('view')} (cold start)")

        # the agents: keep books that stay, wind down the retired, start the new
        wanted = {a.name: a for a in candidate.agents}
        for name, book in self.books.items():
            if name in wanted:
                if book.spec != wanted[name].spec:
                    book.spec = wanted[name].spec
                    changed.append(f"agent {name} spec")
            elif book.spec.get("entry") != _disabled_entry():
                book.spec = dict(book.spec, entry=_disabled_entry())
                changed.append(f"agent {name} retired (winding down)")
        for name, a in wanted.items():
            if name not in self.books:
                self.books[name] = engine.Book(name, a.spec, 0.0, journal=self.journal)
                changed.append(f"agent {name} added")
        if any(c.endswith("retired (winding down)") or c.endswith("added") for c in changed):
            self._resplit_cash(set(wanted))
        self.session.books = list(self.books.values())

        if (new.get("caps") or {}) != (self.cfg.get("caps") or {}):
            self.session.pm_caps = new.get("caps") or {}
            changed.append("caps")

        # The desk describes itself in its manager's words: the founding
        # reasoning first, then every applied change, stamped with its version.
        prior = self.cfg.get("description", "") if not self.cfg.get("founding") else ""
        stamp = (decision.reasoning if not prior
                 else f"{prior}\n\n[v{new['version']}] {decision.reasoning}")
        candidate.cfg["description"] = stamp
        candidate.description = stamp

        self.pm = candidate
        self.pm.view = self.view
        self.cfg = candidate.cfg
        return changed

    def _resplit_cash(self, active):
        """Free cash goes equally to the agents that remain; positions stay
        where they are. Total capital is conserved."""
        pool = sum(b.bankroll for b in self.books.values())
        live = [self.books[n] for n in active]
        for b in self.books.values():
            b.bankroll = 0.0
        for b in live:
            b.bankroll = pool / len(live)

    def decide(self, i, t0, t1, new, benchmarks):
        if self.is_benchmark:               # a fixed desk has nothing to decide
            self.ticks.append(self.tick_summary(i, t0, t1, new))
            return None
        brief = self.brief(i, t0, t1, new, benchmarks)
        entry = {"tick": i, "period": brief["period"], "ts": int(time.time()),
                 "version_before": self.pm.version, "manager": self.manager.describe(),
                 "brief": {k: brief[k] for k in ("this_tick", "cumulative",
                                                 "benchmarks", "variance")}}
        try:
            decision = self.manager.decide(brief)
        except Exception as e:                      # a manager that crashes holds
            decision = mgr_mod.Decision("hold", f"manager failed: {e}",
                                        {"rejected": f"manager failed: {e}"})
        entry.update({"action": decision.action, "reasoning": decision.reasoning,
                      "assessment": decision.assessment, "applied": False,
                      "rejected": decision.assessment.get("rejected"),
                      "changed": []})
        if decision.notes is not None:
            self.notes = decision.notes
        entry["notes"] = self.notes
        if decision.action == "change":
            try:
                entry["changed"] = self.apply(decision)
                entry["applied"] = True
                entry["config"] = self.cfg
            except (mgr_mod.ManagerError, pm_mod.PMError, TypeError) as e:
                entry["rejected"] = str(e)
                entry["proposed"] = decision.config
        self.last_rejected = entry["rejected"] if not entry["applied"] else None
        entry["version_after"] = self.pm.version
        self.decisions.append(entry)
        self.ticks.append(brief["this_tick"])
        return entry

    # ── end of data ──────────────────────────────────────────────────────

    def snapshot(self):
        """The desk as it stands, mid-walk: closed trades only, session left
        running. Same shape as finish(), so the dashboard reads either."""
        trades = list(self.session.trades)
        report = metrics.summarize(trades, self.pm.bankroll, dict(self.session.rejections),
                                   label=f"PM {self.name}")
        report["pm"] = report["strategy"] = self.name
        report["view"] = self.view.describe()
        report["manager"] = self.manager.describe()
        report["version"] = self.pm.version
        report["is_benchmark"] = self.is_benchmark
        report["agents"] = {name: {"n_trades": sum(1 for t in trades if t.get("agent") == name),
                                   "net_pnl": round(sum(t["net_pnl"] for t in trades
                                                        if t.get("agent") == name), 4)}
                            for name in self.books}
        if self.journal is not None:
            report["journal"] = self.journal.summary()
        return {"report": report, "decisions": self.decisions, "ticks": self.ticks,
                "trades": trades, "config": self.cfg, "version": self.pm.version,
                "notes": self.notes}

    def finish(self):
        trades, rej = self.session.finalize()
        tail = self.session.new_trades()
        self._all_trades.extend(tail)
        report = metrics.summarize(trades, self.pm.bankroll, rej,
                                   label=f"PM {self.name}")
        report["pm"] = report["strategy"] = self.name
        report["view"] = self.view.describe()
        report["manager"] = self.manager.describe()
        report["version"] = self.pm.version
        report["is_benchmark"] = self.is_benchmark
        report["agents"] = {}
        for name, book in self.books.items():
            own = [t for t in trades if t.get("agent") == name]
            report["agents"][name] = {"n_trades": len(own),
                                      "net_pnl": round(sum(t["net_pnl"] for t in own), 4)}
        if self.journal is not None:
            report["journal"] = self.journal.summary()
        if self.pm.carries_lookahead:
            report["lookahead_risk"] = "view carries model-fitted lookahead in backtest"
        return {"report": report, "decisions": self.decisions, "ticks": self.ticks,
                "trades": trades, "config": self.cfg, "version": self.pm.version,
                "notes": self.notes}


def describe_universe(con, start_ts, end_ts, source="backfill"):
    """What is on the table, for a manager designing a desk: how many markets
    and series, which legs, over what dates. Cheap counts, no bars."""
    table = data.SOURCES[source]
    row = con.execute(
        f"""SELECT COUNT(DISTINCT m.ticker), COUNT(DISTINCT m.series),
                   MIN(c.ts), MAX(c.ts)
            FROM markets m JOIN {table} c ON c.ticker = m.ticker
            WHERE c.ts >= ? AND c.ts < ?""", (start_ts, end_ts)).fetchone()
    top = con.execute(
        f"""SELECT m.series, COUNT(DISTINCT m.ticker) AS n
            FROM markets m JOIN {table} c ON c.ticker = m.ticker
            WHERE c.ts >= ? AND c.ts < ?
            GROUP BY m.series ORDER BY n DESC LIMIT 12""", (start_ts, end_ts)).fetchall()
    legs = con.execute(
        """SELECT CASE WHEN LOWER(sub_title) IN ('tie','draw') THEN 'draw'
                       WHEN sub_title IS NULL OR home IS NULL THEN 'other'
                       ELSE 'team' END AS leg, COUNT(*)
           FROM markets GROUP BY leg""").fetchall()
    return {
        "markets": row[0] or 0, "series": row[1] or 0,
        "from": _date(row[2]) if row[2] else None, "to": _date(row[3]) if row[3] else None,
        "bars": "hourly OHLC with yes_bid/yes_ask, volume, open_interest",
        "top_series": [{"series": s, "markets": n} for s, n in top],
        "legs": {l: n for l, n in legs},
        "note": ("Kalshi soccer markets: each match has home/draw/away winner legs "
                 "and often totals, spreads, corners. Contracts settle at $1 or $0. "
                 "Fees and slippage are charged on every fill."),
    }


def windows(start_ts, end_ts, tick_days):
    step = int(tick_days * DAY)
    t = start_ts
    while t < end_ts:
        yield t, min(t + step, end_ts)
        t += step


def write_run(out_dir, out, partial=False):
    """The run directory, rewritten. Called after every tick, so a crash costs
    one tick and the dashboard is readable while the walk is still going."""
    os.makedirs(out_dir, exist_ok=True)
    for name, d in out["desks"].items():
        tmp = os.path.join(out_dir, f".pm-{name}.json")
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, default=str)
        os.replace(tmp, os.path.join(out_dir, f"pm-{name}.json"))
    meta = {k: out[k] for k in ("leaderboard", "tick_days", "start", "end")}
    meta["partial"] = partial
    tmp = os.path.join(out_dir, ".leaderboard.json")
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=1)
    os.replace(tmp, os.path.join(out_dir, "leaderboard.json"))
    return out_dir


def _snapshot(desks, tick_days, start_ts, end_ts):
    """What every desk looks like right now, in the shape write_run wants."""
    out = {"desks": {}, "tick_days": tick_days,
           "start": _date(start_ts), "end": _date(end_ts)}
    for d in desks:
        out["desks"][d.name] = d.snapshot()
    return _rank(out)


def _rank(out):
    base = out["desks"].get("baseline-market", {}).get("report", {}).get("net_pnl", 0.0)
    board = sorted((v["report"] for v in out["desks"].values()), key=lambda r: -r["net_pnl"])
    out["leaderboard"] = [{"pm": r["pm"], "net_pnl": r["net_pnl"], "n_trades": r["n_trades"],
                           "n_settled": r["n_settled"], "brier": r.get("brier"),
                           "version": r["version"], "benchmark": r["is_benchmark"],
                           "vs_baseline": round(r["net_pnl"] - base, 4)}
                          for r in board]
    return out


def run_walk(con, cfgs, tick_days=2, start_ts=None, end_ts=None, costs=None,
             source="backfill", interval_min=60, managers=None, series=None,
             log=None, memory_days=7, out_dir=None):
    """The walk. `managers` overrides the manager per desk name (tests, or
    --no-manager). Returns {"desks": {name: {...}}, "leaderboard": [...]}."""
    costs = costs or engine.Costs()
    managers = managers or {}
    if start_ts is None or end_ts is None:
        lo, hi = data.date_range(con, source=source, interval_min=interval_min)
        if lo is None:
            raise ValueError(f"no bars in source {source!r}")
        lo = lo - (lo % DAY)                        # start on a UTC midnight
        start_ts = lo if start_ts is None else start_ts
        end_ts = (hi + 1) if end_ts is None else end_ts

    desks = [Desk(c, costs, manager=managers.get(c["name"]), memory_days=memory_days)
             for c in cfgs]
    for d in desks:
        d.tick_days = tick_days
    # benchmarks first each tick, so their numbers are in every manager's brief
    desks.sort(key=lambda d: (not d.is_benchmark, d.name))
    say = log or (lambda *_: None)

    founders = [d for d in desks if not d.is_benchmark and d.cfg.get("founding")]
    if founders:
        universe = describe_universe(con, start_ts, end_ts, source=source)
        bench = {d.name: {"description": d.pm.description, "view": d.view.describe()}
                 for d in desks if d.is_benchmark}
        for d in founders:
            entry = d.found(universe, bench)
            tag = ("founded: " + ", ".join(entry["changed"]) if entry["applied"]
                   else "NOT FOUNDED, runs the placeholder — " + str(entry["rejected"]))
            say(f"founding {d.name:<20} v{entry['version_after']}  {tag}")

    for i, (t0, t1) in enumerate(windows(start_ts, end_ts, tick_days), 1):
        bars = data.load_bars(con, start_ts=t0, end_ts=t1 - 1, series=series,
                              source=source, interval_min=interval_min)
        say(f"tick {i}  {_date(t0)} → {_date(t1)}  {len(bars):,} bars")
        new_by_desk = {d.name: d.run_tick(bars, t1) for d in desks}
        benchmarks = {}
        for d in desks:
            if d.is_benchmark:
                new = new_by_desk[d.name]
                benchmarks[d.name] = {
                    "this_tick_net": round(sum(t["net_pnl"] for t in new), 4),
                    "this_tick_trades": len(new),
                    "cumulative_net": round(sum(t["net_pnl"] for t in d._all_trades), 4)}
        if out_dir:                     # last tick's results, before this tick's calls
            write_run(out_dir, _snapshot(desks, tick_days, start_ts, end_ts), partial=True)
        for d in desks:
            entry = d.decide(i, t0, t1, new_by_desk[d.name], benchmarks)
            if not d.is_benchmark:
                tag = ("applied " + ", ".join(entry["changed"]) if entry["applied"]
                       else ("REJECTED " + str(entry["rejected"]) if entry["rejected"]
                             else "hold"))
                say(f"   {d.name:<20} v{entry['version_after']}  {entry['action']:<6} {tag}")

    out = _rank({"desks": {d.name: d.finish() for d in desks}, "tick_days": tick_days,
                 "start": _date(start_ts), "end": _date(end_ts)})
    if out_dir:
        write_run(out_dir, out, partial=False)
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Walk-forward the firm with managers.")
    ap.add_argument("--pm", nargs="*", help="PM names (default: all in config/pms/)")
    ap.add_argument("--db", default=data.DB_PATH)
    ap.add_argument("--source", choices=sorted(data.SOURCES), default="backfill")
    ap.add_argument("--interval-min", type=int, default=60)
    ap.add_argument("--tick-days", type=float, default=2.0,
                    help="manager tick length in days: 2 for backtest, 7 in the arena")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--series", nargs="*")
    ap.add_argument("--memory-days", type=float, default=7.0,
                    help="how much of its own log a manager sees in full; older "
                         "ticks are one line each")
    ap.add_argument("--no-manager", action="store_true",
                    help="run every desk fixed (no LLM calls)")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage-cents", type=float, default=1.0)
    ap.add_argument("--max-volume-share", type=float, default=0.10)
    ap.add_argument("--fill-delay-bars", type=int, default=1)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)

    try:
        managers = pm_mod.load_all()
    except pm_mod.PMError as e:
        print(f"[pm] {e}", file=sys.stderr)
        return 2
    if args.pm:
        wanted = set(args.pm)
        managers = [m for m in managers if m.name in wanted]
        missing = wanted - {m.name for m in managers}
        if missing:
            print(f"[pm] unknown: {sorted(missing)}", file=sys.stderr)
            return 2
    if not managers:
        print("[pm] no portfolio managers found", file=sys.stderr)
        return 2
    cfgs = [m.cfg for m in managers]

    if not os.path.exists(args.db):
        print(f"[data] no history DB at {args.db}", file=sys.stderr)
        return 3
    con = data.connect(args.db)
    costs = engine.Costs(fee_rate=args.fee_rate, slippage_cents=args.slippage_cents,
                         max_volume_share=args.max_volume_share,
                         fill_delay_bars=args.fill_delay_bars)
    overrides = {c["name"]: mgr_mod.HoldManager() for c in cfgs} if args.no_manager else {}

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
    out_dir = args.out_dir or os.path.join(RESULTS_DIR, stamp)
    print(f"  writing to {out_dir} after every tick")

    out = run_walk(con, cfgs, tick_days=args.tick_days,
                   start_ts=_ts(args.start) if args.start else None,
                   end_ts=_ts(args.end) if args.end else None,
                   costs=costs, source=args.source, interval_min=args.interval_min,
                   managers=overrides, series=args.series, log=print,
                   memory_days=args.memory_days, out_dir=out_dir)

    print(f"\n  ── firm, {out['start']} → {out['end']}, tick {args.tick_days}d ──")
    for r in out["leaderboard"]:
        kind = "benchmark" if r["benchmark"] else f"v{r['version']}"
        print(f"  {r['net_pnl']:>+9.2f}  {r['pm']:<20} {kind:<10} "
              f"{r['n_trades']:>4} trades  {r['n_settled']:>4} settled  "
              f"brier {r['brier'] if r['brier'] is not None else '—'}")
    print(f"\n  written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
