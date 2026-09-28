"""The manager: the part of a desk that decides between ticks.

A desk is not a rule-set. It is a rule-set plus a manager who reads the
period's results, decides whether the desk is doing well or badly, decides
whether that is variance or structure, and then changes whatever they judge
necessary — the view, its parameters, the sizing, the agents, the caps — or
changes nothing. The desk's track record is one continuous line that includes
those decisions, and that line is what the firm judges.

The manager is not restricted in WHAT it may change. It is restricted in one
way only: what it hands back must be a desk the engine can run — a validated
PM definition (wc/firm/pm.py) on a known view, with agents in the DSL and
caps present — with the same name and the same bankroll. That is
executability, not policy. A manager cannot give itself money.

    brief ─► Manager.decide ─► Decision(hold | change, reasoning, config)
                                     │
                              walk.py validates, applies, logs

Managers here:
    HoldManager       never changes anything. Every benchmark runs on one.
    ScriptedManager   replays a list of decisions. For tests.
    LLMManager        a language model reasoning like a PM, on one of several
                      backends (Anthropic SDK, or an OpenAI-compatible HTTP
                      endpoint such as OpenRouter or Groq). Which model runs a
                      desk is part of the desk's definition, so two desks that
                      differ in nothing but their manager are a fair test of
                      which manager is better.
"""
from __future__ import annotations

import json
import os
import re
import time

from wc.backtest import spec as spec_mod
from wc.firm import views


class ManagerError(ValueError):
    pass


class Decision:
    """What a manager decided this tick.

    `action` is "hold" or "change". `config` is the full PM definition the
    desk should run from now on (only for "change"). `assessment` is the
    manager's own reading of the period — performance vs benchmark and vs
    last period, and whether the move was variance or structure — kept
    separately from `reasoning` so the log can be read as a table.
    """

    __slots__ = ("action", "reasoning", "assessment", "config", "notes")

    def __init__(self, action, reasoning, assessment=None, config=None, notes=None):
        if action not in ("hold", "change"):
            raise ManagerError(f"action must be 'hold' or 'change', got {action!r}")
        if action == "change" and not isinstance(config, dict):
            raise ManagerError("a 'change' decision must carry a config")
        self.action = action
        self.reasoning = str(reasoning)
        self.assessment = dict(assessment or {})
        self.config = config
        # The manager's own standing note: what it wants to remember once the
        # tick-by-tick log has scrolled out of the brief. Carried forward
        # verbatim until it rewrites it.
        self.notes = None if notes is None else str(notes)

    def to_dict(self):
        return {"action": self.action, "reasoning": self.reasoning,
                "assessment": self.assessment, "config": self.config,
                "notes": self.notes}


class Manager:
    name = "base"

    def decide(self, brief) -> Decision:
        raise NotImplementedError

    def describe(self):
        return self.name


class HoldManager(Manager):
    """A fixed desk. Benchmarks run on this, and so does any desk whose
    definition says `"manager": {"type": "none"}`."""
    name = "none"

    def decide(self, brief):
        return Decision("hold", "fixed desk", {})


class ScriptedManager(Manager):
    """Replays decisions in order, then holds. For tests."""
    name = "scripted"

    def __init__(self, decisions):
        self._queue = list(decisions)

    def decide(self, brief):
        if self._queue:
            return self._queue.pop(0)
        return Decision("hold", "script exhausted", {})


# ── the LLM manager ───────────────────────────────────────────────────────────

