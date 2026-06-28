# 0014 — the gardener, phase 1: managed tags as a cheap-AI grouping signal

- Status: accepted — implemented 2026-06-28 (all 13 suites green, incl. the 3a golden `test_concepts`)
- Date: 2026-06-28
- Supersedes: — (the first slice of the gardener ADR-0013 deferred to 3b/3c)
- Superseded by: —

Code (`ratchet/garden.py`) is the source of truth; this records the *why*. The threading into the graph
is `ratchet/concepts.py` (a `shares-tag` edge + a `W_TAG` weight); nothing else is touched.

## Context

3a (ADR-0013) gave the concept layer a structural view derived from PROVENANCE facets: two concepts that
wrote the same file / repo / tool, or landed close in time, are related — with zero text similarity. But
provenance has a blind spot by construction. Two concepts can be about the *same theme* — version control,
the Nix shell, test discipline — while touching entirely different files, so they share no facet and the
3a graph leaves them disconnected. The whole point of a TAG is to relate things that don't co-locate.

The obvious fix — embed each concept and cluster by cosine — is the move ADR-0013 already refused, for the
reason ADR-0010 paid for in real dollars: short, jargon-dense developer text is too sparse for a
similarity score, and ratchet has no embedding model anyway (only the authed `claude` CLI). So tags are
not *discovered* by a metric; they are *assigned* by the LLM itself from a small CONTROLLED VOCABULARY —
the same "LLM as the relatedness oracle over an in-prompt catalog, not a vector index" shape as dream's
router. A controlled vocabulary (not free-text labels) is the load-bearing constraint: reuse is what makes
a tag GROUP things; an open label space fragments into near-duplicates and groups nothing.

