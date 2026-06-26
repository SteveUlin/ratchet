# 0003 — weave + chunk: a materialized clean→chunk pipeline over raw transcript blobs

- Status: accepted
- Date: 2026-06-26
- Supersedes: 0001 §5 (provenance span anchor: a quote verifies against the cleaned blob, not the raw blob)
- Superseded by: —

Code (`ratchet/weave.py`, `ratchet/chunk.py`, the derived path in `ratchet/blobstore.py`) is the
source of truth for the formats; this records the *why*. Validated against the real transcript
corpus and hardened by an adversarial review.

## Context

The blobstore freezes a whole transcript `.jsonl` byte-for-byte (ADR-0002). That raw artifact is a
**tree, not a transcript**: each line is one content *block* (an assistant turn is a chain of
block-records sharing `message.id`); rewinds/retries fork sibling branches; a compact severs the
`parentUuid` chain; subagent threads are `isSidechain`; metadata interleaves. The extractor
(`glean`, future) needs the opposite — bounded, linear, provenance-tagged units. Two blocks bridge
the gap, and the result is an **auditable pipeline of materialized, content-addressed artifacts**:

```
tap → raw blob → weave → cleaned blob → chunk → chunkset → glean → events
              (verbatim)            (clean text)        (pointers into cleaned)
```

## Decisions

### weave and chunk are separate blocks — and separate from tap and glean

A clean no-LLM / LLM cut: `tap` (fetch), `weave` (render → cleaned blob), `chunk` (window →
chunkset) are deterministic and LLM-free; `glean` (extract) is the only LLM stage and stays
**source-agnostic** because weave/chunk absorb the format knowledge. Keeping them separate buys
**re-derive one step without re-running the others** (the same reason ADR-0001 §3 split fetch from
generate): improving the renderer (`render_version` bump) re-derives cleaned blobs over existing
raw blobs with no re-fetch; re-chunking at a new budget re-derives a chunkset without writing a new
cleaned blob (the cleaned-blob `put_derived` is an idempotent no-op; the render itself still runs —
turn boundaries come only from a render — see Known limits).
`weave` and `chunk` are the per-`source_kind` renderer/chunker on the OUT edge that ADR-0002
deferred. weave depends on nothing downstream; chunk depends on weave; neither depends on glean.

### A record is a content block; a turn is a chain of same-`message.id` records

Reconstruction follows from this. Parallel tool calls make each `tool_use`'s result hang off *its
own* block-record, so the surviving linear chain passes through only the **last** parallel call —
a naive walk **drops the earlier results** (measured: 47/678 tool calls on one real path). weave
reassembles turns by contiguous `message.id` and folds results by id (below).

### Active path = walk back from the last leaf, three-tier links

The tip is the last file-order `user`/`assistant` record; walk to a root resolving each edge in
order: **(1) `parentUuid`**, **(2) `logicalParentUuid`** (the compact bridge — a compact nulls
`parentUuid`), **(3) file-order fallback** to the highest-ordered earlier `user`/`assistant`
record. Tier 3 exists because a compact's `logicalParentUuid` sometimes points at a record never
persisted to the blob (observed: a 501-record session stranded to 5 without it); the pre-compact
history still sits in the blob in append order, and **append order is ground truth**. Walking
`parentUuid` from any bridged tail still drops abandoned branches, so **file order disambiguates
branches**: the survivor of a rewind is whatever was appended last.

### Sidechains dropped; compacts kept; the whole session is ONE cleaned blob

- **Sidechains** (`isSidechain`) are subagent threads — a *separate* conversation, out of scope.
  Dropped (and zero appear in the corpus; subagents write elsewhere).
- **Compacts** are kept: weave renders the **raw pre-compact turns** (highest-value extraction
  material, frozen in the blob) and ignores the model's lossy compaction *summary*.
- A whole session renders to **one cleaned blob**, across compacts. A compact is *context
  management, not a task change* — so it does not split the cleaned unit; it is a within-doc
  **segment edge** that the chunker treats as a hard chunk boundary. (Corpus: `/clear` — the one
  signal that *is* a task reset — appears 45× across 107 sessions but only 6× mid-session and
  never forks the conversation structurally, so it is not a V0 split point. One raw → N cleaned
  blobs stays available for sources that bundle unrelated content — PR threads, Slack channels —
  where the dividing line is co-reference: a cleaned blob is a maximal set of mutually-referencing
  content.)

### Tool results folded by `tool_use_id`, last-wins, from a whole-file index

A `tool_result` is matched to its `tool_use` by `tool_use_id` over **all** non-sidechain records,
not by tree adjacency — recovering the parallel-call results the linear path drops, and rendering
each call next to its outcome. On the rare reused id (a rewound attempt), the **later (survivor)**
result wins.

### Render drops noise, truncates output, sanitizes surrogates, and is versioned

