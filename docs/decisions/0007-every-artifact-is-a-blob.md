# 0007 — every artifact is a blob; current state is derived, not stored

- Status: accepted
- Date: 2026-06-26
- Supersedes: 0004 §"Events are NOT blobstore blobs" / §"the event store" (events are now blobs); 0006
  §"the takeaway log" / §"persistence: the takeaway log" (takeaways are now blobs, current via the
  TimeMap, not a stream-log fold). Refines 0001/0002's event-store and 0006's clustering-in-the-ledger.
- Superseded by: —

This is a MODEL ADR: it records the unified storage model that the LLM stages and the review stage
build on. The refactor it implies is staged (below), done behind the green tests; code is the source
of truth for the formats once written.

## Context

Storage had grown three shapes: the **content-addressed blobstore** (raw, cleaned, chunkset —
immutable, versioned, lineage-linked); **append-only stream logs** (`events/*.jsonl` for glean events
and dream takeaways, via `runlog`); and **processed ledgers** (`state/*-processed-*.jsonl` for
idempotency). ADR-0004 deliberately kept LLM output *out* of the blobstore, reasoning that
content-addressing a non-deterministic output is wrong (the same input yields different bytes each
run, so there is no stable hash and no dedup).

sulin's call collapses the three into one: **every artifact is a blob; a queue holds only references
into the store; "what is currently valid" is tracked by us, not by jj or a side file.** This ADR
records that unified model and why ADR-0004's objection dissolves.

## Decisions

### 1. Every artifact is a blob — the deterministic id is the `source_id`, the content is the version

ADR-0004's objection is dissolved by splitting identity from content: a logical artifact has a
**deterministic, stable `source_id`**; each production of it is an immutable **version** (a snapshot,
`prev`-linked → the Memento TimeMap). Non-determinism lives in the version's *content*, not its
identity — so re-extraction, re-synthesis, and refinement are simply *new versions of a stable
source*, exactly what the blobstore's TimeMap already models.

| artifact   | `source_id` (deterministic, stable)          | a new *version* is…                | kind     |
|------------|----------------------------------------------|------------------------------------|----------|
| transcript | session id                                   | a re-fetch (tap)                   | raw      |
| cleaned    | content hash                                 | (deterministic — re-render no-ops) | derived  |
| chunkset   | content hash                                 | (deterministic — re-chunk no-ops)  | derived  |
| event      | `event_id` = hash(cleaned_hash : byte span)  | a re-extraction (new prompt/model) | raw      |
| takeaway   | `cluster_signature` = hash(member event ids) | a re-synthesis (new prompt/model)  | raw      |
| concept    | a minted concept id                          | a refinement (review accept/edit)  | raw      |
| decision   | (a unique fact — hash is its id)             | — (decisions are never re-versioned)| raw      |

`kind` keeps ADR-0003's retention axis: **raw** = ground-truth / non-deterministic content (kept
forever, versioned) — transcripts, events, takeaways, concepts, decisions; **derived** =
deterministically rebuildable (TTL-eligible) — cleaned, chunkset. Versioning (`source_id` + `prev`)
applies to raw kinds because they *evolve*; derived kinds are content-addressed transforms linked by
`derived_from`. Content-references that are intrinsic provenance — a takeaway's `cites`,
`member_events`, and `supersedes`; an event's `cleaned_hash` + span — live *in* the version. (The
ephemeral clustering stays unstored, recomputed each dream run — ADR-0006.)

### 2. References, not copies

Lineage and every collection are expressed as **hashes**. A takeaway references its events; a concept
references the takeaway and evidence spans it was minted from; a decision references its target. **A
queue is a derived list of references** — it holds no artifact data, only the hashes of the blobs to
act on.

### 3. State changes via decision blobs (append-only, immutable)

A **decision** is a blob that references a target artifact and records a state transition. Decisions
are the *one* mechanism for all mutable state — there is no status field flipped in place (which
immutability forbids anyway) and no side ledger:

