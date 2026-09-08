# Phase 4 — Authoring: English and video -> spec

*Cheap only because phase 3 exists. The LLM's job is filling in a schema, not
writing a program.*

---

## 1. English -> spec

`wc/backtest/author.py`. Prompt carries the schema, the signal vocabulary, and
worked examples; the model returns JSON; the validator is the gate.

```
prose -> LLM (schema + vocabulary in prompt) -> JSON -> validate -> strategies/*.json
                                                          |
                                                    reject + reason -> retry once
```

Rules:

- **Validation is the contract.** Invalid output is rejected and retried once
  with the error attached. It is never patched by hand into something that
  runs.
- **The model may not invent signals.** Unknown signal names fail validation.
  If a strategy genuinely needs one, that is a schema-extension decision by a
  human (see 03 §5), not an improvisation.
- **Ambiguity is surfaced, not guessed.** "Bet on longshots" does not specify
  side, threshold, or horizon. The author returns its assumptions alongside the
  spec, and they are shown before the backtest runs.
- **Nothing auto-promotes.** Authoring produces a candidate file. A human reads
  the diff before it is committed.

## 2. Video -> spec

Same path with a transcript in front.

```
YouTube URL -> transcript (youtube-transcript-api, yt-dlp fallback)
            -> LLM extract: claimed strategy, thresholds, markets, evidence offered
            -> the SAME English->spec path
```

Extra care, because video is a low-quality source:

- **Record the source.** `provenance: {url, title, timestamp_range}` on the
  spec. When a strategy fails we need to know which video sold it.
- **Separate claim from evidence.** The extractor reports what the video
  *asserts* worked and what it *shows*. Usually nothing. That distinction goes
  in the spec description.
- **Videos overstate.** Backtest results are the arbiter, and a plausible video
  with a failing backtest is a failing strategy. This is exactly the case the
  repo's existing evidence-quality note was written for.

## 3. Definition of done

- [x] Prose -> valid spec, verified against the live API
- [x] Invalid LLM output rejected with a useful message, retried once, then given up on
- [x] Invented signal names always fail
- [x] Assumptions surfaced before backtest — printed to stderr before the file is written
- [x] Transcript fetch works, with a yt-dlp fallback and a clear error when unavailable
- [x] `provenance` recorded on every video-authored spec

### Notes from building it

**The vocabulary in the prompt is generated from `spec.py`, never restated.** A
hand-maintained copy drifts, and the model is then told about signals the
validator rejects — which reads as hallucination when it is actually
misinformation.

**Reasoning models put a `ThinkingBlock` first,** so `resp.content[0].text`
raises. `author.response_text()` joins every text block instead.

**`temperature` was removed from `messages.create` in `anthropic>=1.4`.** It was
still being passed in `brain.py`, `brain_v4.py`, and `lib/brain_model.py`, where
the caller swallowed the exception into an empty dict — so the live system's LLM
leg had been silently doing nothing. Fixed 2026-09-08.
