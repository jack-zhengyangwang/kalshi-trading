"""The manager: decisions, the LLM backends (stubbed), and what gets refused."""
import json

import pytest

from wc.firm import manager as mgr_mod


def test_decision_shape_is_enforced():
    with pytest.raises(mgr_mod.ManagerError):
        mgr_mod.Decision("panic", "x")
    with pytest.raises(mgr_mod.ManagerError):
        mgr_mod.Decision("change", "no config")
    d = mgr_mod.Decision("hold", "fine", {"vs_benchmark": "up"})
    assert d.to_dict()["config"] is None


def test_build_from_spec():
    assert isinstance(mgr_mod.build(None), mgr_mod.HoldManager)
    assert isinstance(mgr_mod.build({"type": "none"}), mgr_mod.HoldManager)
    m = mgr_mod.build({"type": "llm", "provider": "groq", "model": "llama"})
    assert m.describe() == "llm(groq:llama)"
    with pytest.raises(mgr_mod.ManagerError):
        mgr_mod.build({"type": "llm", "provider": "nope", "model": "x"})
    with pytest.raises(mgr_mod.ManagerError):
        mgr_mod.build({"type": "llm", "provider": "groq"})


def test_parse_tolerates_prose_and_fences():
    text = ('Here is my decision:\n```json\n{"assessment": {"variance_or_structure": '
            '"variance"}, "action": "hold", "reasoning": "inside the noise band"}\n```')
    d = mgr_mod.parse_decision(text)
    assert d.action == "hold" and d.assessment["variance_or_structure"] == "variance"
    with pytest.raises(mgr_mod.ManagerError):
        mgr_mod.parse_decision("I would rather not say.")
    with pytest.raises(mgr_mod.ManagerError):
        mgr_mod.parse_decision('{"action": "change", "reasoning": "but no config"}')


def test_system_prompt_is_generated_from_the_schema():
    s = mgr_mod.system_prompt()
    assert "model_prob" in s and "leg" in s          # DSL vocabulary
    assert "mean_reversion" in s and "lookback=" in s  # view registry with params
    assert "bankroll" in s                              # the invariant is stated


class _Block:
    def __init__(self, text=None, kind="text"):
        self.type, self.text = kind, text


class _Resp:
    def __init__(self, text, stop="end_turn"):
        self.content = [_Block(kind="thinking"), _Block(text)]
        self.stop_reason = stop


class _Stream:
    def __init__(self, resp): self._r = resp
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get_final_message(self): return self._r


class _AnthropicStub:
    def __init__(self, text):
        self.calls = []
        self._text = text
        self.messages = self

    def stream(self, **kw):
        self.calls.append(kw)
        return _Stream(_Resp(self._text))


def test_anthropic_backend_sends_brief_and_parses_reply():
    reply = json.dumps({"assessment": {"vs_benchmark": "down"}, "action": "change",
                        "reasoning": "halve size", "config": {"name": "d"}})
    stub = _AnthropicStub(reply)
    m = mgr_mod.LLMManager(provider="anthropic", model="claude-opus-5", client=stub)
    d = m.decide({"desk": "d", "this_tick": {"net_pnl": -3.0}})
    assert d.action == "change" and d.config == {"name": "d"}
    call = stub.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert '"net_pnl": -3.0' in call["messages"][0]["content"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_openai_compatible_backend_uses_chat_completions_shape():
    seen = {}

    def fake_post(url, headers, payload):
        seen.update(url=url, payload=payload)
        return json.dumps({"action": "hold", "reasoning": "noise", "assessment": {}})

    m = mgr_mod.LLMManager(provider="openrouter", model="qwen/qwen3-235b-a22b",
                           client=fake_post)
    d = m.decide({"desk": "d"})
    assert d.action == "hold"
    assert seen["url"].startswith("https://openrouter.ai/")
    roles = [x["role"] for x in seen["payload"]["messages"]]
    assert roles == ["system", "user"] and seen["payload"]["model"].startswith("qwen/")


def test_unparseable_reply_becomes_a_rejected_hold():
    m = mgr_mod.LLMManager(provider="groq", model="x",
                           client=lambda *a: "The desk is fine, carry on.")
    d = m.decide({"desk": "d"})
    assert d.action == "hold" and d.assessment.get("rejected")


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    m = mgr_mod.LLMManager(provider="groq", model="x")
    with pytest.raises(mgr_mod.ManagerError, match="GROQ_API_KEY"):
        m._ask("hi")


def test_http_backend_retries_transient_errors(monkeypatch):
    """A 429 or 5xx from the provider is a wait, not a decision. Three tries
    with growing pauses; a 4xx other than 429 is not retried."""
    import requests
    calls, sleeps = [], []

    class R:
        def __init__(self, status, body=None):
            self.status_code, self._body = status, body
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code}", response=self)
        def json(self):
            return self._body

    answers = [R(429), R(503), R(200, {"choices": [{"message": {"content":
               '{"action": "hold", "reasoning": "ok", "assessment": {}}'}}]})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: (calls.append(1), answers.pop(0))[1])
    monkeypatch.setattr(mgr_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("GROQ_API_KEY", "k")
    m = mgr_mod.LLMManager(provider="groq", model="x")
    assert m.decide({}).action == "hold"
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]

    answers[:] = [R(400)]
    calls.clear()
    with pytest.raises(requests.HTTPError):
        m._ask("hi")
    assert len(calls) == 1


def test_a_malformed_reply_is_retried_once():
    """One bad JSON reply costs a whole tick's decision. Ask again, saying
    what was wrong, before giving up."""
    replies = ["Sure! {action: hold}",                       # invalid JSON
               '{"action": "hold", "reasoning": "second try", "assessment": {}}']
    asked = []

    def fake(url, headers, payload):
        asked.append(payload["messages"][-1]["content"])
        return replies.pop(0)

    m = mgr_mod.LLMManager(provider="groq", model="x", client=fake)
    d = m.decide({"desk": "d"})
    assert d.action == "hold" and d.reasoning == "second try"
    assert len(asked) == 2 and "was not valid" in asked[1]

    # still unparseable after the retry: a logged, rejected hold
    m2 = mgr_mod.LLMManager(provider="groq", model="x", client=lambda *a: "no.")
    d2 = m2.decide({"desk": "d"})
    assert d2.action == "hold" and d2.assessment.get("rejected")
