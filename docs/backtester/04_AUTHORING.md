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

- [ ] Prose -> valid spec for three worked examples
- [ ] Invalid LLM output rejected with a useful message, retried once, then given up on
- [ ] Invented signal names always fail
- [ ] Assumptions surfaced before backtest
- [ ] Transcript fetch works, with a fallback and a clear error when unavailable
- [ ] `provenance` recorded on every video-authored spec
