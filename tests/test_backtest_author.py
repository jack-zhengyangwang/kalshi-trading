"""Authoring: English (and video) into a spec.

The safety argument is that the validator is the gate — the model cannot emit
code, cannot invent a signal, and cannot produce anything that runs unless it
validates. These tests are that argument, plus the honesty requirements around
assumptions and provenance.
"""
import json

import pytest

from wc.backtest import author, spec as spec_mod

VALID = {
    "name": "t", "side": "no",
    "entry": {"all": [{"signal": "yes_price", "op": "lt", "value": 0.10}]},
    "sizing": {"method": "fixed", "dollars": 5.0},
    "exit": {"any": [{"signal": "days_to_resolution", "op": "lt", "value": 1}]},
    "caps": {"daily_spend_dollars": 50.0, "per_market_dollars": 5.0,
             "total_exposure_dollars": 200.0},
}


class Block:
    def __init__(self, text):
        self.text = text


class Thinking:
    """A reasoning block — has no .text, and used to crash content[0].text."""
    def __init__(self):
        self.thinking = "hmm"


class Resp:
    def __init__(self, blocks):
        self.content = blocks


class FakeClient:
    """Returns queued responses; records the prompts it was given."""

    def __init__(self, *payloads, blocks=None):
        self.payloads = list(payloads)
        self.blocks = blocks
        self.prompts = []
        self.messages = self

    def create(self, model=None, max_tokens=None, messages=None, **kw):
        self.prompts.append(messages[0]["content"])
        if self.blocks is not None:
            return Resp(self.blocks.pop(0))
        p = self.payloads.pop(0)
        return Resp([Block(p if isinstance(p, str) else json.dumps(p))])


# ── the prompt is generated from the schema ───────────────────────────────────

def test_vocabulary_comes_from_the_schema_not_a_copy():
    """A hand-maintained copy would drift, and the model would be told about
    signals the validator rejects — which reads as hallucination when it is
    actually misinformation."""
    p = author.build_prompt("anything")
    for sig in spec_mod.SIGNALS:
        assert sig in p, f"{sig} missing from the prompt"
    for sig in spec_mod.POSITION_SIGNALS:
        assert sig in p


def test_prompt_offers_no_escape_hatch():
    p = author.build_prompt("anything").lower()
    assert "custom_python" not in p and "eval(" not in p


def test_prompt_carries_a_worked_example():
    assert "sell-cheap-longshots" in author.build_prompt("x")


# ── validation is the contract ────────────────────────────────────────────────

def test_valid_output_is_returned_with_assumptions():
    c = FakeClient({"spec": VALID, "assumptions": ["chose side=no"]})
    spec, assumptions = author.author("sell longshots", client=c)
    assert spec["name"] == "t"
    assert assumptions == ["chose side=no"]
    assert len(c.prompts) == 1


def test_invalid_output_is_retried_once_with_the_error_attached():
    bad = {"spec": dict(VALID, caps={}), "assumptions": []}
    c = FakeClient(bad, {"spec": VALID, "assumptions": []})
    spec, _ = author.author("x", client=c)
    assert spec["name"] == "t"
    assert len(c.prompts) == 2
    assert "REJECTED by the validator" in c.prompts[1]
    assert "caps.daily_spend_dollars" in c.prompts[1]


def test_two_failures_give_up_rather_than_hand_patching():
    """A spec nobody can regenerate is a spec nobody can trust."""
    bad = {"spec": dict(VALID, caps={}), "assumptions": []}
    c = FakeClient(bad, bad)
    with pytest.raises(spec_mod.SpecError, match="2 attempts"):
        author.author("x", client=c)


def test_an_invented_signal_always_fails():
    """The model may not extend the vocabulary. That is a human decision."""
    invented = {"spec": dict(VALID, entry={"all": [
        {"signal": "insider_sentiment", "op": "gt", "value": 0.5}]}),
        "assumptions": []}
    c = FakeClient(invented, invented)
    with pytest.raises(spec_mod.SpecError, match="insider_sentiment"):
        author.author("x", client=c)


def test_a_spec_without_caps_never_gets_through():
    no_caps = {"spec": {k: v for k, v in VALID.items() if k != "caps"},
               "assumptions": []}
    c = FakeClient(no_caps, no_caps)
    with pytest.raises(spec_mod.SpecError):
        author.author("x", client=c)


def test_missing_spec_key_is_reported_not_crashed():
    c = FakeClient({"assumptions": []}, {"spec": VALID, "assumptions": []})
    spec, _ = author.author("x", client=c)
    assert spec["name"] == "t"
    assert "no 'spec' key" in c.prompts[1]


# ── response shapes ───────────────────────────────────────────────────────────

def test_thinking_blocks_are_skipped():
    """Reasoning models put a ThinkingBlock first; content[0].text raises."""
    c = FakeClient(blocks=[[Thinking(),
                            Block(json.dumps({"spec": VALID, "assumptions": []}))]])
    spec, _ = author.author("x", client=c)
    assert spec["name"] == "t"


def test_prose_around_the_json_is_tolerated():
    c = FakeClient("Here you go:\n" + json.dumps({"spec": VALID, "assumptions": []})
                   + "\nHope that helps!")
    spec, _ = author.author("x", client=c)
    assert spec["name"] == "t"


# ── video ─────────────────────────────────────────────────────────────────────

def test_video_records_provenance(monkeypatch):
    """When a strategy fails we need to know which video sold it."""
    monkeypatch.setattr(author, "fetch_transcript",
                        lambda url: "just sell the longshots bro")
    c = FakeClient("Sell longshots. EVIDENCE: none shown.",
                   {"spec": dict(VALID), "assumptions": []})
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    spec, _ = author.from_video(url, client=c)
    assert spec["provenance"]["url"] == url
    assert spec["provenance"]["source"] == "video"
    assert "EVIDENCE" in spec["provenance"]["extracted_idea"]


def test_video_prompt_separates_claim_from_evidence():
    """Videos overstate. A plausible video with no backtest is an untested
    idea, not a strategy."""
    p = author.VIDEO_PROMPT.lower()
    assert "claims" in p and "evidence" in p and "overstate" in p


def test_transcript_failure_is_loud():
    """'No transcript' must be a clear error, never an empty string that
    silently becomes a strategy authored from nothing."""
    with pytest.raises(RuntimeError, match="video id"):
        author.fetch_transcript("https://example.com/not-a-video")


def test_vtt_stripping_removes_timestamps():
    vtt = "WEBVTT\n\n00:01.000 --> 00:03.000\nsell the longshots\n"
    assert author._strip_vtt(vtt) == "sell the longshots"
