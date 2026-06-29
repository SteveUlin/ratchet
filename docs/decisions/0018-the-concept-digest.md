# 0018 — the concept digest: a bounded, structured "what we already know" read-view

- Status: accepted — implemented 2026-06-28 (all 17 suites green, incl. the 3a/3b/3c goldens, untouched)
- Date: 2026-06-28
- Supersedes: dream's flat `_render_concepts` (the bare `- id X: title — statement` list ADR-0010 injected).
- Superseded by: —

Code (`ratchet/concepts.py` — `concept_digest`/`_digest_entrench`/`_digest_relations`; `ratchet/dream.py` —
`DreamBlock.items` builds it, the synth fns read it) is the source of truth; this records the *why*. This is
**4a**: the structured concept read-view, and dream's switch to reading it.

## Context

Section 3 gave the concept layer STRUCTURE — facet clusters + derived edges (3a/ADR-0013), managed tags
(3b/ADR-0014), the gardener's asserted `generalizes`/`supersedes`/`relates-to` edges and the hierarchy spine
(3c/ADR-0015). But the upstream LLM stages never SEE it. dream renders the valid concepts into its synth
prompt as a FLAT list (`_render_concepts`: `- id X: title — statement`), the same bag the structure was built
to replace. So synth judges belief-change (`new`/`strengthens`/`refines`/`contradicts`) against a pile with
no grouping, no tags, no relations — it cannot tell that two "different" concepts are one cluster, or that a
candidate already has a parent. The read-back the model needs is "here is what we already know AND how it is
organized" — the **Bayesian-surprise** read against the store — and the flat list throws the organization away.

The fix is a DIGEST: a single, bounded, structured text rendering of the concept graph, injected where the flat
list was. The constraint is the same one that shapes dream's catalog and garden's vocabulary — ratchet has no
embeddings (only the authed `claude` CLI), so the whole thing goes IN-PROMPT, which means it must stay small.

## Decisions

### 1. A rebuildable read-view, not a stored artifact

`concept_digest(root, *, budget)` is a pure function of the blobs, computed on read and never stored — the
ethos of all of `concepts.py` (`concept_graph`/`concept_clusters`), `dream.catalog`, `garden.vocabulary`. It is
built from `concept_graph` in ONE facet pass (the expensive raw re-parse happens once), plus a cheap
`load_concepts` for the statements `concept_graph`'s nodes omit. No migration, no sidecar, reproducible from the
hashes like every other ratchet view.

### 2. Group by CLUSTER; show the hierarchy as per-concept relations

The primary grouping is the facet CLUSTER (`concept_graph`'s `clusters`), because leader clustering is a
COMPLETE PARTITION — every valid concept lands in exactly one cluster — so grouping by it covers everything with
no overlap and no orphans. The `generalizes` hierarchy can't be the partition: it is SPARSE (empty until an
`abstract`/`reparent` runs) and covers only some concepts. So the hierarchy rides as ANNOTATION — each concept
line shows its OUTGOING asserted relations (`generalizes → c-x (note)`), which surfaces the spine right on the
parent's line without a separate tree render. Only the gardener's DELIBERATE asserted edges are shown; the
derived facet-overlap edges are not — they ARE the clustering, already the grouping axis, so re-listing them is
noise. (This grouping choice — cluster-primary, hierarchy-as-annotation — is the call most worth a second look:
once the hierarchy is dense, a nested render under generalization may read better than flat clusters.)

### 3. Bounded by an entrenchment budget, with a `…(+N more)` honesty marker

