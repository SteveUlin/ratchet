# 0001 — Storage layers, blobstore, and concurrency

- Status: accepted
- Date: 2026-06-26
- Supersedes: —
- Superseded by: —

A decision is superseded by a **new** ADR, never edited in place. Code (`ratchet/*`) is the
source of truth for the formats; this records the *why*.

## Context

ratchet mines signals (corrections, preferences, explicit instructions) from sources —
transcripts first, later PR comments / Slack — into reviewable edits to CLAUDE.md / rules /
skills. V0 covers the transcript source and the ingest path: **fetch → blobstore → event
generation**. ratchet is the only thing that creates events.

## Decisions

### 1. Three storage layers

- **Datastore** — the external original source (Claude transcripts in `~/.claude/projects`,
  later GitHub PRs, Slack, docs). Mutable, evolving, not owned by ratchet; read-only to us.
- **Blobstore** — ratchet's immutable, **content-addressed, versioned** freeze of the raw
  material we analyze (see §2). The durable ground truth.
- **Event store** — append-only files of extracted signals that **point into** the blobstore.
  Ratchet is the sole producer, so this is a single stream (see §4).

Chain: **event → blob → datastore.** Freezing the blob keeps evidence verifiable forever,
even after the original transcript is compacted or deleted.

All three live in the configurable data dir (`$RATCHET_DATA_DIR`, else `$XDG_DATA_HOME/ratchet`,
else `~/.local/share/ratchet`), **outside the code repo and not under jj**. jj/git tracks
only code and (later) the reviewed config projections.

### 2. Blobstore: immutable snapshots, versioned per logical source

A blob is an **immutable, content-addressed snapshot** of one chunk (a bounded window of
normalized source text — the exact thing the extractor analyzes).

- Content at `blobs/<hash[:2]>/<hash>`; sidecar `<hash>.meta.json` =
  `{content_hash, source_kind, source_id, origin_ref, fetched_at, prev}`.
- **Versioning.** The same logical source evolves — a Slack thread gets replies, a doc gets
  edited, a PR gets comments. A blob records a stable **`source_id`** (the logical thing)
  plus `fetched_at` and `prev` (the previous snapshot's hash). Re-fetching an unchanged
  source yields the same hash (dedup / no-op); a changed source yields a new snapshot linked
  by `prev`.
- **Blobstore tracking.** An append-only tracking ledger (`blobs/tracking/<tap-run>.jsonl`)
  records each fetch as `{source_id, hash, fetched_at, …}`, so version history per logical
  source is queryable without scanning every sidecar.
- **Reconciling updates is a future-us problem.** V0 records version lineage; it does NOT
  diff versions or supersede events when a source changes.

### 3. Fetch is separate from event generation

- **Fetch** (datastore → blobstore): select interesting chunks, freeze each as a versioned
  blob with a datastore backlink. Deterministic, cheap, **no LLM**. (Tool: `tap`.)
- **Generate** (blobstore → events): run extraction over frozen blobs; emit events pointing
  at blobs. **Source-agnostic** — only ever sees blobs, never the mutable datastore.
  (Tool: `glean`.)

Splitting them lets you re-fetch without re-extracting, and re-extract (improved prompt) over
the same frozen blobs without re-fetching. Extraction is reproducible.

### 4. Concurrency = single-writer-per-file ownership

No file ever has two writers ⇒ no locks, no per-write atomicity problem.

- **Blobs** are content-addressed and immutable: write to a temp file, `os.replace()` into
  place; an existing hash is a no-op. (Atomic rename once per blob.)
- **Events.** Ratchet is the **only** event producer (`glean`), so events are NOT split into
  per-source/per-producer streams — there is one event stream. Concurrent `glean` runs stay
  safe via per-run shard files (`events/glean-<run_id>.jsonl`), written `.partial` then
  renamed on clean exit; a reader globs + merges. Single writer per file ⇒ plain append is
  safe (the 4 KB `PIPE_BUF` atomicity limit only bites files *shared* by processes).
- **Consumers** (the analysis / judge tool) never write producer files; they keep their OWN
  pointer files (cursors over the event stream; indexes that select / dedup by id).
- **Ledgers** (blob tracking, extraction-processed) use per-run shard files; readers merge.

### 5. Event format (`ratchet/model.py`) — a thin pointer

One event = one discrete signal:

- `blob` + `evidence[].span` — points at the frozen raw text; the verbatim quote is
  `blob[span]`. **Trusted.**
- `summary` — model-generated, one imperative sentence, reusable. **Untrusted** until judged.
- `signal`, `confidence`, `producer{stage, model, run_id, cost_usd}`, `id`, `supersedes`,
  `status`. (`source_kind` / `source_id` are reachable via the blob, not duplicated.)

The judge re-reads the blob, never the summary. `glean` verifies each quote is a real
substring of the blob before accepting an event, so a hallucinated quote is rejected
deterministically.

### 6. Identity & dedup

- Blob id = `sha256(content)`. Event id = `sha256(blob + first-quote span)[:12]` so pointer
  files reference it and consumers dedup across runs. Producers are dumb appenders; **dedup
  is a consumer concern**.
- Re-fetch / re-extract idempotency via per-run ledgers keyed by datastore span / blob hash.

## Not in V0

- Sensitivity / trust-zone tagging per source (transcript source is local-only).
- Reconciling source **updates** across blob versions (diff, supersede affected events) —
  V0 records lineage but does not resolve it.
- "Interesting" selection beyond chunking (cost-sampling heuristics) — V0 freezes all
  active-path chunks.
- The analysis / judge tool, proposal queue, review/apply, and usefulness scoring.
