# 0037 — the external-reference extraction mode: a fetched page is not the owner's rules

- Status: accepted — offline suites green (all 30; `test_document` gains §4b, `test_subject` gains
  the document-union regression). Confirmed live: re-gleaning the Claude Code best-practices page
  went 33 events (1 chunk REFUSING under the doc prompt) → 56 events (0 refusals) under the reference
  prompt.
- Date: 2026-07-07
- Extends: ADR-0031 (the document source — owner-authored files), ADR-0033 (the `--url` source —
  which routed fetched pages through the owner-authored document prompt), ADR-0026 (the model points,
  the code copies — the trust anchor, untouched), ADR-0036 (glean is blind — reference mode is blind
  too; no digest), ADR-0029 (the kind facet — a source's claim skews `reference`, not `behavioral`).

Code (`glean.py`: `REF_SYSTEM_PROMPT`, `REF_PROMPT_VERSION = "glean-ref/1"`, the `mode == "reference"`
routing in `items()`/`key()`/`process()`/`has_signal_potential`) is the source of truth; this records
the *why*.

## Context

ADR-0031 introduced ONE document prompt, framed for its use case — the owner's own CLAUDE.md:
"You extract the durable rules from an excerpt of a curated rules/notes document its owner WROTE."
ADR-0033 added `--url` and routed fetched pages through that SAME prompt. But a fetched webpage is
not a document the owner wrote — it is THIRD-PARTY reference material. The mismatch surfaced the
first time a real external page was tapped: on Anthropic's Claude Code best-practices page the model
returned `{"events": []}` with a note — *"this excerpt is from Anthropic's public Claude Code
documentation, not a personal rules document written by the user"* — and refused. It was inconsistent
(a chunk that read like distilled principles extracted anyway; a chunk that announced itself as docs
refused), so extraction depended on whether a page happened to LOOK owner-authored.

"Document" conflated two genuinely different epistemic kinds:

- **Owner-authored** (`tap --file` a CLAUDE.md): the owner WROTE it; each line is their DIRECTIVE,
  high authority, skews `behavioral`.
- **External reference** (`tap --url` a page/PDF): a THIRD PARTY wrote it; each line is a CLAIM THE
  SOURCE MAKES that the owner found valuable, skews `reference`.

Both are `source_kind == "document"`; they differ in `origin_ref` — a `--file` doc carries `path`, a
`--url` doc carries `url` (ADR-0033's `_process_url`).

## Decision

**A third extraction mode, `reference`, for `--url` sources — a sibling of the document prompt, not
the transcript one.**

1. `REF_SYSTEM_PROMPT` inherits the document prompt's mostly-signal, restate-don't-infer posture (a
   curated article is dense, unlike a noisy transcript) but DROPS the "rules its owner WROTE" framing:
   each event is "a CLAIM THE SOURCE MAKES, never a directive the user issued." The summary states the
   claim as the source makes it, not as the user's rule; markers expect `insight`/`research` (external
   knowledge), not the all-low of a bare directive.
2. `items()` sets the mode: `source_kind == "document"` AND `origin_ref.url` present → `reference`;
   else `document`; else `transcript`. One `raw_meta_of` hop per document chunkset (all its chunks
   share one cleaned blob → one origin), cached with the `--source`/`--exclude` reads.
3. `key()`/`process()`/`has_signal_potential` route on the three modes. Reference keys on
   `glean-ref/1`, independent of `glean-doc/2` — a `--url` doc and a `--file` doc never share a
   done-key, and each prompt versions on its own.

## Why this shape

- **A page is closer to a curated doc than to a transcript.** The transcript prompt expects mostly
  NOISE (the empty list is the common answer); a fetched article is mostly SIGNAL. Reusing the
  transcript prompt would under-extract. So the reference prompt is the document prompt with the
  authorship claim removed — the "similar but not identical" the owner asked for.
- **Authorship, not just tone, is the fix.** The refusal was specifically the model refusing to treat
  third-party docs as the user's own rules. Removing that assertion removes the refusal — measured:
  0 refusals, 33→56 events on the same page.
- **The authority distinction rides DOWNSTREAM, not into the trust chain.** Evidence is still bytes
  copied from the page by line number (ADR-0026); the reference framing only shapes the untrusted
  summary and which claims surface. A source's claim proposes a `reference`-kind concept far more
  often than a `behavioral` one — the reviewer confirms the kind at the gate (ADR-0029), where a
  third-party claim gets exactly the scrutiny a personal directive would not need.

## Consequences

- `tap --url` pages re-glean under `glean-ref/1`; any that were gleaned under `glean-doc/2` (before
  this ADR) re-open for optional, budgeted re-extraction — and SHOULD be re-gleaned, since the doc
  prompt under-extracted or refused them.
- A companion robustness fix (same investigation): `subject.subject_key` no longer runs the TRANSCRIPT
  parser on a document's raw — a document is not a coding session, has no edited-files union, and a
  bare-number line in fetched text json-parsed to an int that `active_path` then `.get`-crashed on.
  The union is format-gated to transcripts and, as an advisory fallback, degrades to empty on any
  parse failure rather than crashing the chunk (the module's "never fatal" contract, now enforced).
- Deferred: a `--file`-tapped EXTERNAL document (an article saved to disk rather than fetched) still
  gets the owner-authored prompt — `--file` is assumed owner-authored. A `--reference-file` flag, or
  inferring from content, is a future refinement; the `--url` split covers the common case.
- Owner-authored (`--file` CLAUDE.md) is UNCHANGED: still `document` mode, `glean-doc/2`.
