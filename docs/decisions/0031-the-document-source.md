# 0031 — the document source: hand-written rules join the concept layer

- Status: accepted — implemented 2026-07-04 (all 28 offline suites green, incl. the new
  `test_document`)
- Date: 2026-07-04
- Supersedes: —
- Extends: ADR-0002 (tap the fetcher), ADR-0025 (tap does not eat its own tail), ADR-0026 (glean
  points at lines), ADR-0023 (valid-time weighting).

Code (`tap --file`/`read_document`; `weave.render_document`/`strip_generated`/`DOC_RENDER_FORMAT`;
`chunk.DOC_CHUNKSET_FORMAT`; `glean.DOC_SYSTEM_PROMPT`/`DOC_PROMPT_VERSION` + `ChunkItem.mode`;
`blobstore.project_of`'s path fallback; `dream._session_valid_times`'s document arm) is the source
of truth; this records the *why*.

## Context

The owner's hand-written `~/.claude/CLAUDE.md` is knowledge ratchet cannot see. The cost is not
hypothetical: the novelty digest judges every gleaned event against "what we already know", and
what he already WROTE DOWN isn't in it — so his oldest, most settled rules keep surfacing as
`novel` claims he must re-review, and they sit outside decay/contradiction tracking (a lived
session contradicting a written rule produces no contradicts edge, because the rule has no claim).
The store needed a way to ingest a curated FILE, not just a session.

Two design pressures shaped the mechanism:

1. **The self-loop.** `generate --apply` writes a marked region into the very file being ingested.
   Naïve ingestion would feed ratchet's own projection back in as fresh evidence — the concept
   layer corroborating itself. ADR-0025 met the same failure one layer down (tap re-ingesting its
   own completer transcripts) and its answer holds here: prevent structurally, don't detect.
2. **Self-maturation.** Maturity = distinct corroborating sessions. If every save/re-tap of the
   file counted as a new "session", a rule would mature by being written once and saved twice —
   assertion masquerading as corroboration.

This is also the pilot for the researcher pre-tap source (fetched PDFs/webpages): same
explicit-file ingest, same no-conversation-structure render, so the mechanism stays general where
that is free.

## Decision

**A `document` source kind: `tap --file PATH` ingests a file verbatim; its path is its source AND
its session; weave renders it minus the generated region; glean extracts rules under a document
prompt with its own version knob.**

1. **Verbatim ingest, the transcript machinery reused.** `tap --file` reads the file with a STRICT
   utf-8 decode (the raw blob is the true bytes — nothing mangled enters the store),
   `source_id = the absolute resolved path`, `origin_ref = {path, session_id: path, mtime,
   size_bytes}`. The (size, mtime, hash) fingerprint cursor and the version fold apply unchanged:
   an untouched file is never re-examined, a changed file is a new prev-linked VERSION of the same
   source. `--file` refuses the sweep selectors (`--project`/`--last`/`--since`/`--exclude`) —
   they would be silently inert, a hidden rule (ADR-0027 posture).

2. **PATH-AS-SESSION — the epistemology.** All versions of one file are ONE session. So: a
   re-tapped identical rule lands on resolve's exact-dup fast path and corroborates
   deterministically into its claim with ZERO added maturity (same session — no new
   distinct-session support); an edited rule seeds a fresh claim, or adjudicates only against
   LIVED claims (different sessions — the same-session gate blocks document-vs-itself residue
   calls); maturity comes only from the owner's real sessions living the rule, or his direct
   accept at review. A document asserts; it never corroborates itself.

3. **THE RENDER-TIME REGION GUARD (non-negotiable).** `weave.render_document` copies the file's
   text MINUS every `<!-- ratchet:generated START …` → `<!-- ratchet:generated END -->` region
   (`strip_generated`; an unterminated START strips to EOF — when in doubt, exclude). Excluding at
   RENDER time makes the guard STRUCTURAL: every downstream span points into the cleaned blob, and
   the cleaned blob does not contain the region, so no event can ever cite generated text. The
   markers couple to `generate.START`/`END` by convention, not import (importing would cycle
   generate→dream→glean→weave); `test_document` pins the exact marker text as the drift tripwire.