This is the gardener's first phase. It is deliberately LOW-STAKES: tagging only PRODUCES a grouping signal,
it never rewrites what a concept asserts, so — unlike the concept-promotion gate (ADR-0008) — it is
AUTO-applied with no human review. The structural ops that ACT on the graph (merge / split / abstract /
retire concepts, and the vocab's own curation — tag merge / retire) are higher-stakes and stay in 3c.

## Decisions

### 1. The vocabulary is a DERIVED FOLD over append-only `tag` blobs — never a stored set

A tag is a blob (ADR-0007): `source_kind="tag"`, `source_id` = the slug, content `{slug, gloss}`.
`vocabulary(root)` folds `latest_by_kind('tag')` to `{slug: gloss}` — the current controlled set, computed
on read exactly like `catalog` / `load_concepts`, never persisted as a mutable set. It starts EMPTY and
grows only as the tagger proposes new tags. Re-proposing an identical slug+gloss is a byte-identical
ingest no-op (the content is run-invariant; producer/cost ride in `origin_ref`), so the vocab never churns.
Tag merge/retire — folding the vocab DOWN — is the symmetric gardening act, and like every other curation
op it is deferred to 3c; here the fold only ever grows, which is why the vocab is "managed" (it will need
pruning) rather than fixed.

The growth is BOUNDED by `VOCAB_MAX`: once the frozen vocab reaches it, every new-tag proposal is dropped
(reuse-only from then on). This is an INVARIANT, not a tuning knob — the no-embeddings design renders the
WHOLE vocabulary into every prompt, so "small and in-prompt" is load-bearing. The cap is sized generously
(never to fire on the happy path), and it bounds two worst cases at once: the COARSE-fingerprint
amplification (a vocab change re-tags every concept, below) and the auto-grow bloat, until 3c's merge/retire
curates the set down for real.

### 2. Assignments are append-only per-concept blobs, latest-wins — the concept stays IMMUTABLE

A concept is re-taggable as the vocabulary grows, and that re-tagging must be INDEPENDENT of what the
concept asserts — so it cannot re-version the concept blob (that would couple a cheap, frequently-revised
signal to the system's most-trusted, human-reviewed artifact, and bloat its version history). Instead a
tag assignment is its OWN blob: `source_kind="tag_assignment"`, `source_id = "ct-"+concept_id` (a DISTINCT
source_id namespace, so the kind-agnostic `latest_version` fold never entangles a concept with its tags),
content `{concept_id, tags, vocab_fingerprint}`. `concept_tags(concept_id)` is the latest-wins fold of
that source's versions. The content is run-invariant (producer/cost in `origin_ref`), so a crash-retry
re-ingests byte-identically — the same no-churn discipline as dream's `takeaway_content`. The
`vocab_fingerprint` is recorded IN the assignment so a read can tell which vocabulary a tagging judged
against.

### 3. A `GardenBlock` reusing the whole driver; idempotency via a VOCAB FINGERPRINT param

Tagging is a cheap-LLM-per-concept pass, so it is a `block.Block` (ADR-0009) and inherits the entire
driver — enumeration, per-item commit, error isolation, `--limit` / `--max-usd`, resume, and the modular
`priority` (ADR-0011). `items()` = the valid concepts (`dream.load_concepts`); `process()` is ONE cheap
tagger call over `{title + statement + the 3a provenance facets as CONTEXT + the FROZEN vocabulary}` →
assign tags from the vocab + optionally propose new ones; it commits the assignment and auto-adds any
proposed tag. `commits_per_item=True` — assignments are independent.

Idempotency is the driver's done-skip with the right key. Like glean's `prompt_version`, the block puts a
VOCAB FINGERPRINT (a short hash of the frozen `{slug: gloss}` set) in `params`, so the done-key is
`(concept_id, PROMPT_VERSION, vocab_fingerprint)`. A concept whose latest tagging ran against the CURRENT
vocab is done-skipped; ANY vocab change flips the fingerprint and re-tags every concept — correct, because
a grown vocabulary may tag an old concept differently. The vocabulary is read ONCE, FROZEN at run start:
tags proposed this run are committed immediately (so NEXT run sees them) but do not change THIS run's
fingerprint, which keeps the per-item done-key and the assignments consistent regardless of processing
order — the same "freeze the catalog at run start, fold from store next run" discipline as dream's
on-instance catalog. `priority()` is untagged-first, then facet-rich-first (an untagged concept has no
grouping at all; a facet-rich one gives the tagger more context to judge from).

One operational wrinkle follows from freezing the vocab per run: SEED the vocab with ONE uncapped pass. A
`--limit`/budgeted run over an empty-ish vocab tags its slice, then PROPOSES tags that grow the vocab —
flipping the fingerprint and invalidating the very concepts it just completed, which re-tag next run. It
self-heals (the vocab converges as proposals taper, then re-runs done-skip) and is harmless (cheap model,
cold path), but a first full pass avoids the churn.

Untrusted output is coerced defensively, mirroring `_clean_route`: a proposed slug is `slugify`d and kept
only if novel; an ASSIGNED tag is kept only if it is in the frozen vocab UNION this call's proposals (a
tag the model invented out of nothing is dropped — never act on a hallucinated slug). New-tags-per-call
and tags-per-concept are capped so a controlled vocabulary stays small.

### 4. Tags thread into the 3a graph as a SECOND facet, sharpening — not replacing — provenance

`concept_facets` gains a `tags` axis (threaded in: the graph folds all assignments once in `_facet_index`
and passes the per-concept list — not recomputed per call). `facet_overlap` adds `shares-tag`, `facet_score`
adds `W_TAG · |shared tags|`, and `shares-tag` joins `EDGE_KINDS` so `derived_edges` emits it for free.

`W_TAG < CLUSTER_THRESHOLD` (2.0 < 3.0), and this inequality is load-bearing — naming the tension honestly:
a managed tag here is UNCURATED and AUTO-applied (no merge/retire until 3c), so a single tag is a SOFT
signal, not yet a trustworthy assertion that two concepts belong together. So one shared tag fires the
`shares-tag` EDGE (the thematic relation stays visible and explainable) but does NOT force a cluster alone;
clustering requires CORROBORATION — a SECOND shared tag (2·W_TAG = 4.0 ≥ bar), or a shared tag plus a shared
file (5.0) or repo (3.0). This protects 3c's per-cluster LLM passes from garbage, over-broad clusters (a
`general` tag smeared across 50 concepts dragging them into one blob) BEFORE the vocab is curated. The
staging is explicit: once 3c's tag merge/retire makes the vocabulary trustworthy, `W_TAG` is raised
toward/above `CLUSTER_THRESHOLD` and a single curated tag can clear the bar on its own.

