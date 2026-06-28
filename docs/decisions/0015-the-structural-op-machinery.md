# 0015 — the structural-op machinery: asserted edges + append-only concept ops, no LLM

- Status: accepted — implemented 2026-06-28 (all 14 suites green, incl. the 3a/3b goldens, untouched)
- Date: 2026-06-28
- Supersedes: — completes the gardener's structural ops deferred by ADR-0013 §5 / ADR-0014 §Context, and
  the vocab-DOWN curation deferred by ADR-0014 §1.
- Superseded by: —

Code (`ratchet/garden.py` — the ops + the asserted-edge/tag-curation folds; `ratchet/concepts.py` — the
graph folds them in; a one-line `dream.load_concepts` extension) is the source of truth; this records the
*why*. This is **3c-i**: the deterministic, trust-critical foundation. The LLM that DRIVES these ops (3c-ii)
and the human gate that ACCEPTS the high-stakes ones (3d) are deliberately NOT here.

## Context

3a (ADR-0013) gave concepts a structural VIEW — facet-overlap edges + clusters, recomputed on read. 3b
(ADR-0014) added a semantic grouping signal — managed tags. Both only *describe*; neither *changes* the
concept layer. The gardener's job is to act: two concepts say the same thing (merge them), one concept
conflates two lessons (split it), several share a parent idea (abstract it), the hierarchy is wrong
(reparent), a concept is stale (retire) — and the auto-grown tag vocabulary has accumulated near-duplicates
that need folding down (merge/retire tags). These are *mutations* of the system's most-trusted artifact, so
they are the highest-stakes operations in ratchet and the place a bug corrupts curated knowledge
irreversibly.

The whole pipeline already answers "how do you mutate an immutable store safely": you don't mutate, you
append a fact and re-derive (ADR-0007). review's `retire` drops a concept from the valid set with a
*decision*, not a deletion; dream's `merge`/`weaken` demote a takeaway the same way (ADR-0012). The field
converges on exactly this for contradiction handling — Zep's invalidate-don't-delete, AGM's minimal change,
SSGM's quarantine, mem0's hard-DELETE as the documented anti-pattern (ADR-0012 §Context). 3c-i applies that
discipline to *structural* edits: **every op ingests new blobs/decisions only; nothing is ever deleted; the
trust chain re-proves on every write.** Deterministic first, because the trust-critical machinery must be
correct independent of any model, and a green golden is the contract the 3c-ii LLM Block writes against.

## Decisions

### 1. Asserted edges — append-only artifacts, distinct from 3a's DERIVED edges (the ADR-0013 §B realised)

3a's edges are *derived*: recomputed from provenance facets on every read, never stored (ADR-0013 §4) —
the right shape for a relation that IS a function of the data (two concepts wrote the same file). But a
gardener's claim that two concepts relate — *this generalizes that*, *this supersedes that* — is **not** a
function of the data; it is a deliberate assertion, so it must be *stored*. ADR-0013 §B flagged exactly this
split ("the structural ops will *act on* this view"); 3c-i realises it.

An asserted edge is a blob (ADR-0007): `source_kind="concept_edge"`, `source_id` = the edge identity
`src|kind|dst`, content `{src, kind, dst, note, active}`, latest-wins. `asserted_edges(root)` folds
`latest_by_kind('concept_edge')` to the live (active) set — computed on read like `vocabulary`/`catalog`,
never a stored set. **Retract = a new version with `active:false`** (invalidate-don't-delete; the edge blob
+ history stay, the fold simply stops surfacing it). The content is run-invariant, so re-asserting an
identical edge is a byte-identical ingest no-op — the live set never churns. THREE kinds, ONE canonical
direction each: `generalizes` (the hierarchy spine — its inverse `specializes` is NOT stored, read it by
reversing a `generalizes` edge so the two directions can never disagree), `supersedes` (lineage),
`relates-to` (association). `assert_edge` REJECTS (raise) any other kind: in this append-only,
trust-critical store an unknown edge kind is a producer bug (a future 3c-ii typo), not data to fold in
silently and have to invalidate later.

`concepts.concept_graph` folds the active asserted edges into `edges` ALONGSIDE the derived ones (marked
`asserted: True`), but only between two VALID concepts — so a `supersedes` loser→winner edge naturally drops
out of the live graph once the loser is invalidated, while the lineage stays readable in the asserted-edge
store. The fold is EMPTY until an op runs, so an un-edited graph — and the 3a/3b goldens — are byte-identical.
The generalization spine is a separate view, `concept_hierarchy(root)` = `{parent: children}` over active
`generalizes` edges — kept OUT of the `concept_graph` dict precisely so the golden's bytes don't move.

