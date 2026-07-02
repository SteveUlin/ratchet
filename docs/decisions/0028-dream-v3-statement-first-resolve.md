# 0028 — dream v3: statement-first resolve

- Status: accepted 2026-07-01 — design final; implementation in progress (S1, the glean stamps, landed)
- Date: 2026-07-01
- Supersedes: ADR-0010's `route`+`apply` consolidation core (dream v2). The `dream` module and its ADR
  stay for history; the distinct-session maturity gate, supersession model, and priority queue carry
  forward unchanged.
- Superseded by: —
- Extends: the redesign arc — 0012 (WEAKEN), 0013 (recompute-on-read), 0023 (recency weighting).

This is the compact record. The full design + rationale — graph model, cascade, review surface,
backpressure, thirteen keyed decisions, two rounds of adversarial critique — live in
`docs/dream-v3-design-2026-07-01.md` (**rev-3 final**), accepted 2026-07-01.

## Context

dream v2 routes each event by a forced choice over the whole takeaway catalog ("which of these N?") —
a framing biased toward picking *something*. On a real run it strengthened one takeaway
(`t-f837a3e9b25e`, "Zig structs are unnamed") with events about JAX autodiff and NumPy NEP-50, then
graduated it as "corroborated across 2 distinct sessions": corroboration measured co-occurrence in a
takeaway, not the same lesson recurring. The human gate caught the fusion — but the loop never closes:
v2 mutates `support` in place, so a wrong merge latches. The reviewer's verdict has no retraction to
land on; the fake-mature takeaway keeps its count.

Measured before deciding (`sig --band-report`, 2026-07-01: 284 events, 40,186 pairs): **zero of 40,186
pairs reach the deterministic merge bar** (`J_HIGH = 0.55`; the corpus maximum is 0.311, and that pair
is two *different* lessons sharing vocabulary), while genuine same-lesson paraphrases sit at Jaccard
**0.21–0.24**. So "mostly deterministic identity" is dead on this corpus: deterministic signals can
only REJECT; all acceptance is judgment, and the design must own that honestly.

## Decision

Replace route+apply with **statement-first entity resolution**: deterministic REJECTION at $0 + ONE
LLM call owning ACCEPTANCE + a human audit at the gate.

1. **A `resolve` Block, pairwise and deterministic-first.** Events carry a statement signature
   (char-shingle SimHash + shingle set + entropy) and a subject key (repo + co-located files), stamped
   at glean. Candidates come from the UNION of the subject-facet index and a **rare-shingle inverted
   index** — the recall channel that reaches down to 0.21–0.24 where real matches live (LSH bands
   collide only for near-duplicates, so they provide no paraphrase recall). Below `J_MAYBE` a pair is
   NON-MATCH at $0 — the 40k mass costs nothing.
2. **One comparative-with-none Haiku call owns acceptance.** Per event with residue candidates: one
   call over the top-`K_RESIDUE` by similarity, an explicit *none* option stated as the expected
   default. Never per-pair yes/no (the most over-merge-prone framing, per ComEM), never the whole
   catalog (v2's error). Every merge persists a match key — `{stmt_sim, subj, by, candidates_shown,
   prompt_version, model}` — so the review card renders exactly what the model saw.
3. **Corroboration is a minted, retractable edge.** Support = distinct sessions of live corroborates
   edges, recomputed on read (ADR-0013). Retracting an edge IS the split; nothing latches.
4. **Subject is soft scope, never a veto.** Disjoint subjects tag a claim `cross-cutting` and the
   review card shows it; a **single maturity bar** gates as today. Hard vetoes would kill exactly the
   cross-repo principles a global CLAUDE.md exists for.
5. **The active pool is a derived view** (`ACTIVE_FLOOR`/`ACTIVE_DAYS`) — the pool's only drain, since
   resolve consolidates every event and v2's `forget` never fires on the main path. Dormant claims
   fold out of the candidate indexes; blobs, edges, and history remain.
6. **The human "not the same" verdict is ONE compound `reject-merge` decision** carrying
   `{edge_id | pair, event_id}`: retraction + reopen + permanent pair-block in one atomic append (the
   triple-append it replaces had two crash windows). Resolve's done-key carries a reject-merge epoch so
   a reopened event re-enters the driver's item set.
7. **Merge suggestions are a derived query, not storage**: residue-band pairs of live active claims,
   minus reject-merged pairs, computed at review-render time and TTL'd. No stored object exists to
   harden into a de-facto merge (Wikidata's P460 failure).
8. **`synthesize` runs at maturity, never gating review.** New claims are born with `title = event
   summary, why = null`; Sonnet prose fills in only after a claim crosses the bar (or on review
   demand). `claim.stmt_sig` signs evidence summaries, never synthesized prose — signing prose chains
   transitive generalization, the slow-motion v2 failure.

## Backlog (named, deferred until evidence exists)

- `CONTRADICTION_WEIGHT` + sqrt saturation — speculative arithmetic with no contested claim to tune on.
- The dual maturity bar (`MATURITY_WEIGHT_CROSS`) — the corpus is single-project; no cross-subject
  pair exists to ground it.
- The negation lexicon (`NEGATION_TOKENS`) — required before reactivating the dormant $0-merge bands
  (`J_HIGH`/`J_CROSS`): a polarity-flipped restatement scores Jaccard ≈ 0.73 and would merge for free.
- Periodic Senzing/Splink-style ER audits — re-scoring stored match keys against the gold set the
  `sig` CLI drafts.

## Consequences

- glean stamps `subject_key` + `stmt_sig` on every new event (deterministic, no extra LLM call); old
  events compute-on-read — pre-stamp blobs project byte-identically, no migration.
- The residue call's verdict quality on ~10-word summaries is the load-bearing unknown: on this corpus
  it is the *entire* acceptance path. `sig --score-gold` over the hand-labeled pair file is the
  instrument that validates or retunes it.
- Every shipped threshold is a named, explained, CLI-overridable knob (the design-philosophy
  directive, ADR-0025/0026/0027); `J_HIGH`/`J_CROSS` stay documented-dormant in `sig.classify`.
