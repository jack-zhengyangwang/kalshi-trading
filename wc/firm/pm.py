"""Portfolio managers: the unit of independent capital and track record.

A PM is what a trading firm actually promotes or fires. It owns:

  • A VIEW      — its own opinion of fair value. Never shared (see views.py).
  • CAPITAL     — its own bankroll, so its P&L is its own and nobody else's
                  allocation decisions muddy the record.
  • AGENTS      — one or more sub-strategies, each a DSL spec, sharing the PM's
                  book. A PM with one agent is just a strategy; a PM with
                  several is a desk.
  • ALLOCATION  — how its capital is split across those agents.

WHY THIS EXISTS. Before it, every arena strategist read `p_fair` from one shared
per-league brain and differed only in risk parameters. Sixteen agents, one
opinion. When the brain was wrong about a match they all lost together, so
"promote the best performer" was ranking risk-parameter luck rather than skill,
and the window-A/window-B selection test in docs/backtester/07_PROMOTION.md had
no genuine diversity to select across. Independent views are what make that gate
mean something.

League is deliberately NOT a structural axis any more. A PM that chooses to
trade only EPL expresses that in its agents' `universe.series` — a strategy
decision, not an architectural one.

A PM is config, never a new file: config/pms/<name>.json.
"""
from __future__ import annotations

import glob
import json
import os

from wc import paths
from wc.backtest import spec as spec_mod
from wc.firm import views

PM_DIR = os.path.join(paths.CONFIG_DIR, "pms")

ALLOCATIONS = {"equal", "weighted"}


class PMError(ValueError):
    """A portfolio manager definition is malformed. Raised at load time."""


class Agent:
    """One sub-strategy inside a PM's book: a DSL spec plus its capital share."""

    __slots__ = ("name", "spec", "weight", "bankroll")

    def __init__(self, name, spec, weight=1.0):
        self.name, self.spec, self.weight = name, spec, float(weight)
        self.bankroll = 0.0

    def __repr__(self):
        return f"Agent({self.name}, weight={self.weight})"


class PortfolioManager:
    """A view, a bankroll, and the agents that trade it."""

    def __init__(self, name, view, agents, bankroll=1000.0, allocation="equal",
                 caps=None, description=""):
        self.name = name
        self.view = view
        self.agents = agents
        self.bankroll = float(bankroll)
        self.allocation = allocation
        self.caps = caps or {}
        self.description = description
        self.allocate()

    # ── capital ──────────────────────────────────────────────────────────────

    def allocate(self):
        """Split the PM's bankroll across its agents.

        Equal by default. Performance-based reallocation is deliberately absent:
        it is itself a strategy, it needs its own backtest before it is trusted,
        and adding it here would silently change what a PM's track record means.
        """
        if not self.agents:
            return
        if self.allocation == "equal":
            share = self.bankroll / len(self.agents)
            for a in self.agents:
                a.bankroll = share
            return
        total = sum(a.weight for a in self.agents) or 1.0
        for a in self.agents:
            a.bankroll = self.bankroll * (a.weight / total)

    # ── introspection ────────────────────────────────────────────────────────

    @property
    def carries_lookahead(self):
        """True when this PM's view is fitted on data a backtest bar could not
        have seen. Surfaced so a report can say so rather than a reader having
        to know which views are safe."""
        return bool(getattr(self.view, "lookahead", False))

    def describe(self):
        return (f"{self.name}: view={self.view.describe()} "
                f"${self.bankroll:,.0f} across {len(self.agents)} agent(s)")

    def __repr__(self):
        return f"PortfolioManager({self.name}, {len(self.agents)} agents)"


# ── loading ───────────────────────────────────────────────────────────────────

def validate(cfg, base_dir=None):
    """Validate a PM definition. Raises PMError with a useful message."""
    if not isinstance(cfg, dict):
        raise PMError(f"a PM definition must be an object, got {type(cfg).__name__}")

    for key in ("name", "view", "agents", "bankroll"):
        if key not in cfg:
            raise PMError(f"missing required field '{key}'")

    if not str(cfg["name"]).strip():
        raise PMError("'name' must be a non-empty string")

    bankroll = cfg["bankroll"]
    if not isinstance(bankroll, (int, float)) or isinstance(bankroll, bool) \
            or bankroll <= 0:
        raise PMError(f"'bankroll' must be a positive number, got {bankroll!r}")

    alloc = cfg.get("allocation", "equal")
    if alloc not in ALLOCATIONS:
        raise PMError(f"'allocation' must be one of {sorted(ALLOCATIONS)}, "
                      f"got {alloc!r}")

    agents = cfg["agents"]
    if not isinstance(agents, list) or not agents:
        raise PMError("'agents' must be a non-empty list — a PM with no agents "
                      "cannot trade")

    seen = set()
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            raise PMError(f"agents[{i}]: must be an object")
        if "spec" not in a:
            raise PMError(f"agents[{i}]: missing 'spec' (a path to a strategy JSON)")
        nm = a.get("name") or os.path.splitext(os.path.basename(a["spec"]))[0]
        if nm in seen:
            raise PMError(f"agents[{i}]: duplicate agent name {nm!r} — per-agent "
                          f"P&L would be indistinguishable")
        seen.add(nm)
        if alloc == "weighted":
            w = a.get("weight")
            if not isinstance(w, (int, float)) or isinstance(w, bool) or w <= 0:
                raise PMError(f"agents[{i}]: 'weighted' allocation needs a "
                              f"positive 'weight', got {w!r}")
    return cfg


def load(path):
    """Load and build a PM from a JSON definition."""
    with open(path) as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            raise PMError(f"{path}: invalid JSON — {e}") from e

    try:
        validate(cfg)
    except PMError as e:
        raise PMError(f"{path}: {e}") from e

    try:
        view = views.build(cfg["view"])
    except ValueError as e:
        raise PMError(f"{path}: {e}") from e

    root = paths.ROOT
    agents = []
    for a in cfg["agents"]:
        spec_path = a["spec"]
        if not os.path.isabs(spec_path):
            spec_path = os.path.join(root, spec_path)
        try:
            spec = spec_mod.load(spec_path)
        except spec_mod.SpecError as e:
            raise PMError(f"{path}: agent {a.get('name') or spec_path}: {e}") from e
        name = a.get("name") or os.path.splitext(os.path.basename(spec_path))[0]
        agents.append(Agent(name, spec, a.get("weight", 1.0)))

    return PortfolioManager(
        name=cfg["name"], view=view, agents=agents,
        bankroll=cfg["bankroll"], allocation=cfg.get("allocation", "equal"),
        caps=cfg.get("caps"), description=cfg.get("description", ""))


def load_all(pattern=None):
    """Every PM in config/pms/. The firm."""
    out = []
    for p in sorted(glob.glob(pattern or os.path.join(PM_DIR, "*.json"))):
        out.append(load(p))
    return out