SYSTEM = """You are the portfolio manager of one trading desk at a firm that
trades Kalshi soccer prediction markets with paper money. The desk has a VIEW
(an opinion about probabilities), AGENTS (rule-based traders that act on that
opinion), a fixed BANKROLL and CAPS. Every period you receive the desk's
results and decide what, if anything, to change. Your decisions are logged and
your desk's whole track record — including your decisions — is what the firm
judges you on, against other desks and against fixed benchmarks that never
change.

How a good manager works each period:

1. EVALUATE. Are we up or down — against the benchmarks over the same period,
   and against our own last period? Look at net P&L after fees, the Brier
   score (whether the view's probabilities are any good), win rate, and what
   each agent contributed. Read the counts carefully: `positions_opened` is
   what the desk BOUGHT this period; `n_trades` / `positions_closed` is what
   it CLOSED (settled or sold early); `open_positions` is the book it still
   holds, with its cost and unrealized P&L. A period with many positions
   opened and none closed is a busy desk waiting on results, not an idle one.
   Matches settle when they finish, usually within a day or two of the bet.

2. VARIANCE OR STRUCTURE. A period's P&L is a small sample. The brief gives
   you the per-trade standard deviation and a noise band (std x sqrt(n)): a
   period result inside that band is what luck alone produces. Ask whether the
   move is noise, or whether something structural is wrong — the view is
   miscalibrated (bad Brier), the sizing is wrong (good Brier, bad P&L), fees
   are eating the edge (many small trades), a leg or league is consistently
   losing, an agent is dragging the desk. A good manager does not fire a
   method after one unlucky period, and does not keep a broken one because
   it got lucky.

   YOUR MEMORY. The brief quotes the last week of your decisions in full.
   Older ticks appear in `earlier` as one line each — action, what changed,
   net — with the prose dropped. `notes` is your own standing note: the one
   thing that survives in full however long the desk runs. Use it for what
   you must not forget once the log scrolls away — what you have already
   tried and what it did, what you believe the desk's edge is, what would
   falsify it. Rewrite it when you learn something; return it unchanged when
   you have not. Keep it under 200 words; it is a note to yourself, not a
   report.

3. DECIDE. Change whatever you judge necessary, or hold. You may change the
   view (a different one from the list, or new parameters), sizing, entry and
   exit rules, which agents the desk runs, universe filters, caps, anything.
   You may also decide the last change was a mistake and go back. Holding is
   a real decision: "we made money and the method is sound, keep it" is often
   right. Read your own decision log so you do not oscillate or repeat an
   experiment that already failed.

FOUNDING. When the brief says `"founding": true` there are no results yet:
you are designing the desk from nothing. The seed config is a placeholder
that trades nothing. Choose a view and its parameters, and write as many
rule-based agents as you want in the DSL — use the example agents as
templates. Give each agent a job that follows from the view. Say in your
reasoning what edge you believe the desk has and how you would know if you
were wrong. Return `"action": "change"` with the complete desk. If a previous
attempt was rejected, the brief says why — fix exactly that.

What you hand back must be a complete, valid desk definition the engine can
run. Rules you cannot break:
- `name` and `bankroll` stay exactly as they are. You cannot fund yourself.
- `view.view` must be one of the known views; `view.params` only its parameters.
- Every agent needs a `name` and a full inline `spec` in the strategy DSL
  below (side, universe, entry, sizing, exit, caps). You may copy the current
  ones and edit them, or write new ones.
- Keep `caps` present on the desk and on every agent.

{vocabulary}

Known views and their parameters (defaults shown):
{views}

The exact shape of a desk definition (this is what `config` must look like):
{{
  "name": "<unchanged>", "bankroll": <unchanged>, "allocation": "equal",
  "view": {{"view": "<one of the known views>", "params": {{...its parameters...}}}},
  "caps": {{"daily_spend_dollars": 60.0, "total_exposure_dollars": 300.0}},
  "agents": [
    {{"name": "my-agent",
      "spec": {{"name": "my-agent", "version": 1, "side": "yes",
               "universe": {{"series": ["*"], "min_volume": 20}},
               "entry": {{"all": [{{"signal": "edge", "op": "gt", "value": 0.06}},
                                 {{"signal": "spread", "op": "lt", "value": 8}}]}},
               "sizing": {{"method": "kelly", "fraction": 0.2, "max_bet_dollars": 8.0,
                          "max_concurrent_positions": 25}},
               "exit": {{"any": [{{"signal": "hold_to_settlement", "op": "eq", "value": true}}]}},
               "caps": {{"daily_spend_dollars": 60.0, "per_market_dollars": 8.0,
                        "total_exposure_dollars": 300.0}}}}}}
  ]
}}

Respond with JSON only, in this shape:
{{
  "assessment": {{
    "vs_benchmark": "<up|down|flat> and by how much",
    "vs_last_period": "<up|down|flat> and by how much",
    "variance_or_structure": "<variance|structure|mixed>",
    "diagnosis": "<one or two sentences on what is actually going on>"
  }},
  "action": "hold" | "change",
  "reasoning": "<why, in plain English, as you would write in the desk log>",
  "notes": "<your standing note, carried to every future period; rewrite or repeat>",
  "config": <the complete new desk definition, only when action is "change">
}}
"""


