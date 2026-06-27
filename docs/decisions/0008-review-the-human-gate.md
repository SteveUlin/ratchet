# 0008 — review: the human gate, with Claude as the active faithfulness-checker

- Status: accepted
- Date: 2026-06-26
- Supersedes: 0006 §"Deferred 1 (faithfulness gate)" as a *separate `verify` stage* — the check folds
  into review instead. Builds on 0007 (concepts/decisions are blobs).
- Superseded by: —

Code (`ratchet/review.py`) + the `/ratchet-review` skill are the source of truth; this records the *why*.

## Context

dream produces **takeaways** (a synthesized `why` + verified evidence). They are the reviewable unit,
but the `why` is the one thing the trust chain cannot prove — it cites real events yet can
over-generalize past them (ADR-0006's top gap). review is the one hard gate in the pipeline and the
stage where the loop closes: an accepted takeaway becomes a **concept** that dream reads next run to
judge belief-change. Because a false concept feeds back into the system and is far costlier than a
missed one (sulin: "approving a lie is pretty costly"), the gate must make the human's call *informed*.

## Decisions

### Two layers: a pure backend + a skill where Claude is active

`review.py` is the pure, offline-tested **backend** — it serves materials and records verdicts. The
human interaction lives in the `/ratchet-review` **skill**, where Claude is an active participant. The
split mirrors the rest of ratchet (deterministic core + an injected/interactive edge).

### Verify folds INTO review — no separate stage

The faithfulness check (does the `why` follow from its cited quotes?) is **not** a batch gate upstream;
it is **Claude, at the moment sulin judges**. This is where the prior art says it pays off — Traceable
Text measured a provenance-link review UI lifting a reviewer's catch-rate on hallucinated summaries
12.5%→70%, and the gains are near-zero on already-correct ones, so the check belongs *with* the human,
on the doubtful cases, not as a blanket pass. Concretely the skill runs **two-speed**:

- **fast path:** the `why` follows the quotes and support is decent → present compactly, sulin approves.
- **deep path** (Claude escalates *itself* on a risk signal — thin support, why-drift, a `contradicts`
  relation, or on request): pull the surrounding transcript (`--context`), investigate, and show sulin
  the discrepancy before asking. The deep path is a research-and-learning moment, not a rubber stamp.

This is FActScore/SAFE's "atomize the claim, check each piece against the evidence" done
conversationally by Claude (no NLI model → stays stdlib at this layer), and — per SAFE's ~1-in-4
auto-verifier error rate — it *informs* the human rather than auto-gating. Claude never decides.

### Zero-click verified evidence; the reviewer judges interpretation only

Each cited span is **re-resolved and re-validated** from the immutable blob at the review read boundary
(a malformed/foreign span is dropped, never shown), so the UI can mark every quote **verified ✓**. The
substring guarantee is the thing almost no comparable system has — the reviewer never asks "is this
quote real," only "is the interpretation right." A widening **context window** feeds the deep path.

### Four verbs + retire, as append-only decision blobs (ADR-0007)

`accept` / `reject` / `snooze` / `edit`, plus `retire` for concepts — each an immutable decision blob
referencing its target; state is the latest decision, never a flipped field.

- **accept** mints/updates a **concept** and records Claude's `assessment` + sulin's call as provenance
  ("verified by Claude, approved by sulin"). The takeaway's `relation` decides identity:
  `strengthens`/`refines` a known concept → a new *version* of that concept; `new` → a freshly minted,
  stable concept id (derived from the takeaway, not the shifting `cluster_signature`, so a later
  refinement reuses it via `relation.concept_id`).
- **edit** is accept-with-changes: the corrected `title`/`why` becomes the concept, and the decision
  captures *before/after* — the highest-value correction signal (a future PROMPT_VERSION can learn from
  it). 
- **reject** persists as a label — no fine-tuning; a later prompt bump folds rejections into negative
  few-shot and suppresses semantic near-dupes, fixing the "keeps re-suggesting dismissed things"
  trust-killer (MemPrompt).
- **snooze** defers until a `until` time, *validated as ISO at write time* — an unparseable trigger
  would never fire and the snooze would become a permanent graveyard. "Re-surface on more evidence" is
  deliberately *not* a snooze trigger: more evidence grows a cluster into a new `cluster_signature` →
  a fresh takeaway that surfaces on its own (the old one superseded), so a corroboration counter keyed
  on a now-frozen id would be inert.
- **retire** drops a concept from the valid set (a contradiction sulin affirms) — not a deletion; the
  blob + history stay.
- An **accept** stores into the concept ONLY the evidence that re-validates at decision time (the same
  filter the reviewer's view passed) — the trust chain reaches the concept, never the raw takeaway
  evidence — and refuses a takeaway with no resolvable evidence unless explicitly overridden.

### The queue, validity, and the closed loop are derived queries

`pending` = `dream.current_takeaways` minus anything with a terminal decision or a live snooze —
references only, nothing stored. `valid_concepts` (= `dream.load_concepts`, kept there to avoid a
review→dream import cycle) = the latest version of each concept source minus any whose latest decision
is `retire`. **The loop closes**: review writes concept blobs, `dream.load_concepts` reads them, and
the next dream run labels related takeaways `strengthens`/`refines`/`contradicts`.

## Consequences

- **Good:** the human touches exactly one node (the gate) and a single decision propagates both
  directions — outward (concepts → generate → skills) and inward (concepts → dream's belief-change);
  the faithfulness check is contextual and can go arbitrarily deep, instead of a shallow batch pass;
  concept versioning + retirement come free from the TimeMap + decisions; the whole backend is offline
  testable, and the trust chain reaches the reviewer.
- **Costs / known limits:** the skill's faithfulness check is Claude's judgment, not a measured
  quantity (it informs, never auto-gates — correctly); volume control depends on dream's clustering
  staying tight (a flooded queue gets bulk-dismissed — the Dependabot trust-cliff); concept identity is
  a minted opaque id, so cross-concept merge/split is a future review action, not yet modelled; the
  derived queue/validity scans are O(total blobs) until ADR-0002's index lands.
- **Deferred (found by adversarial review):** a *retired* concept is not re-established by a later
  refinement — dream stops surfacing it, so a takeaway won't normally re-reference it; re-establishing
  is a future manual action. Reject/snooze decisions attach to a `cluster_signature` that supersession
  can retire, so they accumulate as orphans with no reclaim path (consistent with the deferred-GC
  stance — ADR-0006/0007). An accept ingests the concept *before* recording its decision (the correct
  crash order: a crash leaves a valid concept whose acceptance re-completes on retry, never a takeaway
  out of the queue with no concept) — so a crash window can leave a concept valid before its accept
  decision lands; harmless (the retry re-records, same content).

## References

From the review prior-art pass (see [[ratchet-extraction-prior-art]]): Traceable Text 2409.13099
(phrase-level provenance lifts reviewer error-catching; gains concentrate on the bad cases); FActScore
2305.14251 + SAFE 2403.18802 (atomize a multi-claim summary and check each piece — done conversationally
here); RARR 2210.08726 (flag/route, never silently revise — why edit is explicit, not auto-applied);
Dependabot trust-cliff (volume control is load-bearing); MemPrompt (close the loop with labels, no
fine-tuning).