- **review:** `accept` (also references the minted concept) / `reject` / `snooze` (with a concrete
  re-surface trigger) / `edit` (captures before+after) — referencing a takeaway.
- **retire:** a decision referencing a concept that contradiction/review takes out of the valid set.
- **producer "done" markers:** a `processed` decision referencing an input (a chunkset for glean, the
  event-set/clusters for dream), keyed by `(stage, prompt_version, model)` — this **folds the
  idempotency ledger into the blob model** (decision #5).

The current state of any target = the **latest decision referencing it** (recency-folded), or none.

### 4. Current / validity / the queue are DERIVED — never stored

This is the answer to "how do we track what's valid": *we don't store it, we query it* — the same
move the blobstore already makes with `latest_version` (scan the sidecars, walk `prev`; the current
view is a query over immutable history, not stored mutable state). One notch up:

- **current version of a source** = `latest_version(source_id)` (already exists).
- **valid concepts** = scan concept sources → latest version each → drop those with a `retire`
  decision (and drop superseded). A query.
- **pending review queue** = scan takeaway sources → latest version → drop superseded → drop those
  with a terminal decision (accepted/rejected) or a not-yet-due snooze. A query returning *references*.
- **glean/dream to-do** = inputs with no `processed` decision for `(stage, prompt_version, model)`.

Nothing stores "the valid set" or "the queue." Per ADR-0002 these scans are O(total blobs); an index
remains a rebuildable, deletable cache, introduced only when a linear scan actually hurts.

### 5. Idempotency folded in; crash-safety preserved

"Already done" = a `processed` decision blob exists for the input + `(stage, prompt_version, model)`.
The ordering that made the old stream model crash-safe carries over: **output blobs are committed
first (each crash-safe on its own — content then meta-as-commit), the `processed` marker last.** A
crash before the marker re-processes the input next run; re-extraction/re-synthesis produces a *new
version of the same deterministic `source_id`*, so the duplicate is absorbed as a version (latest
wins) rather than a conflicting record. The blobstore's existing content-first/meta-last commit is the
only atomicity primitive needed; the `runlog` stream substrate (`.partial` shards, glob+merge) is
retired.

## Consequences

- **Good:** one storage model, one validity mechanism (`latest_version` + decision-fold) for events,
  takeaways, concepts, and the queue; the trust chain and the TimeMap reach every artifact uniformly;
  "what did we believe, and when" is answerable for *all* of it (decisions + versions are an immutable
  audit trail); the review skill builds on blob references, not soon-removed stream logs; no
  dependency on jj or loose files for any data.
- **Costs / known limits:** deriving current state is O(total blobs) until an index is added (ADR-0002
  defers it); a crashed producer leaves orphan output-version blobs (harmless — superseded by the
  next run's version, and a future GC reclaims unreferenced derived blobs); the store grows
  monotonically (every version + every decision retained) — compaction/excision stays the deferred
  item it is for dream's tombstones (ADR-0006).
- **Migration (staged, behind green tests):**
  1. blobstore: let raw `ingest` carry the new `source_kind`s (event/takeaway/concept/decision) and a
     `references` field; add a `latest-by-kind` scan helper.
  2. glean: write event blobs (source_id = `event_id`) + a `processed` decision, instead of
     `events/glean-*.jsonl` + the processed ledger.
  3. dream: write takeaway blobs (source_id = `cluster_signature`) + `processed` decisions; `current`
     and supersession derive from the TimeMap.
  4. retire `runlog`'s stream/ledger code (the crash-safe commit lives in the blobstore already).
  5. review builds natively on this: pending = a query; accept = ingest a concept + a decision.

## References

Grounded in the append-only / immutable-store prior art from the dream research pass (see
[[ratchet-dream-prior-art]]): git's object model + refs (immutable objects by hash; "current" is a
derived ref); Datomic (the database as a value, assertion/retraction, history as a query); event
sourcing / CQRS (append-only facts, current = a projection); CRDT tombstones (delete = a superseding
fact, not a removal); Memento RFC 7089 (the TimeMap as a first-class version history).