4. **A line-preserving passthrough render.** One `[document] <path>` header turn (the speaker-tag
   analogue), then the stripped body split into turns on the exact `"\n\n"` join separator —
   split+join is the identity, so turns tile the cleaned blob byte-for-byte, chunk packs whole
   paragraphs, and glean's numbered lines match the file's own lines. Formats self-describe:
   `document.render/1` / `document.chunkset/1` beside the transcript shapes, so every consumer
   picks its mode from sidecars alone.

5. **Glean's document mode.** The chunkset sidecar's `source_kind` sets `ChunkItem.mode` (no
   content read). Document chunks get `DOC_SYSTEM_PROMPT` — same JSON contract, same ADR-0026
   pointing discipline, INVERTED prior ("this file is already distilled: most of it IS durable
   signal; restate each rule, don't infer a lesson") — a relaxed pre-filter (no speaker tags to
   require), and no transcript-shaped structural cues. `DOC_PROMPT_VERSION` rides in the DONE-KEY
   (params is a run-level constant and one `--all` run mixes modes), so document extraction
   versions independently. Accepted asymmetry: a transcript `PROMPT_VERSION` bump re-keys doc
   chunks too; their re-extraction re-ingests byte-identically, wasting only a few calls on a
   small corpus.

6. **The seams that make the flow usable.** A document's `--topic` FOCUS handle is its path
   (`blobstore.project_of` falls back to `origin_ref.path`) — but it carries NO
   `project`/`cwd`, so `concepts._repo_label` yields no repo, the subject key stays EMPTY
   (seed-only via subject, ADR-0028 §3.1), and the derived scope proposal is `global`, never a
   path-shaped pseudo-repo. Document sessions join `dream._session_valid_times` dated by file
   mtime — the file's save time is its valid-time, so an untouched rules file decays exactly like
   an unlived lesson (documents joined decay tracking on purpose).

## Why this shape

- **Prevent, don't detect (the guard).** Filtering generated rules downstream (by content
  signature, or by remembering what generate wrote) would be fragile and would still leave the
  bytes citable. Absence from the cleaned blob is a property no downstream bug can undo.
- **Path-as-session is the honest unit of assertion.** A file is one voice speaking once,
  revised. Anything finer (per-version sessions) manufactures corroboration out of saves; anything
  coarser (no session) would break the edge/maturity machinery that expects one. The exact-dup
  path and same-session gate then give the right behavior for free — verified end-to-end, not
  assumed.
- **The mechanism generalizes because it assumes nothing conversational.** Verbatim bytes,
  passthrough render, paragraph turns, "most of this is signal" prompt — a fetched PDF/webpage
  needs exactly this plus a fetcher. The recency clock already handles conversation-less sources
  (glean's `valid-then-arrival`); documents even carry their own valid-time (mtime), so they are
  the easy case.
- **Seeding stays a human act.** Document claims incubate at 1 session by construction; they
  become concepts only through `review --accept`. The pipeline moves the owner's rules TO the
  gate; it never smuggles them past it.

## Consequences

- `tap --file ~/.claude/CLAUDE.md` → weave → chunk → glean → resolve puts every hand-written rule
  in the claim pool; one review sitting seeds them as concepts (RUNBOOK "Documents"). The novelty
  digest then knows them — re-learned rules judge `known`, and a lived contradiction lands as a
  real contradicts edge on a real claim.
- Re-running the flow after `generate --apply` writes into the same file is safe by construction
  (the guard) and cheap by construction (the cursor).
- The marker-text coupling to generate is now load-bearing for the guard; it is pinned by test,
  and drift fails a test rather than silently re-opening the loop.
- Deferred: the researcher pre-tap fetcher itself (URL/PDF → file is manual for now); per-document
  chunk budgets for very large documents (a paragraph-turn document packs fine today); a document
  variant of the review card (the path shows as the session id, which reads fine).
