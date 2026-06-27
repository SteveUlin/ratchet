# 0009 — the uniform Block abstraction: one driver, per-item commit, streaming progress

- Status: accepted
- Date: 2026-06-26
- Supersedes: the per-stage ad-hoc `run()`/`materialize()` drivers (0004 glean, 0006 dream; weave/chunk
  single-item). Generalizes 0007's per-input `processed` markers to per-ITEM. Glean's idempotency unit
  moves from the chunkset to the chunk.
- Superseded by: —

Code (`ratchet/block.py` + each stage) is the source of truth; this records the *why*.

## Context

The stages grew organically and ended up at different abstraction levels with ad-hoc APIs: glean/dream
got a batch `run()` with idempotency; weave/chunk are single-item `materialize()` with no `--all`, no
batch driver; none stream progress, and glean's unit of persistence is the whole *chunkset* — it
buffers a session's events in memory and commits them (plus the done-marker) only when the entire
chunkset finishes. A backfill (`glean --all` over ~234 sessions) exposed all three at once: **no
visible progress** (output batched to the end), **work that doesn't persist** (Ctrl-C on a giant
session loses every chunk's work), and **inconsistent APIs**. sulin: "all blocks at the same
abstraction level and similar APIs."

## Decisions

### A Block is `(enumerate items, process one)`; a shared driver does everything else

A stage implements a tiny interface — its two stage-specific bits — and inherits the rest:

```
class Block:
    name: str
    params: tuple                      # the idempotency params (e.g. prompt_version, model) — the done-key suffix
    def items(root, *, source_id=None) -> Iterable[Item]    # enumerate the inputs to process
    def key(item) -> str                                    # the item's stable, deterministic id
    def process(item, *, root, run_id) -> tuple[int, float] # transform → INGEST output blobs; return (n_outputs, cost)
    def finalize(processed, *, root, run_id) -> None        # OPTIONAL cross-item pass (default: no-op)
```

The shared `block.run(block, *, source_id=None, max_usd=None, limit=None, progress=…, root=None) ->
Report` driver handles, **identically for every stage**: enumeration, the done-skip, per-item error
isolation, budget (`--max-usd`), `--limit`, `--dry-run`, crash-safety, and — the three fixes —
**incremental persistence, streaming progress, and uniform idempotency** below.

### Per-item commit — work persists (fix #2)

The driver processes one item at a time and commits it fully before the next: `process` ingests the
item's output blobs (each crash-safe via the blobstore's content-then-meta commit), then the driver
writes the item's `processed` marker LAST. So a kill or crash keeps every completed item and re-does
only the one in flight — never a whole session's work. This is 0007's commit-marker-last ordering,
applied per item instead of per run.

### Streaming progress — the run is watchable (fix #1)

The driver calls a `progress(report, item, n, cost)` callback as each item lands; the default prints
one line per item (`glean  812 items · 47 done · 0 events · $0.21`), `--quiet` suppresses it, `--json`
emits structured lines. No stage batches output to the end.

### Uniform idempotency, generalized from 0007

"Done" = a `processed` marker exists for `(stage, key(item), *params)` — a `processed` decision blob
(0007 §3), one per item. `block.run` derives the done-set in one scan and skips members; bumping a
param (prompt/model) re-does the items. This is exactly glean/dream's old `processed_index`, lifted
into the driver and applied at item granularity.

### Item granularity — the smallest unit of work (the heart of fix #2)

| stage | item | output | params |
|-------|------|--------|--------|
| tap   | a transcript file (via the fingerprint cursor) | raw blob | — |
| weave | a raw blob | cleaned blob | render_version |
| chunk | a cleaned blob | chunkset | render_version, budget |
| glean | **a chunk** (one LLM call) | event blobs | prompt_version, model |
| dream | a cluster (one LLM call) | takeaway blob | prompt_version, model |

**Glean's unit drops from the chunkset to the chunk.** One chunk = one Haiku call = one commit, so a
giant session no longer loses all its work on Ctrl-C and progress ticks per chunk. A filter-skipped
chunk still gets a 0-output marker so the done-set stays exact. (The chunkset becomes just the
container glean enumerates chunks from.)

### Uniform CLI + Report

Every block CLI is `--all` | `--source-id <id>`, with `--max-usd`, `--limit`, `--dry-run`, `--quiet`;
every block returns the same `Report` (examined / processed / skipped / errored / outputs / cost_usd /
stopped_on_budget). One mental model, one surface. (dream is the exception: it clusters the *whole*
event pile, so `--all` is implicit and `--source-id` is N/A; it keeps `--limit`/`--max-usd`/`--dry-run`/
`--quiet`.)

### dream's cross-item supersession uses `finalize`

dream is the one block with a genuine cross-item dependency: coverage-conditioned supersession needs
the whole run's emitted takeaways before any `supersedes` is known (ADR-0006). So dream streams
synthesis progress per cluster but commits in `finalize` (the supersession pass) — its persistence
stays per-run, a documented exception. (Making dream per-item too means deriving supersession at fold
time instead of storing it; deferred — dream is rare and small, so per-run commit is not the pain.)

### review stays the human gate

review is not a batch transform, so it keeps its own surface (`--pending`/`--accept`/…). Its
read-derivations already ride the same blob primitives, so it stays consistent with the model.

## Consequences

- **Good:** backfills are watchable and resume at chunk granularity; one driver, one CLI, one Report
  across tap/weave/chunk/glean/dream; weave/chunk gain `--all` + progress + idempotency they lacked;
  the per-item commit makes Ctrl-C cheap and safe everywhere; the substrate (`block.py`) is the single
  tested home for the cross-cutting concerns (what `runlog` was, now over the blob model).
- **Costs / known limits:** one `processed` marker per item — for glean, one per *chunk* (hundreds–
  thousands on a full backfill), O(total blobs) to scan until 0002's index lands (markers are tiny);
  dream's per-run commit (the `finalize` exception) until supersession is moved to a derived fold;
  global enumeration (dream gathers+clusters in `items`) is recomputed each run, as today.

## References

ADR-0007 (every artifact is a blob; `processed` markers as decision blobs — this generalizes them to
per-item). The retired `runlog` (the prior crash-safe producer substrate; `block.py` is its
blob-model successor). The backfill experience (2026-06-26) that surfaced all three issues at once.