def _vocabulary():
    lines = ["Strategy DSL — ENTRY + EXIT signals:"]
    for name, (lo, hi, desc) in sorted(spec_mod.SIGNALS.items()):
        rng = f"[{lo}, {hi}]" if hi is not None else f">= {lo}"
        lines.append(f"  {name:<20} {rng:<16} {desc}")
    lines.append("EXIT-ONLY signals:")
    for name, (lo, hi, desc) in sorted(spec_mod.POSITION_SIGNALS.items()):
        lines.append(f"  {name:<20} {'':<16} {desc}")
    lines.append(f"operators: {sorted(spec_mod.OPS)}; combinators: "
                 f"{sorted(spec_mod.COMBINATORS)} (nestable one level)")
    lines.append(f"side: {sorted(spec_mod.SIDES)}; sizing.method: "
                 f"{sorted(spec_mod.SIZING_METHODS)}; sizing keys: "
                 f"{sorted(spec_mod.SIZING_KEYS)}")
    lines.append("universe keys: series (list or ['*']), max_yes_price_cents, "
                 "min_yes_price_cents, min_volume, min_open_interest, "
                 "min_days_to_resolution, max_days_to_resolution, "
                 f"leg (one of {sorted(spec_mod.LEGS)})")
    lines.append("agent caps (all required): daily_spend_dollars, "
                 "per_market_dollars, total_exposure_dollars")
    return "\n".join(lines)


def _views():
    import inspect
    lines = []
    for name, cls in sorted(views.REGISTRY.items()):
        sig = inspect.signature(cls.__init__)
        params = ", ".join(f"{p.name}={p.default!r}" for p in sig.parameters.values()
                           if p.name != "self")
        doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        lines.append(f"  {name:<16} {params or '(no parameters)'}\n      {doc}")
    return "\n".join(lines)