Speaker-tagged `[user]`/`[assistant]`/`[compact]`. Dropped: system bookkeeping, `isMeta` caveats
(reliably mark injected `<local-command-caveat>` noise), empty/encrypted thinking. Tool output and
large inputs are truncated head+tail so a turn stays bounded. **Lone surrogates are dropped**
(`encode("utf-8","replace")`, 1:1 so offsets hold): Claude Code truncates tool output by UTF-16
unit and can split a surrogate pair, yielding a lone surrogate that survives `json.loads` but is
not UTF-8 encodable — it would otherwise crash the content hash on an unprocessable poison-pill
blob. `RENDER_VERSION` ("weave/1") pins the logic that produced a span.

### chunk: never split a turn, break at compacts, pack to budget — materialized as a chunkset

Turns pack into chunks ≤ a char budget (a token proxy); a turn is never split (an over-budget turn
stands alone); a compact segment edge always starts a fresh chunk (no chunk straddles a context
discontinuity); chunks tile the turns contiguously. The result is **materialized**, not on-demand:
one **chunkset** blob per cleaned blob (`derived_from` = cleaned hash, `produced_by` = "chunk",
`format` = "transcript.chunkset/1"), which is exactly the cluster of related chunks under one
cleaned file.

### Provenance: a chunk is a byte-offset POINTER into the cleaned blob, not a copy

A chunk records `[byte_start, byte_end)` into the cleaned blob's UTF-8 bytes (plus turn range,
segment, kinds). **Pointer, not text copy**, because content-addressing makes the pointer exact:
`cleaned_hash` pins the bytes, so a copy could only duplicate the whole cleaned text or diverge
from it. Resolving reads **only stored bytes** — `get(cleaned_hash)` sliced — never a re-render, so
a downstream quote is **trusted iff it is a substring of `resolve(chunk)`**, and every lineage hop
is content-addressed: **event → byte span in cleaned blob → `derived_from` → raw blob → datastore.**
This *refines ADR-0001 §5*: the extractor reads `render(blob)`, not raw JSON (a multi-line/escaped
quote is not a literal substring of the JSON), and weave's determinism makes the cleaned blob as
verifiable as the raw blob — re-rendering reproduces the identical hash. Byte (not char) offsets
because the blob *is* bytes — tool-agnostic, and it sidesteps the code-unit ambiguity behind the
surrogate bug. Trust rests on weave/chunk being **deterministic and LLM-free**.

### Derived blobs: two orthogonal axes (mutability vs. retention), self-describing, reachability-kept

Every pipeline artifact lives in the blobstore with traceable history, without weakening the store:

- **Mutability — never, for raw *and* derived.** Bytes under a hash never change in place; that is
  what keeps content-addressing and provenance spans sound. Derived blobs are also content-addressed.
- **Retention — by reachability.** A raw blob is irreplaceable ground truth (kept forever). A
  cleaned blob / chunkset is rebuildable, hence TTL-eligible (`expires_at`) — but **kept as long as
  a downstream artifact points into it** (a dangling pointer would force a regenerate-on-read,
  which the design forbids). TTL reaps only *unreferenced* derived blobs; re-deriving reproduces
  the same hash. Deletion ≠ mutation.

`blobstore.put_derived` writes a self-describing sidecar (`kind`, `source_kind`, `format`,
`render_version`, `produced_by`, `derived_from`, `tags`, `expires_at`) so a consumer can skip/use
without reading the content. Derived blobs carry no `source_id`, staying out of the raw TimeMap.
`derived_for(hash)` walks lineage by one sidecar scan. `_commit` refuses to re-commit a hash under
a different `kind` (raw vs derived share one namespace; identical bytes across kinds fail loud
rather than silently dropping lineage).

## Known limits (deferred, accepted)

- **TTL/reachability GC unimplemented.** `expires_at` is recorded; no reaper reclaims unreferenced
  derived blobs yet, and reachability is a retention *rule*, not yet an enforced sweep.
- **Re-render on chunk production.** The `chunk` step renders once to find turn boundaries (reads
  are pure pointer resolution). The hot path renders once and passes the doc to both materializers;
  caching boundaries in the cleaned sidecar would remove even that, deferred.
- **Chunk budget is char-based** (token proxy), no semantic boundaries. Safe to revise: re-chunking
  re-derives the chunkset and moves no cleaned-blob span; only a *render* change bumps `render_version`.
- **Slash-command XML wrappers render raw** (minor noise). Tighter filtering is a future `render_version`.
- **Truncation is fixed head+tail.** A quote inside an elided middle cannot be cited — acceptable:
  the extractor only ever sees, and quotes, the rendered text.
- **Single-input derivations / reverse lineage is best-effort.** `derived_from` is one hash;
  merging across blob versions is unmodeled. And because a cleaned blob is content-addressed,
  two *distinct* raw blobs that render to *identical* cleaned text (e.g. two all-noise sessions
  → `""`) dedup to one cleaned blob whose `derived_from` records only the **first** producer — so
  `derived_for(second_raw)` is empty. `weave.materialize` still returns the right hash; only the
  reverse sidecar scan is partial. Fixing it would need a mutable/multi-valued back-link, which
  trades away immutability — not worth it for a collision this rare.
- **Subagent transcripts** (`isSidechain`) are dropped — a future `source_kind`, not a weave knob.
