"""Phase 4 — authoring: English (or a video) into a strategy spec.

Cheap only because phase 3 exists. The model's job is filling in a schema, not
writing a program: it cannot emit code, cannot invent a signal, and cannot
produce anything the validator does not accept. That is the whole safety
argument, and it is why this file is short.

    prose ─► LLM (schema + vocabulary in the prompt) ─► JSON ─► validate ─► spec
                                                          │
                                                    reject + reason ─► retry once

Usage:
    python3 -m wc.backtest.author "sell contracts under 10 cents" -o strategies/x.json
    python3 -m wc.backtest.author --video https://youtube.com/watch?v=... -o strategies/y.json

See docs/backtester/04_AUTHORING.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from wc.backtest import spec as spec_mod

MODEL = "claude-sonnet-5"
MAX_TRANSCRIPT_CHARS = 40_000


# ── prompt ────────────────────────────────────────────────────────────────────

def _vocabulary():
    """The signal table, generated FROM the schema rather than restated.

    A hand-maintained copy in the prompt would drift from spec.py, and the model
    would then be told about signals the validator rejects — which reads as the
    model hallucinating when it is actually being misinformed.
    """
    lines = ["ENTRY + EXIT signals:"]
    for name, (lo, hi, desc) in sorted(spec_mod.SIGNALS.items()):
        rng = f"[{lo}, {hi}]" if hi is not None else f">= {lo}"
        lines.append(f"  {name:<20} {rng:<16} {desc}")
    lines.append("EXIT-ONLY signals (meaningless without an open position):")
    for name, (lo, hi, desc) in sorted(spec_mod.POSITION_SIGNALS.items()):
        lines.append(f"  {name:<20} {'':<16} {desc}")
    lines.append(f"\noperators:   {sorted(spec_mod.OPS)}")
    lines.append(f"combinators: {sorted(spec_mod.COMBINATORS)} (nestable one level)")
    lines.append(f"side:        {sorted(spec_mod.SIDES)}")
    lines.append(f"sizing.method: {sorted(spec_mod.SIZING_METHODS)}")
    lines.append(f"universe keys: ['series', 'max_yes_price_cents', "
                 f"'min_yes_price_cents', 'min_volume', 'min_open_interest', "
                 f"'min_days_to_resolution', 'max_days_to_resolution']")
    return "\n".join(lines)


PROMPT = """You translate a trading idea into a strategy SPEC for a Kalshi
prediction-market backtester. You are filling in a fixed schema. You never write
code, and you never use a signal that is not listed below — an invented name is
rejected by the validator and the whole attempt is wasted.

{vocabulary}

RULES
- `caps` is MANDATORY and every value must be positive. A strategy with no cap
  is not a strategy.
- `entry` must be non-empty, or the strategy trades everything.
- `price` is what WE pay given our side. `yes_price` is the market's own price.
  "a cheap longshot" describes the MARKET, so it is `yes_price`; use `price`
  for what the position costs us.
- Exit-only signals may appear in `exit`, never in `entry`.
- Prices are probabilities in 0..1. Cents fields (yes_bid, yes_ask, spread,
  universe.*_cents) are integers 0..100.
- Sizing: "kelly" needs `fraction` in (0, 1].

EXAMPLE
{example}

IDEA
{idea}

Return ONLY a JSON object with two top-level keys:
{{"spec": <the strategy spec>,
  "assumptions": ["each thing the idea did not specify and what you chose"]}}

