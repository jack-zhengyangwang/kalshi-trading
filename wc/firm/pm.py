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


MANAGER_TYPES = {"none", "llm"}
# LLM backends a manager may run on. "anthropic" uses the SDK; the others are
# OpenAI-compatible HTTP endpoints. Which model runs a desk is part of the
# desk's definition, so two desks can differ in nothing but their manager.
PROVIDERS = {"anthropic", "openrouter", "groq"}


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
                 caps=None, description="", learns=True, manager=None,
                 version=1, cfg=None):
        self.name = name
        self.view = view
        self.agents = agents
        self.bankroll = float(bankroll)
        self.allocation = allocation
        self.caps = caps or {}
        self.description = description
        # Whether this PM keeps a journal and sizes on its own record. On by
        # default: a desk that never learns from being wrong is not a desk.
        self.learns = bool(learns)
        # Who runs this desk between ticks. {"type": "none"} is a fixed desk —
        # every benchmark is one. See wc/firm/manager.py.
        self.manager_spec = dict(manager or {"type": "none"})
        # Bumped every time the manager changes the desk; the decision log
        # says why. Versions live in this number and in git, never in filenames.
        self.version = int(version)
        # The definition this desk was built from, so a manager can hand back
        # a changed one and the runner can diff the two.
        self.cfg = json.loads(json.dumps(cfg)) if cfg is not None else None
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

def _normalize_agent(a):
    """Accept the shapes a manager writes by hand, in place: a spec written
    as the agent itself becomes {"name", "spec"}, and an inline spec without
    a name takes the agent's."""
    if "spec" not in a and "side" in a and "entry" in a:
        name, weight = a.get("name"), a.get("weight")
        body = {k: v for k, v in a.items() if k != "weight"}
        a.clear()
        a["name"], a["spec"] = name, body
        if weight is not None:
            a["weight"] = weight
    if isinstance(a.get("spec"), dict) and a.get("name") and not a["spec"].get("name"):
        a["spec"]["name"] = a["name"]


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

    mgr = cfg.get("manager", {"type": "none"})
    if not isinstance(mgr, dict) or mgr.get("type") not in MANAGER_TYPES:
        raise PMError(f"'manager' must be an object with type in "
                      f"{sorted(MANAGER_TYPES)}, got {mgr!r}")
    if mgr.get("type") == "llm":
        if mgr.get("provider", "anthropic") not in PROVIDERS:
            raise PMError(f"manager.provider must be one of {sorted(PROVIDERS)}, "
                          f"got {mgr.get('provider')!r}")
        if not str(mgr.get("model") or "").strip():
            raise PMError("manager.model must name a model")

    if "founding" in cfg and not isinstance(cfg["founding"], bool):
        raise PMError("'founding' must be true or false")

    ver = cfg.get("version", 1)
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        raise PMError(f"'version' must be a positive integer, got {ver!r}")

    seen = set()
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            raise PMError(f"agents[{i}]: must be an object")
        _normalize_agent(a)
        if "spec" not in a:
            raise PMError(f"agents[{i}]: missing 'spec' (a path to a strategy "
                          f"JSON, or the spec itself)")
        if isinstance(a["spec"], dict):
            if not a.get("name"):
                raise PMError(f"agents[{i}]: an inline spec needs a 'name'")
            nm = a["name"]
        else:
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

    return build(cfg, where=path)


def build(cfg, where="<config>"):
    """A validated definition -> a PortfolioManager. `where` names the source
    in errors: a file path, or "manager decision" when a manager wrote it."""
    try:
        validate(cfg)
    except PMError as e:
        raise PMError(f"{where}: {e}") from e

    try:
        view = views.build(cfg["view"])
    except (ValueError, TypeError) as e:
        raise PMError(f"{where}: {e}") from e

    root = paths.ROOT
    agents = []
    for a in cfg["agents"]:
        if isinstance(a["spec"], dict):
            try:
                spec = spec_mod.validate(json.loads(json.dumps(a["spec"])))
            except spec_mod.SpecError as e:
                raise PMError(f"{where}: agent {a['name']}: {e}") from e
            name = a["name"]
        else:
            spec_path = a["spec"]
            if not os.path.isabs(spec_path):
                spec_path = os.path.join(root, spec_path)
            try:
                spec = spec_mod.load(spec_path)
            except spec_mod.SpecError as e:
                raise PMError(f"{where}: agent {a.get('name') or spec_path}: {e}") from e
            name = a.get("name") or os.path.splitext(os.path.basename(spec_path))[0]
        agents.append(Agent(name, spec, a.get("weight", 1.0)))

    return PortfolioManager(
        name=cfg["name"], view=view, agents=agents,
        bankroll=cfg["bankroll"], allocation=cfg.get("allocation", "equal"),
        caps=cfg.get("caps"), description=cfg.get("description", ""),
        learns=cfg.get("learns", True), manager=cfg.get("manager"),
        version=cfg.get("version", 1), cfg=cfg)


def load_all(pattern=None):
    """Every PM in config/pms/. The firm."""
    out = []
    for p in sorted(glob.glob(pattern or os.path.join(PM_DIR, "*.json"))):
        out.append(load(p))
    return out