The in-prompt invariant (decision 1's no-embeddings consequence) makes "small" load-bearing, not a nicety. So
`budget` is a CONCEPT cap: past it, rank every concept by ENTRENCHMENT and keep the top, drop the tail. The rank
is distinct cited SESSIONS first (the same corroboration signal dream's maturity gate trusts — a belief seen
across more sessions is more durable), then evidence-pointer count, then id for stability. The dropped tail is
NOT silent: a `…(+N more, dropped as least-corroborated)` marker tells the model the view is PARTIAL — those
concepts EXIST, they are just not shown — so it never mistakes a dropped lesson for a genuinely new one. The
empty set yields a clear sentinel (`(no concepts yet — treat everything as new)`) so a stage is never asked to
relate against nothing. `budget <= 0` disables the cap.

### 4. dream reads the digest — a behaviour-preserving renderer swap

`_render_concepts` is deleted; `DreamBlock.items` builds the digest ONCE per run (the load-once pattern its
`_concepts`/`_cat` already follow) and threads it as a pre-rendered STRING down `apply` → `synthesize_new`/
`update_support` → `_synth_user`. No routing/apply LOGIC changes — synth's prompt simply now carries structure.
The dream↔concepts import cycle (concepts imports `dream.load_concepts` at module load) is broken the way 3a/3b
already break the concepts↔garden pair: a FUNCTION-LOCAL `from .concepts import concept_digest` inside `items`.
Because the fakes in dream's offline tests don't read the prompt text, the swap is byte-invisible to them — they
stay green unmodified.

### 5. In-prompt now; a queryable index deferred

The whole digest goes in the prompt — no retrieval, no top-K, no vector store — the same bet dream's catalog and
garden's vocabulary make, and bounded by the same forces: the maturity gate (3 of ADR-0010) and the gardener's
consolidation (merge/abstract/retire, ADR-0015) keep the live concept set small, and decision 3's budget is the
backstop. This is a deliberate tradeoff, not an oversight: a queryable concept index (embed + retrieve the
relevant subset per observation) is the move ONCE the concept set outgrows a prompt — and it is deferred until
then, because it is premature complexity while the set fits and it re-introduces the similarity-metric the whole
facet substrate exists to avoid (ADR-0013 §Context). The budget's truncation is the bridge: it degrades
gracefully (most-entrenched-first) long before a hard index is needed.

## Consequences

- **Good:** the upstream LLM stages now read the concept layer's STRUCTURE, not a flat bag — synth judges
  belief-change against grouped, tagged, related concepts. Purely additive at the read-view layer: the
  3a/3b/3c goldens are byte-identical (no graph/cluster/edge/tag view changed — the digest is a new READER of
  them), proven by the prior suites staying green. The new acceptance test (`test_concept_digest`) pins the
  contract: the structure renders (cluster grouping, tags, asserted relations), a small budget truncates to the
  most-entrenched + emits the marker, and the empty store yields the sentinel.
- **This is the substrate 4b reads.** glean's novelty-awareness (the new relevance marker — the NEXT slice)
  reads the SAME digest to gauge whether an observation is already covered. 4a builds the read-view and wires
  dream; 4b adds the second consumer. glean is untouched here.
- **Costs / known limits:** the digest costs one facet pass per dream run (the `concept_graph` re-parse), paid
  once in `items` — negligible beside dream's LLM spend. A dropped concept's id may still appear as a RELATION
  target on a kept concept's line (the relation is real; the target is just not itself rendered under a tight
  budget) — accepted as honest partiality, not hidden. The cluster-primary grouping (decision 2) is the open
  design question once the hierarchy densifies.

## References

ADR-0013 (the concept-facet graph — `concept_graph`/`concept_clusters` the digest renders; the
derive-structure-from-provenance-not-similarity lesson decision 5's deferral honors). ADR-0014 (the managed tags
the digest shows; the same in-prompt, kept-small-by-design invariant — `render_vocabulary` — the digest
inherits). ADR-0015 (the asserted edges + hierarchy spine the digest surfaces as per-concept relations).
ADR-0010 (dream v2 — the flat `_render_concepts` this replaces, the `DreamBlock` load-once pattern, the maturity
gate that bounds the concept set, the in-prompt-no-embeddings bet decision 5 extends). The acceptance test
`tests/test_concept_digest.py`.
