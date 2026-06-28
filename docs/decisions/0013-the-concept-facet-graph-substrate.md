# 0013 — the concept-graph substrate: structure from PROVENANCE FACETS, no embeddings

- Status: accepted — implemented 2026-06-28 (all 12 suites green, incl. the golden-file `test_concepts`)
- Date: 2026-06-28
- Supersedes: — (nothing). The additive read-side substrate the gardener (3b/3c) builds on.
- Superseded by: —

Code (`ratchet/concepts.py`) is the source of truth; this records the *why*. weave and the blobstore
are untouched by this ADR — that is the point of the design below.

## Context

The concept layer is a FLAT bag. A concept is a versioned blob `{id, title, statement, evidence,
source_takeaway}` minted by `review.accept`; "valid concepts" is the derived `dream.load_concepts` view
(latest version per id, minus retired). Nothing relates one concept to another — no clusters, no edges,
no "these three are about the same file." The gardener that is coming (3b/3c — managed tags, split/merge,
asserted edges) needs a structural view to operate over, and `/ratchet-review` wants "here are the
related concepts" when triaging. Both need inter-concept structure that does not exist yet.

The obvious move — embed each concept's text and cluster by cosine — is the move that **already failed
once**. dream v1 clustered events by lexical TF-IDF over short verbatim quotes; it under-merged so badly
that 106 events became 81 clusters (~one takeaway per event) for ~$3 of wasted Sonnet (ADR-0010
§Context). Short, jargon-dense developer text is too sparse for a similarity metric to group reliably,
and ratchet has no embedding model anyway (only the authed `claude` CLI). dream v2's answer was to stop
relying on a similarity score at all — corroboration-over-time became the filter, and the LLM (not a
vector index) became the relatedness oracle. This ADR applies the *same lesson* to concepts: **derive
structure from metadata ratchet already has, not from a similarity score over the text.**

The metadata is sitting in the provenance. Every concept cites `evidence` → a `cleaned_hash`; every
cleaned blob hops one `derived_from` edge to its raw transcript, whose `origin_ref` already carries
project/session/session-time. The one thing not recorded anywhere is the *session-level facts* — which
files were edited, which tools ran — but those are not lost: the raw transcript (ground truth, kept
forever) RE-PARSES to them deterministically. Re-derive them on read, and two concepts that touched the
same file are RELATED with zero text comparison.

## Decisions

### 1. Recompute facets on READ from the raw — store NOTHING

A concept's facets are a pure function of immutable blobs, so `concepts` recomputes them on every read
and persists nothing. `session_facts(spine)` → `{files_edited, tools}` distilled from a session's ACTIVE
PATH: `files_edited` is the written PATH of every Edit/Write/MultiEdit/NotebookEdit (a Read views, it
does not write — and a NotebookEdit names its target `notebook_path`, not `file_path`); `tools` is every
invoked tool name; both as SORTED lists (stable bytes). `_cleaned_facets(cleaned_hash)` hops the one
`derived_from` edge to the raw (kept forever), reads repo/session/session-time straight off its
`origin_ref`, and re-parses its body (`parse → active_path → session_facts`) for files/tools — returning
`{repo, session, files, tools, time}`.

Nothing touches a sidecar, so the cleaned blob's content hash and every span anchored into it stay valid
**by construction** (we never write to it). The decisive payoff is the migration that does not exist: an
OLD cleaned blob — minted before this stage — yields full facets on its very first read, because the
facts were never stored and are always re-derived. The backfill problem *vanishes* instead of being
solved. `session_facts` + `EDIT_TOOLS` live in `concepts` (concept-substrate concerns), importing only
weave's public `parse`/`active_path`; weave stays PURE render and the blobstore keeps its write-once
sidecar with zero post-commit-amendment surface.

### 2. Rejected: capture `session_meta` at weave + stamp/backfill

The alternative — built first, then reverted — was to CAPTURE `session_meta` onto the cleaned blob's META
sidecar at weave time (a new `put_derived` param), amend already-committed blobs in place via a
`blobstore.stamp_session_meta`, and run a one-shot `weave.backfill_session_meta` for everything minted
before the capture. Rejected for two reasons, both about where state lives:

- **It punctures the blobstore's write-once invariant.** The meta sidecar IS the commit marker (ADR-0007):
  written last, atomically, never touched again — that property is what makes "the sidecar is the source
  of truth" sound. `stamp_session_meta` is a post-hoc amendment to a committed marker; however carefully
  scoped (it touched no content field), it opens a second, sanctioned way to mutate a sidecar, and every
  future reader inherits the burden of reasoning about a marker that can change after commit.
- **It stores a rebuildable view in a module whose premise is the opposite.** `concepts` — like
  `current_takeaways` / `load_concepts` — exists to DERIVE a view on read and never persist it. Stamping
  facets onto a sidecar persists exactly the derived state this module is built to NOT store, and then
  *requires* a migration (the backfill) to repair the blobs that predate the store — a migration that
  exists only because the facts were stored in the first place.

Recompute-on-read dissolves both: no amendment surface, no stored view, no migration.

### Accepted cost + the clean upgrade if it goes hot

Recompute pays one raw re-parse per `_cleaned_facets` call (a cleaned blob cited by many concepts is
cached per `concept_graph` call, so it re-parses once per call, not once per edge). This is fine because
`concepts` is a COLD path — the CLI spot-check and the rare sleep-time gardener read, never the hot
ingest loop. If it ever goes hot, the right fix is **not** a sidecar amendment but a *derived facets
blob*: content-addressed, `derived_from` the cleaned blob, TTL-eligible, blobstore-PURE — a first-class
immutable artifact with lineage, not a mutation of a commit marker. The cold path keeps that upgrade open
without taking on the puncture today.