def founding_material():
    """What a manager needs to design a desk from nothing: the DSL, the views,
    and the shipped agents as worked examples."""
    import glob
    from wc import paths
    examples = {}
    for path in sorted(glob.glob(os.path.join(paths.ROOT, "strategies", "agents", "*.json"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if "-v2" in name or "-v3" in name:
            continue
        with open(path) as f:
            examples[name] = json.load(f)
    return {"dsl": _vocabulary(), "views": _views(), "example_agents": examples}


def system_prompt():
    return SYSTEM.format(vocabulary=_vocabulary(), views=_views())


def user_prompt(brief):
    """The brief, verbatim. Sorted keys so the prefix is stable for caching
    where the backend supports it."""
    return ("Here is this period's brief for your desk. Evaluate, separate "
            "variance from structure, decide.\n\n"
            + json.dumps(brief, indent=1, sort_keys=True, default=str))


def parse_decision(text):
    """Model text -> Decision. Tolerates fences and prose around the JSON."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ManagerError("no JSON object in the manager's reply")
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ManagerError(f"manager reply is not valid JSON: {e}") from e
    if not isinstance(out, dict):
        raise ManagerError("manager reply must be a JSON object")
    action = out.get("action")
    cfg = out.get("config")
    if action == "change" and not isinstance(cfg, dict):
        raise ManagerError("a 'change' reply must include the full config")
    return Decision(action, out.get("reasoning", ""),
                    out.get("assessment") or {},
                    cfg if action == "change" else None,
                    notes=out.get("notes"))


# OpenAI-compatible endpoints. The key comes from the environment, never from
# config, so a desk definition can be committed.
OPENAI_COMPATIBLE = {
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
    "groq":       ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
}


class LLMManager(Manager):
    """A language model as the desk's manager.

    `provider` picks the backend: "anthropic" through the SDK, or one of
    OPENAI_COMPATIBLE over HTTP. `client` may be injected for tests; for the
    HTTP backends it is a callable (url, headers, payload) -> reply text.

    A reply the validator rejects is not patched: it is returned as a decision
    the runner will refuse and log, exactly like any other bad decision. The
    log is the point — a manager whose replies keep failing validation is a
    fact about that manager.
    """
    name = "llm"

    def __init__(self, provider="anthropic", model=None, client=None,
                 max_tokens=16000):
        if provider != "anthropic" and provider not in OPENAI_COMPATIBLE:
            raise ManagerError(f"unknown provider {provider!r}")
        if not model:
            raise ManagerError("LLMManager needs a model")
        self.provider, self.model = provider, model
        self._client = client
        self.max_tokens = max_tokens
        self._system = system_prompt()

    def describe(self):
        return f"llm({self.provider}:{self.model})"

    def decide(self, brief):
        """One call, and one retry if the reply cannot be read.

        A malformed reply costs the desk a whole period's decision, so it is
        worth asking again with the error attached. Twice is the limit: a
        model that cannot answer the schema twice is telling us something,
        and the log should say so rather than hide it behind retries.
        """
        prompt = user_prompt(brief)
        for attempt in (1, 2):
            text = self._ask(prompt)
            try:
                return parse_decision(text)
            except ManagerError as e:
                if attempt == 1:
                    prompt = (user_prompt(brief) +
                              f"\n\nYour previous reply was not valid: {e}\n"
                              f"Answer again with JSON only, in the shape given.")
                    continue
                # Surface it as a decision the runner will log as rejected,
                # with the raw reply kept for the post-mortem.
                d = Decision("hold", f"unparseable reply: {e}", {"raw": text[:2000]})
                d.assessment["rejected"] = str(e)
                return d

    # ── backends ─────────────────────────────────────────────────────────

    def _ask(self, user):
        if self.provider == "anthropic":
            return self._ask_anthropic(user)
        return self._ask_openai_compatible(user)

    def _ask_anthropic(self, user):
        client = self._client
        if client is None:
            import anthropic
            client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
            self._client = client
        with client.messages.stream(
            model=self.model, max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": self._system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason == "refusal":
            raise ManagerError("the model refused the brief")
        parts = [b.text for b in resp.content if getattr(b, "text", None)]
        if not parts:
            raise ManagerError("model returned no text block")
        return "\n".join(parts).strip()

    def _ask_openai_compatible(self, user):
        url, key_env = OPENAI_COMPATIBLE[self.provider]
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": self._system},
                                {"role": "user", "content": user}],
                   "max_tokens": self.max_tokens}
        if self._client is not None:
            return self._client(url, {}, payload)
        key = os.environ.get(key_env)
        if not key:
            raise ManagerError(f"{key_env} is not set")
        import requests
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        # A 429 or a 5xx is the provider being busy, not a decision. Three
        # tries with growing pauses; anything else is a real error.
        for attempt, pause in enumerate((5, 20, None)):
            r = requests.post(url, headers=headers, json=payload, timeout=300)
            if r.status_code in (429,) or r.status_code >= 500:
                if pause is None:
                    r.raise_for_status()
                time.sleep(pause)
                continue
            r.raise_for_status()
            break
        body = r.json()
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ManagerError(f"unexpected reply shape from {self.provider}: "
                               f"{str(body)[:300]}") from e


def build(spec, client=None):
    """{'type': 'none'} | {'type': 'llm', 'provider': ..., 'model': ...} -> Manager."""
    spec = spec or {"type": "none"}
    kind = spec.get("type", "none")
    if kind == "none":
        return HoldManager()
    if kind == "llm":
        return LLMManager(provider=spec.get("provider", "anthropic"),
                          model=spec.get("model"), client=client)
    raise ManagerError(f"unknown manager type {kind!r}")
