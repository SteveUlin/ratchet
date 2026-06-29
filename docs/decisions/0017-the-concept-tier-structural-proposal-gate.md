# 0017 — the concept-tier review: a second human-gate tier for structural-op proposals

- Status: accepted — implemented 2026-06-28 (all 16 suites green, incl. the 3a/3b/3c-i/3c-ii goldens, untouched)
- Date: 2026-06-28
- Supersedes: — builds the 3d gate ADR-0016 deferred ("the human gate that ACCEPTS the queued high-stakes
  proposals is deliberately NOT here — a high-stakes op only gets QUEUED").
- Superseded by: —

Code (`ratchet/review.py` — `pending_proposals`/`accept_proposal`/`reject_proposal`/`_apply_proposal`;
`ratchet/garden.py` — `open_proposals`/`RESOLVE_VERBS` + the decision-sourced `queue_proposal` suppression; the
`/ratchet-review` skill's tier-2 section) is the source of truth; this records the *why*. This is **3d**, the
human gate that **closes Section 3**: the gardener (3b/3c) now proposes structural edits, and a human — advised
by Claude — accepts or rejects them.

## Context

3c-ii (ADR-0016) gave the gardener a SHARP proposer that reads each high-tension concept cluster and proposes
structural ops. It auto-applies the low-stakes ones (edge/tag/reparent) and QUEUES the high-stakes ones
(merge/split/abstract/retire) as append-only `garden_proposal` blobs — but nothing RESOLVES that queue. A
queued op is inert; it needs a human, because a bad restructuring of the concept layer (merging two distinct
lessons, retiring a still-true one) corrupts ratchet's most trusted artifact and feeds back into dream/generate
— the exact "approving a lie is costly" pressure review already exists for (ADR-0008).

So 3d is not a new mechanism — it is the *same* human gate, a SECOND TIER beside the takeaway-promotion tier.
Tier 1 promotes a synthesized takeaway into a concept (a belief ADDED). Tier 2 adjudicates a structural op (the
concept GRAPH reorganized). Both share the one invariant the gate enforces: the model's *justification* is
untrusted, the *evidence* is ground truth, and the human decides on the evidence. review.py already serves that
discipline for takeaways; 3d extends it, almost entirely by reuse.

## Decisions

### 1. Two tiers, one gate — the structural-op queue beside the takeaway queue

`review.pending_proposals(root)` is the tier-2 query, parallel to `pending`: it folds `garden.open_proposals`
(the open `garden_proposal` queue) and renders each proposal for the human — its `op` + `params` + UNTRUSTED
`rationale`, plus EACH cited concept with its title/statement, a `valid` flag, and its **re-validated evidence**
(`resolve_evidence`, the same read-side trust anchor tier 1 uses). The rationale is the proposer's `why`,
carried as provenance, never trusted; the cited evidence is what reaches the reviewer as ground truth — so the
faithfulness check (does the rationale FOLLOW from the evidence?) has something real to judge against. A
still-`valid` `retire`/`merge` target is flagged, the skill's escalation cue.

### 2. Accept APPLIES the op via the 3c-i machinery — then records the decision

`accept_proposal(proposal_id, …)` loads the proposal, APPLIES the op by calling its 3c-i `garden` fn with the
proposal's params — `merge`/`split`/`abstract`/`reparent`/`retire`/`merge_tags`/`retire_tag` — and only THEN
records the accept (an append-only decision; no status flip — see decision 4). This is deliberate reuse: the
*same* trusted, append-only, invalidate-don't-delete, trust-chain-on-write machinery that 3c-ii auto-applies the
low-stakes ops through (ADR-0015) is what the human gate applies the high-stakes ones through, so accept and
auto-apply land byte-identical effects — a `merge` unions the winner + invalidates the losers, a `retire` drops
a concept.

ORDER matters: apply FIRST, record SECOND. A refused op (a `split` whose parts re-validate to no evidence,
ADR-0015's zero-evidence floor) raises and leaves the proposal OPEN and unrecorded — never half-resolved.
`split` is the one op the proposal cannot fully carry: its per-part EVIDENCE PARTITION is the human's to choose
(why it is never auto-applied — ADR-0016 N1), and the queued params hold only title/statement per part, so the
reviewer supplies `split_parts`; absent it the queued parts re-validate to nothing and `split` refuses,
surfacing the requirement rather than guessing a partition.

### 3. Reject SUPPRESSES re-surfacing — the L2 feedback loop closing

`reject_proposal(proposal_id, reason, …)` flips the status to `rejected` and records the reject; the op is NOT
applied. The load-bearing part is what happens NEXT TIME. `mint_proposal_id` is deterministic on the op's
identity (kind + params), so a re-gardened cluster re-proposes the SAME id — and re-queuing a fresh version
over a rejected one would RESURRECT a dismissed op, the "re-suggests dismissed things" trust-killer
(MemPrompt) review.reject already guards tier 1 against. So `garden.queue_proposal` reads the resolve DECISION
FIRST — `blobstore.latest_decision(pid).verb in RESOLVE_VERBS` (`rejected`, or `accepted` — already applied):
a resolved proposal is NOT re-opened; the resolution stands and the re-queue is skipped. The gardener REMEMBERS
its dismissals.
This is the **L2 loop closing**: 3c-ii's proposals flow OUT to the gate, and the gate's verdicts flow BACK to
suppress what was dismissed. (Tier 1's analogous "remember rejections" is still a deferred PROMPT_VERSION
fold — ADR-0008; tier 2 closes it concretely because the proposal id IS the op identity, so suppression is
exact, not semantic.)

### 4. The queue is decision-driven — no status field

A `garden_proposal` is a QUEUED artifact with NO lifecycle field: the audit DECISION *is* the lifecycle, not a
denormalized mirror of it. The accept/reject is an append-only decision blob (`review._record`, verbs
`accept_proposal`/`reject_proposal`, target = the proposal id) carrying reviewer + Claude's assessment, and
`open_proposals` derives OPEN by SUBTRACTING any proposal whose `latest_decision` verb is in `RESOLVE_VERBS` —
the garden-owned `{accept_proposal, reject_proposal}` vocabulary review records against. So a resolved proposal
simply drops out of the fold: no status to flip, no second write, no derived field that could disagree with the
decision. This is **byte-symmetric with tier-1's `pending`** — there too the takeaway blob carries no review
state and the queue is `current_takeaways` minus `latest_decisions` (ADR-0007: state is a fold, never a flipped
field). The earlier design wrote BOTH a status-flipped `garden_proposal` re-version AND the decision; the status
was derivable from the decision alone, so collapsing to the decision removes the redundancy and the risk of the
two diverging. The decision target space (`gp-…`) is disjoint from takeaway (`t-…`) and concept (`c-…`) ids —
and the verbs are OUTSIDE tier-1's terminal set and `CONCEPT_INVALID_VERBS` — so a `gp-` decision never pollutes
the tier-1 `latest_decisions`/`load_concepts` folds.

### 5. The cycle break — a function-local `garden` import

`garden` imports `review` at module load (for `resolve_evidence`, the trust anchor it serves the proposer).
So review's tier-2 path imports `garden` FUNCTION-LOCALLY — `accept_proposal`/`reject_proposal`/
`pending_proposals`/`_apply_proposal` each `from . import garden` inside the body — the same break the
`concepts`↔`garden` pair already uses. The blob shapes and op fns stay in garden (its layer); review serves the
materials and records the verdict (its layer).

## Consequences

- **Good:** the structural-op DECISION now has the same shape as every other human-gate decision — untrusted
  justification, evidence as ground truth, human-decides — and reuses tier-1's plumbing (`resolve_evidence`,
  `_record`, the decision-blob pattern) almost wholesale. Purely ADDITIVE at the read-view layer: the
  3a/3b/3c-i goldens are byte-identical (no concept/edge/tag view changed; the new decision verbs target a
  disjoint `gp-` id space), proven by the prior suites staying green. (The 3c-ii proposer test moved with the
  rename `pending_proposals`→`open_proposals` and the dropped `status` field — a mechanical change, its
  behavioural assertions intact.) The new acceptance test (`test_review_proposals`) pins the contract:
  `pending_proposals` surfaces a queued
  merge with its cited evidence; `accept_proposal` APPLIES it (loser invalidated, winner unioned, supersedes
  asserted) and it leaves the queue accepted; `reject_proposal` leaves the graph unchanged, drops it from
  pending, and a re-queue of the SAME op stays rejected (the L2 close).
- **This CLOSES Section 3.** The gardener loop is now whole: 3a/3b build the concept-graph substrate (facets +
  tags), 3c proposes + applies structural ops (deterministic machinery 3c-i, sharp proposer 3c-ii), 3d gates the
  high-stakes ones. A model can no longer silently restructure the concept layer — every concept-altering op is
  either deterministic-low-stakes-auto or human-gated.
- **NOT closed here: the GENERATION review.** Projecting the curated concepts into CLAUDE.md/skills, and the
  separate human review of *that* output, is **Section 5** — a different gate over a different artifact (the
  generated config, not the concept graph). 3d is the concept-tier gate only.
- **Costs / known limits:** a `split` accept needs the human's evidence partition (`split_parts`) — the CLI
  takes it as JSON, but the rich "which quote goes where" interaction lives in the skill, and an unsure reviewer
  is told to reject and let the gardener re-propose. Accept/reject after a prior accept/reject is permitted (a
  manual override) rather than guarded — the audit trail records every call; resurrecting a genuinely-changed
  situation is a deliberate human action, not an automatic one. One narrow crash window: `accept` of a `split`
  applies the op — minting the parts and writing the invalidating `split` decision — BEFORE its own
  `accept_proposal` decision, so a crash between the two leaves the split fully applied but the proposal still
  OPEN (no accept decision). A re-accept re-runs `split`, which RAISES (the target is no longer valid) —
  non-destructively: the graph is consistent and the proposal can be cleared by `reject`. A future
  "already-applied → just record-accept" special-case is deferred (microsecond window, no data loss).

## References

ADR-0008 (review = the human gate; Claude the active faithfulness-checker; the decision-blob / latest-decision
pattern + `resolve_evidence` trust anchor tier 2 reuses; the deferred "remember rejections" tier 2 now closes
concretely). ADR-0015 (the 3c-i op machinery accept APPLIES — append-only, invalidate-don't-delete,
trust-chain-on-write, the zero-evidence floor a refused split hits, the `op_stakes` gradient that put the op in
the queue). ADR-0016 (3c-ii the proposer that QUEUES the high-stakes ops 3d resolves; the deterministic
`mint_proposal_id` that makes suppression exact; the `garden_proposal` blob shape; N1 — split is queued-only,
its partition the human's). ADR-0012 (the trust chain / net-entrenchment discipline the evidence re-validation
extends from takeaways to the cited concepts). ADR-0007 (every artifact is a blob; state is a fold, never a
flipped field — the decision-driven queue here, byte-symmetric with tier-1 `pending`). The acceptance test
`tests/test_review_proposals.py`.