### 2. The ops — append-only, invalidate-don't-delete, trust-chain preserving

A concept leaves the valid set the same way review's `retire` does: its latest decision drops it from the
`dream.load_concepts` fold. `load_concepts` now honors `("retire", "supersede", "split")` — the one-line
mirror of `dream.catalog`'s `("merge", "retire", "reject")` for takeaways. The verbs are DISTINCT on purpose:
the audit answers *why* a concept left the set (human-retired vs. merged-into vs. dissolved-into-parts), not
just *that* it did. The blob + history are always retained. These invalidating verbs are a **single-sourced
contract**: `dream.VERB_RETIRE`/`VERB_SUPERSEDE`/`VERB_SPLIT` define them once and `CONCEPT_INVALID_VERBS` is
built from them; `merge`/`split` reference the same constants when they WRITE the decision (garden→dream
already holds, no cycle). The bare-literal spelling invited a trust-corruption — a misspelt `"supersede"`
writes a decision the fold never recognizes, silently leaving a merge-loser in the valid set — so the
contract has exactly one spelling.

The trust chain is preserved by RE-VALIDATING evidence on every write, exactly as `review.accept` does
(`_verified_pointers`): an op pools the source concepts' evidence (union or subset), runs each pointer back
through `blobstore.validate_span` (via `review.resolve_evidence`), and keeps only the spans that re-anchor
NOW — deduped and sorted for order-invariant bytes. So every minted/versioned concept carries exactly the
evidence that re-proves against its immutable blobs; a malformed or stale span is dropped on write, never
baked into a concept. And a re-validation that comes back EMPTY is **REFUSED** (a `ValueError`, the same
floor `review.accept` enforces at the human gate): an unbacked concept would feed dream/generate a belief
with no verifiable anchor, and because cleaned spans are TTL-eligible that floor is load-bearing, not
theoretical. Each of `merge`/`split` (PER-PART)/`abstract` takes an `allow_no_evidence=False` escape hatch
(mirroring accept's) for the deliberate, recorded override.

The evidence direction is a principled ASYMMETRY: **`abstract` defaults to the UNION** of its children's
evidence (a generalization legitimately subsumes all the specifics it abstracts over — the deterministic
layer can't pick a subset, and 3c-ii supplies a curated one via the `evidence=` override), while
**`split` requires a SUBSET** per part (a part is NARROWER than the original, so it must carry only the
slice that belongs to it). Same reason — evidence scope tracks conceptual scope — pointing opposite
directions. The six ops:

- **`merge(losers, winner)`** — a new winner VERSION unioning the losers' (re-validated) evidence + their
  relational edges (carried, not dropped); each loser invalidated by a `supersede` decision + a `supersedes`
  edge loser→winner (the edge points from the retired concept TO its replacement). Title/statement default
  to the winner's (3c-ii synthesizes); the losers' `source_takeaway`s ride in `origin_ref` (the concept
  schema keeps its single field). Idempotent: a re-run re-ingests the byte-identical winner — no churn.
- **`split(concept, parts)`** — mint a new concept per part carrying a re-validated SUBSET of the original's
  evidence; invalidate the original (`split` decision) + a `supersedes` edge original→part each.
- **`abstract(children, title, statement)`** — mint a NEW parent (evidence = union of children's, or a
  curated subset) + `generalizes` edges parent→child. Children STAY valid: a generalization ADDS a belief,
  it does not remove the specifics.
- **`reparent(concept, new_parent)`** — retract every active `generalizes` edge into the concept, assert the
  new one. Edge-only: no concept versioned or invalidated. Idempotent.
- **`retire(concept)`** — reuse `review.retire` (one op surface; the human-gate verb already folds out).
- **`merge_tags` / `retire_tag`** — vocab curation, §3.

Op-minted ids (`split` parts, `abstract` parents) are DETERMINISTIC on the op inputs (`review._mint_concept_id`'s
`c-` space) — a crash-retry re-mints the same id and is absorbed as a byte-identical version, never an orphan
duplicate (the resumability discipline dream's `mint_takeaway_id` already uses).

### 3. Tag-vocab curation — a fold-at-READ redirect, not an assignment rewrite

3b's vocabulary only ever GROWS (ADR-0014 §1 deferred the DOWN curation). 3c-i adds it as the symmetric
twin, and the design choice is *where the redirect lives*. Two options: re-point every assignment blob
(rewrite each concept's `tag_assignment` swapping the loser slug), or fold the merge at read. **Fold at
read.** A `tag_curation` blob (`source_id` = loser slug, content `{loser, winner, active}`, winner None for
a retire) records the redirect; `vocabulary` drops the loser from the set, and `concept_tags`/
`all_concept_tags` resolve loser→winner (chasing chains, cycle-guarded) at read. No concept or tag or
assignment blob is ever rewritten — the redirect is its own append-only, reversible (`active:false`)
artifact, and a concept re-groups under the curated vocab for free. This is strictly the ADR-0007 ethos
("state is a fold over append-only blobs, never a flipped field") and avoids the assignment-rewrite churn
(which would also flip every concept's `vocab_fingerprint` and force a re-tag). The cost — every tag read
now folds the (small) curation map — is the same cold-path cost the derived views already pay.

### 4. A fuzzy stakes gradient, defined in ONE place

3c-ii will route ops high→human-review (3d) / low→auto-apply. That threshold lives on `op_stakes(op) ->
float`, a FUZZY gradient (not hard lines, so the cut is a tunable knob): higher for ops that change what
concepts EXIST or ASSERT (`merge`/`split`/`retire`/`abstract` ≈ 0.65–0.85), lower for edge-only / vocab
curation (`reparent` 0.25, `retire_tag`/`merge_tags`/bare-edge ≈ 0.1–0.2). Breadth (more concepts touched)
nudges the score up a little, clamped to [0,1]; an unknown op lands mid-gradient (0.5) so a new,
unclassified op routes to review by default — fail-safe. The gradient is defined HERE, with the machinery,
so 3c-ii reads one source instead of re-deriving the policy.

## Consequences

- **Good:** structural editing with the same guarantees as the rest of ratchet — append-only,
  invalidate-don't-delete, the trust chain re-proven on every write, no store to desync, no migration. It is
  purely additive: the 3a/3b goldens are byte-identical (asserted edges + curation folds are empty until an
  op runs), proven by all prior suites staying green untouched. Asserted edges are first-class artifacts
  with the SAME blob/fold/retract shape as every other ratchet view, so the gardener's claims are auditable
  and reversible. The deterministic golden (`test_garden_ops`) is the contract 3c-ii writes against: it pins
  each op's append-only effects (winner unions + re-validates evidence, losers invalidated-not-deleted,
  supersedes/generalizes edges, subset/union evidence, the curation fold, the hierarchy spine, idempotency,
  and a malformed span dropped on write).
- **Costs / known limits:** the ops are CONTENT-deterministic but each appends a fresh `decision` blob per
  call (the "unique fact" shape review/dream already use), so a redundant re-run leaves harmless redundant
  decisions even where the concept/edge state is a byte-identical no-op — state is idempotent, the audit log
  is append-only. Edge carry-over on merge handles `generalizes`/`relates-to` but skips `supersedes`
  (lineage), and a complex tangled hierarchy could need a 3c-ii pass to re-assert cleanly. The concept schema
  keeps its single `source_takeaway`, so a merge unions the losers' takeaway pointers only into `origin_ref`
  provenance, not the body (a list-valued field is the clean upgrade if a reader ever needs them
  first-class). `op_stakes` weights are untuned named constants pending a gold set — the same caveat ADR-0013/
  0014 carry for the facet/tag weights. Tag curation folds at read with no transitive cap beyond the
  cycle-guard; the curation map stays small by construction.

## References

ADR-0007 (every artifact is a blob; state is a derived fold over append-only blobs, never a flipped field —
the asserted edges, the tag-curation redirects, and the concept-invalidating decisions all). ADR-0013 (the
3a facet substrate; §4 the DERIVED edges these ASSERTED edges are deliberately distinct from; §5 deferred
the structural ops to here; the recompute-on-read, weighted-overlap, rebuildable-view discipline the graph
fold preserves). ADR-0014 (3b managed tags; §1 deferred the vocab-DOWN curation `merge_tags`/`retire_tag`
implement; the auto-grow whose symmetric twin this is). ADR-0010 (dream v2 — `merge` unioning evidence,
deterministic minted ids for resumability, the freeze-then-fold discipline; §10 soft-revision-not-hard-freeze).
ADR-0012 (the WEAKEN path — invalidate-don't-delete / quarantine-over-deletion, Zep/AGM/SSGM/ExpeL, the
demote-by-decision-not-deletion pattern the concept ops mirror). The acceptance test
`tests/test_garden_ops.py`.
