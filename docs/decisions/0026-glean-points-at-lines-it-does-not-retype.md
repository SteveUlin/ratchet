# 0026 — glean points at lines; it does not retype evidence

- Status: accepted — implemented 2026-06-30 (all 21 offline suites green)
- Date: 2026-06-30
- Supersedes: the quote-as-locator trust anchor of ADR-0004 (the event MODEL — span pointer, untrusted
  summary — is unchanged; only HOW the span is obtained changes).
- Superseded by: —

Code (`glean.number_lines`, `glean.resolve_lines` replacing `glean.verify`; `SYSTEM_PROMPT` + `_user_prompt`;
`PROMPT_VERSION = "glean/4"`) is the source of truth; this records the *why*.

## Context

glean's evidence is a byte SPAN into the immutable cleaned blob — the event never stores text, and the
verbatim evidence is `get(cleaned_hash)[span]`, copied from the frozen blob (ADR-0004/0007). That part was
always right. The problem was how glean *obtained* the span: the model returned a `quote` (the exact
transcript text), and glean located it with `cleaned_bytes.find(quote)`, accepting the event only if the
quote was a real substring. The model was made to **reproduce transcript bytes**, and the system
string-searched for them.

That reproduction is fragile. When the model trims whitespace, normalizes a smart-quote, joins across a gap
with "…", or paraphrases by a character, `.find()` misses and the candidate is **dropped**. In the first
real trial (2026-06-30, Haiku over interactive sessions) **15 of 51 candidates — 29% — were rejected this
way.** Some were genuine hallucinations that *should* die; but the failure mode cannot tell a fabricated
learning from a real one whose locator was mistyped, so real learnings were lost. It also spends output
tokens having the model re-type text it is only pointing at.

The naive fix — "have the model return byte offsets" — is worse: LLMs cannot count characters/bytes, so
direct offsets are confidently wrong. A quote is at least self-locating. The author chose quote-then-search
for exactly that reason.

## Decision

**Separate SELECTION (the model's job) from TRANSCRIPTION (the system's job).** The chunk is presented to the
model with a **line number on every line** (`12| ...`); the model returns the inclusive line range its
evidence lives on (`{"from": N, "to": M}`); the system copies those lines' bytes from the immutable blob.

- `number_lines(cleaned_bytes, ch)` renders the numbered excerpt AND returns the parallel map
  (line → `(byte_start, byte_end)` in the cleaned blob). Lines split on `\n`; a line's span excludes its
  trailing newline.
- `resolve_lines(candidate, line_spans, cleaned_bytes)` maps a line selection to a `[start, end)` span. It
  is **tolerant by design** (recall-first): accepts `{from,to}`, a bare int, or `[i,j]`; swaps a reversed
  range; **clamps** out-of-range numbers into the chunk (a near-miss still yields real, nearby evidence).
  Only a selection it cannot read at all (no `lines`, non-numeric, empty chunk) — or one resolving to an
  empty/whitespace-only span — returns None.
- Evidence granularity is now the **line**: evidence is the line(s) carrying the learning, ⊇ the precise
  phrase. Coarser, but always exact (chosen over precise-but-fragile; sulin, 2026-06-30). Sentence/phrase
  granularity is the same mechanism at finer grain, deferred until line evidence proves too coarse in review.

The event record, `event_id` (span-derived), the store, lineage, and idempotency are unchanged — only the
span's provenance changed. `PROMPT_VERSION` bumps to `glean/4` so a re-run re-extracts over the same frozen
chunks.

## Why this is a STRONGER trust anchor, not a weaker one

The old anchor was "the retyped quote is a real substring." The new anchor is "**the model never touches the
evidence bytes**." Worst case the model points at the wrong lines — but the bytes are always real,
copy-pasted from the frozen blob. Fabricated evidence is impossible by construction; there is nothing to
verify because there is nothing retyped. The 29% drop becomes near-zero (only truly unreadable selections),
and the cost is fewer output tokens.

This also enacts the project's wider turn (sulin, 2026-06-30) from hidden hard rules toward
explained-and-trusted models: the prompt now *explains the mechanism* ("you point, we copy, because copied
bytes can't drift") instead of *threatening a rule* ("retype verbatim or it's discarded").

## Consequences

- `glean.verify` and `MIN_QUOTE_BYTES` are gone. Downstream is untouched: events never stored a `quote`
  (dream/review/garden already resolve quotes from spans), so nothing else changed.
- Evidence spans are line-aligned (slightly larger). dream's robust-anchor quote = the resolved line text.
- Test fakes that drove glean by a `quote` now translate it to a line selection by finding it in the
  numbered prompt (so a quote is still "found" only by its owning chunk); session fixtures put the durable
  line ALONE on its rendered line so a line-selection resolves to exactly it.
- The line-numbering format is a new prompt contract; a future renderer change to the cleaned blob's line
  structure would shift selections, but never produces fabricated bytes (worst case: coarser/empty → reject).