### 3. Facets — UNION the cited sessions' provenance per concept

`concept_facets(concept)` walks the concept's `evidence`, recomputes each `cleaned_hash`'s facets (§1),
and UNIONs into `{repos, files, tools, sessions, time_range}`. The union (not a single session) is forced
by the model: a concept accretes evidence across sessions as it strengthens/refines (ADR-0010), so a
mature concept legitimately spans several. `time_range` is `[earliest, latest]` over the cited sessions'
raw mtimes — a min/max over ISO strings that reads chronological only because every mtime is tz-aware UTC
ISO (tap's invariant). A gone/empty raw contributes nothing (never fatal). The result is sorted lists —
stable, JSON-ready, the node payload of the graph.

### 4. Derived edges + leader clusters — a REBUILDABLE VIEW, weighted SET OVERLAP not similarity

Two rebuildable views, computed on read, **never stored** — exactly like `current_takeaways` /
`load_concepts`:

- **Edges**: for each concept PAIR, every non-empty facet overlap is an edge —
  `shares-file` / `shares-repo` / `shares-tool` (the shared members) and `temporal-proximity` (time
  ranges within `TEMPORAL_WINDOW_SECONDS`). Disjoint concepts → no edge. Order-stable (concepts sorted
  by id → source < target).
- **Clusters**: leader (sequential) clustering over a facet-overlap SCORE — `W_FILE·|shared files| +
  W_REPO·|shared repos| + W_TOOL·|shared tools| + W_TEMPORAL_BONUS`. A shared FILE is the strongest
  co-location signal (same file ⇒ almost certainly the same code), a repo weaker (holds many unrelated
  lessons), a tool weaker still (everyone runs Bash); temporal proximity is a tie-break bonus, never
  structure alone. The weights are named constants and **untuned** — starting values pending a gold set;
  `CLUSTER_THRESHOLD == W_FILE` is the explicit coupling that makes "one shared file clears the bar."
  Leader clustering was chosen for the same property ADR-0010 §8 chose it for: a single, order-stable,
  deterministic pass — the most distinctive concept (sorted-id order) seeds a cluster, each later one
  joins the first leader within threshold.

This is **a weighted set overlap, NOT a similarity score** — no tf-idf, no cosine, no embeddings. The
distinction is the whole point: an overlap is grounded in a fact (these concepts wrote the same path); a
similarity is a guess over sparse text, and that guess is what sank dream v1.

`concept_graph` → `{nodes, edges, clusters}`; `concept_clusters` is the cluster view alone; a CLI
(`python -m ratchet.concepts`, mirroring the other stages' read-only inspectors) dumps either for
spot-checking.

### 5. PURELY additive read-side — the gardener is deferred

Nothing here mutates a blob or what a concept asserts. No LLM, no managed tags, no asserted edges, no
split/merge/supersede. Those are the gardener's (3b/3c): managed tags will become a SECOND facet source
unioned alongside the provenance facets (the substrate is built to take them), and the structural ops
will *act on* this view. This ADR ships only the substrate they stand on.

## Consequences

- **Good:** structure with zero LLM cost and zero similarity guessing — every edge is explainable by a
  shared concrete fact; the views are rebuildable (a concept re-resolves its facets from immutable
  blobs, like the rest of the trust chain), so there is no graph store to desync. Recompute-on-read
  needs NO weave/blobstore change at all — old cleaned blobs get facets for free, so there is no
  migration and nothing to backfill, proven by the other 11 suites staying green untouched. The golden
  (`test_concepts`) pins facets + edges + clusters over deliberately-overlapping fabricated sessions with
  a legible diff, and pins that no cleaned/chunkset sidecar stores `session_meta` (a regression that
  re-adds stored facets fails).
- **Costs / known limits:** facet overlap is a COUNT of shared members, not a calibrated relatedness — a
  file edited in fifty unrelated sessions (a `flake.nix`) over-connects, the obvious refinement being an
  inverse-session-frequency down-weight (the IDF idea WITHOUT the tf-idf-over-text trap). Each
  `_cleaned_facets` call re-parses the raw (one parse per cited blob per `concept_graph` call, via the
  per-call cache); acceptable on this cold CLI/gardener path, and a derived facets blob is the clean
  upgrade if it ever goes hot. A cleaned blob whose raw was TTL-reclaimed would lose its facets (the
  recompute returns None) — moot today: raw is ground truth, never GC'd. The temporal window and the
  weights are magic numbers the prior art warns don't transfer (ADR-0010's open question); they are
  named constants precisely so a gold set can tune them later. Edges are derived every call
  (O(concepts²)); fine at the current scale, an index is a later deletable cache (ADR-0002).

## References

ADR-0010 (dream v2 — the TF-IDF-over-short-text failure this refuses to repeat; §8's order-stable Leader
clustering, reused here; the "derive a view, don't store it" discipline). ADR-0012 (the weaken path —
the same additive-fact, single-source, rebuildable-view style). ADR-0007 (every artifact is a blob; the
meta sidecar as the write-once commit marker — the invariant recompute-on-read refuses to puncture).
ADR-0003 (weave/chunk — the cleaned blob + span model, kept PURE render here). The golden-file acceptance
test `tests/test_concepts.py` + `tests/golden/concept_graph.json`.