The idea will underspecify things — side, thresholds, horizon, sizing. Choose
sensible values and LIST every one in `assumptions`. Do not silently guess."""


def _example():
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))),
        "strategies", "sell-cheap-longshots.json")
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return "{}"


def build_prompt(idea):
    return PROMPT.format(vocabulary=_vocabulary(), example=_example(), idea=idea)


# ── the LLM leg ───────────────────────────────────────────────────────────────

def _parse_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0) if match else text)


def response_text(resp):
    """The text of a response, skipping non-text blocks.

    Reasoning models put a ThinkingBlock first, so `content[0].text` raises.
    Joining every text block is correct regardless of what else the model
    emitted, and stays correct if block types change again.
    """
    parts = [b.text for b in resp.content if getattr(b, "text", None)]
    if not parts:
        raise ValueError("model returned no text block")
    return "\n".join(parts).strip()


def _call(prompt, model=MODEL, client=None):
    if client is None:
        import anthropic
        client = anthropic.Anthropic()               # reads ANTHROPIC_API_KEY
    resp = client.messages.create(
        model=model, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}])
    return _parse_json(response_text(resp))


def author(idea, client=None, model=MODEL, provenance=None):
    """Idea -> (spec, assumptions). Raises SpecError if the model cannot produce
    a valid spec in two attempts.

    Validation is the contract. Invalid output is retried ONCE with the error
    attached, and then given up on — never hand-patched into something that
    runs, because a spec nobody can regenerate is a spec nobody can trust.
    """
    prompt = build_prompt(idea)
    last_error = None

    for attempt in (1, 2):
        try:
            out = _call(prompt, model=model, client=client)
        except Exception as e:
            raise spec_mod.SpecError(f"LLM call failed: {e}") from e

        candidate = out.get("spec") if isinstance(out, dict) else None
        if candidate is None:
            last_error = "response had no 'spec' key"
        else:
            try:
                spec_mod.validate(candidate)
                if provenance:
                    candidate["provenance"] = provenance
                return candidate, list(out.get("assumptions") or [])
            except spec_mod.SpecError as e:
                last_error = str(e)

        if attempt == 1:
            prompt = (build_prompt(idea) +
                      f"\n\nYour previous attempt was REJECTED by the validator:\n"
                      f"  {last_error}\n"
                      f"Fix exactly that and return the corrected JSON.")

    raise spec_mod.SpecError(
        f"could not produce a valid spec in 2 attempts. Last error: {last_error}")


# ── video ─────────────────────────────────────────────────────────────────────

def fetch_transcript(url):
    """Transcript for a YouTube URL, via youtube-transcript-api with a yt-dlp
    fallback. Raises RuntimeError with a usable message when neither works —
    'no transcript' must be a clear failure, not an empty string that silently
    becomes a strategy authored from nothing.
    """
    vid = None
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    if m:
        vid = m.group(1)
    if not vid:
        raise RuntimeError(f"could not find a YouTube video id in {url!r}")

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        parts = YouTubeTranscriptApi.get_transcript(vid)
        return " ".join(p["text"] for p in parts)[:MAX_TRANSCRIPT_CHARS]
    except ImportError:
        pass
    except Exception as e:
        print(f"[author] youtube-transcript-api failed ({e}); trying yt-dlp",
              file=sys.stderr)

    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--write-auto-sub", "--sub-format",
             "vtt", "--output", "-", "--print", "%(subtitles)s", url],
            capture_output=True, text=True, timeout=120)
        if out.returncode == 0 and out.stdout.strip():
            return _strip_vtt(out.stdout)[:MAX_TRANSCRIPT_CHARS]
        raise RuntimeError(out.stderr.strip()[:300] or "yt-dlp returned nothing")
    except FileNotFoundError:
        raise RuntimeError(
            "no transcript source available. Install one:\n"
            "  pip install youtube-transcript-api\n"
            "  or: brew install yt-dlp") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp timed out") from None


def _strip_vtt(text):
    keep = [ln for ln in text.splitlines()
            if ln.strip() and "-->" not in ln and not ln.startswith("WEBVTT")]
    return " ".join(keep)


VIDEO_PROMPT = """Below is the transcript of a video claiming a trading
strategy. Extract, honestly:

1. The strategy as stated — markets, side, thresholds, horizon, sizing.
2. What the video CLAIMS worked.
3. What EVIDENCE it actually shows. Usually none. Say so plainly if so.

Videos overstate. Separating the claim from the evidence is the point of this
step: a plausible video with no backtest is an untested idea, not a strategy.

Return a plain-English description of the strategy suitable for turning into a
spec, then one line: "EVIDENCE: <what was actually shown>".

TRANSCRIPT
{transcript}"""


def from_video(url, client=None, model=MODEL):
    """YouTube URL -> (spec, assumptions). The spec carries `provenance` so a
    failing strategy can be traced back to the video that sold it."""
    transcript = fetch_transcript(url)
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=1500,
        messages=[{"role": "user",
                   "content": VIDEO_PROMPT.format(transcript=transcript)}])
    idea = response_text(resp)
    return author(idea, client=client, model=model,
                  provenance={"source": "video", "url": url,
                              "extracted_idea": idea[:2000]})


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Author a strategy spec.")
    ap.add_argument("idea", nargs="?", help="the strategy idea, in English")
    ap.add_argument("--video", help="YouTube URL to extract an idea from")
    ap.add_argument("-o", "--out", help="write the spec here (default: stdout)")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args(argv)

    if not args.idea and not args.video:
        ap.error("give an idea or --video")

    try:
        if args.video:
            spec, assumptions = from_video(args.video, model=args.model)
        else:
            spec, assumptions = author(args.idea, model=args.model)
    except (spec_mod.SpecError, RuntimeError) as e:
        print(f"[author] {e}", file=sys.stderr)
        return 2

    # Assumptions are shown BEFORE anything is written or backtested. An
    # unstated assumption is how an idea quietly becomes a different idea.
    if assumptions:
        print("\n  Assumptions made (the idea did not specify these):",
              file=sys.stderr)
        for a in assumptions:
            print(f"    - {a}", file=sys.stderr)

    text = json.dumps(spec, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"\n  -> {args.out}\n  Nothing auto-promotes: read the diff, then "
              f"backtest it.\n", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