The backward-compat invariant is exact: a `tags` key is added to a concept's facets ONLY when non-empty, so
an untagged concept's facet bytes — and therefore the whole 3a graph and its committed golden — are
byte-IDENTICAL to before tags exist. `shares-tag` simply never fires until a tag is assigned.

(`concepts` imports `concept_facets` from itself and the tag-fold readers from `garden`, while `garden`
imports `concept_facets`; the cycle is broken with a function-local `from . import garden` inside
`_facet_index` — the tag-fold readers carry no Block, so a runtime import is clean.)

## Consequences

- **Good:** the missing semantic grouping axis, at cheap-model cost on a cold path, with no embeddings and
  no similarity guess — every `shares-tag` edge is explainable by a curated tag a model deliberately
  assigned. It is purely additive over 3a (the golden proves the untagged graph is unchanged) and reuses
  the Block driver wholesale (budget/limit/resume/priority for free). Everything is a rebuildable fold:
  the vocab and a concept's tags re-derive from immutable blobs, the concept blob is never touched, and a
  crash-retry no-ops — so there is no tag store to desync and no migration. `test_garden` pins the store
  folds, the graph sharpening (a shared tag forces a cluster two facet-disjoint concepts lack), idempotent
  re-runs (zero tagger calls), and re-tag-on-vocab-change.
- **Costs / known limits:** the fingerprint is COARSE — any vocab growth re-tags EVERY concept next run,
  O(concepts) cheap calls per vocab change. Acceptable because the vocab converges (proposals taper), each
  run is bounded by `--limit`/`--max-usd`, the model is cheap on a cold path, and `VOCAB_MAX` caps the
  amplification's worst case outright. A per-concept "relevant-vocabulary" fingerprint — keying a concept's
  done-skip on only the tags relevant to IT, so unrelated vocab growth no longer re-tags it — is the obvious
  refinement, and is deliberately NOT built: deciding which tags are "relevant" to a concept is itself the
  judgement the gardener makes, so the narrowing is unsound without a gold set to validate it, and getting
  it wrong silently freezes a concept against a stale vocab. The coarse-but-correct fingerprint stands;
  `VOCAB_MAX` bounds its cost instead. Tagging is AUTO with no review, so a mistagged concept mis-groups
  until re-tagged or the vocab is curated — the intended trade for a low-stakes signal, with 3c's tag
  merge/retire (and human spot-check via `--vocab` / the graph CLI) as the backstop. The auto-grow
  vocabulary will accumulate near-duplicate slugs (the open-label failure the controlled vocab is meant to
  avoid, only deferred) until 3c folds them down — now BOUNDED by `VOCAB_MAX`. `W_TAG` is set BELOW
  `CLUSTER_THRESHOLD` on purpose (corroboration-gated, raised once 3c curates); it and the caps are untuned,
  named constants pending a gold set — the same caveat ADR-0013 carries for the facet weights.

## References

ADR-0013 (the 3a facet substrate this sharpens — the recompute-on-read, weighted-set-overlap,
rebuildable-view discipline tags slot into; `W_TAG` sits BELOW `CLUSTER_THRESHOLD`, corroboration-gated —
unlike `W_FILE`, which clears the bar alone). ADR-0010 (dream v2 — the LLM as the
relatedness oracle over an in-prompt catalog instead of an embedding index, the TF-IDF-over-short-text
failure this refuses to repeat, and the freeze-at-run-start / fold-from-store catalog discipline).
ADR-0009 (the uniform Block driver — per-item commit, done-skip, budget/limit, error isolation — reused
whole). ADR-0011 (the modular `priority` signal — untagged/facet-rich first). ADR-0007 (every artifact is
a blob; state is a derived fold over append-only blobs, never a stored mutable field — the vocab and
assignments both). ADR-0008 (the human concept gate this is deliberately NOT — tagging is low-stakes,
auto-applied). The acceptance test `tests/test_garden.py`.
