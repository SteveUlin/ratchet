# 0016 — the ops-proposer Block: a sharp model proposes structural ops, the gradient routes

- Status: accepted — implemented 2026-06-28 (all 15 suites green, incl. the 3a/3b/3c-i goldens, untouched)
- Date: 2026-06-28
- Supersedes: — builds the 3c-ii proposer ADR-0015 §4 anticipated (the LLM that DRIVES the 3c-i ops).
- Superseded by: —

Code (`ratchet/garden.py` — `GardenOpsBlock` + the proposer/coercion/queue machinery; `cluster_tension`,
`pending_proposals`, `queue_proposal`) is the source of truth; this records the *why*. This is **3c-ii**:
the sharp proposer that drives the deterministic 3c-i ops. The human gate that ACCEPTS the queued
high-stakes proposals (3d) is deliberately NOT here — a high-stakes op only gets QUEUED.

## Context

3c-i (ADR-0015) gave the gardener a deterministic, trust-critical op machinery — `merge`/`split`/`abstract`/
`reparent`/`retire` of concepts, `merge_tags`/`retire_tag` of the vocab — and the `op_stakes` gradient, but
nothing DECIDES which ops to run. That decision needs judgment: which two concepts say the same thing, which
one conflates two lessons, which several share a parent idea. It is the same shape as every other judgment
call in ratchet — so it is a model call, and the question is only HOW to spend it.

dream (ADR-0010) already answered the analogous question one level down: a CHEAP per-item router over an
in-prompt catalog, then a RARE sharp synthesizer where the router says it matters — a cascade, no embeddings.
3c-ii is that cascade one level UP. The cheap pre-gate is the 3a/3b clustering (ADR-0013/0014): facet-overlap
+ shared-tag leader clusters already narrow the field from "every concept pair" to "the few clusters of
related concepts." So the sharp call is RARE — ONE per high-tension cluster, not one per pair, and never an
O(n²) sweep nor an embedding index. The model reads a cluster, proposes edits; the deterministic machinery
applies the safe ones and queues the rest.

## Decisions

### 1. The cascade — cheap cluster (3a/3b), then ONE sharp call per high-tension cluster

`GardenOpsBlock.items()` is `concept_clusters` (via a single `concept_graph` pass that also yields the
per-node facets + asserted edges the proposer needs), filtered to clusters of size >= 2 (a singleton has
nothing to merge/relate). `process()` is ONE injected sharp `Completer` call (the Sonnet seat) over the
cluster: each concept's id + title + statement + provenance facets (repos/files/tools) + tags + a few
VERIFIED evidence quotes + the asserted edges among them. The pre-gate does the cheap narrowing; the sharp
call does the judgment — the whole `block.run` driver (budget/`--limit`/resume/priority) carries it, exactly
as the tagger Block and dream do.

`priority()` is the cluster's TENSION (ADR-0011's modular signal — highest first): a DENSE cluster of
well-related concepts backed by lots of evidence holds the most candidate edits, so it earns the sharp call
first. Tension is `cluster_tension` — size + the members' pairwise facet-overlap mass (the same `facet_score`
that grouped them) + total cited-evidence count, all pure and deterministic. A known limit: concept-level
CONTRADICTION signals live on TAKEAWAYS (ADR-0012), not concepts, so the cluster cannot read them directly —
tension proxies on the structural density it CAN see. The weights are untuned named constants, the same
caveat ADR-0013/0014 carry for the facet/tag weights.

### 2. The untrusted proposal — coerce hard, route on stakes, the rationale is the human's to check

The proposal is untrusted model output, so it passes the same defensive coercion as dream's `_clean_route`
(`_clean_op`): an op of an unknown kind, an op whose shape is malformed, or an op citing a concept NOT in
THIS cluster / not in the valid set is DROPPED — never act on an id the model invented out of nothing, nor
one outside the cluster it was actually shown (tag ops validate their slugs against the cluster's tags the
same way). The surviving ops are deduped on their deterministic identity and capped per cluster.

Each surviving op routes on `op_stakes` — the 3c-i FUZZY GRADIENT (ADR-0015 §4), read as the SINGLE source,
never a re-derived policy. The cut is `AUTO_APPLY_MAX_STAKES = 0.35`: at/below it an op AUTO-APPLIES (call the
3c-i fn directly), above it the op is QUEUED for the 3d gate. The value is RECALL-FIRST — it sits LOW, just
above the edge/tag/reparent band (`assert_edge` .10 … `reparent` .25) and well below the concept-altering band
(`abstract` .65 … `merge`/`split` .85). HONESTY: with the current `OP_STAKES` no op falls in (0.25, 0.65), so
0.35 is behaviourally IDENTICAL to any cut in that range — the "queue the fuzzy middle" framing is
FORWARD-LOOKING (for an op that later lands mid-gradient), not active today. It is a tunable knob (op_stakes IS
a gradient, not hard lines) — 3d can raise it as trust grows — exposed as `--auto-max-stakes`; the conservative
default is to QUEUE near the line.

Routing has a SECOND gate beside stakes: an op auto-applies only if it is ALSO auto-applicable — a kind
`_apply_op` actually handles (`AUTO_APPLICABLE_OPS`). `split` is the exception — its per-part EVIDENCE
PARTITION is the human's to choose, never auto-guessed — so a split (and any future kind `_apply_op` does not
handle) is QUEUED UNCONDITIONALLY, regardless of `--auto-max-stakes` (the **N1** fix). Without it, a
manually-raised threshold (>= 0.85) would route a split INTO `_apply_op`'s raise and error the whole cluster,
stranding the split (neither applied nor queued); with it, `_apply_op`'s split branch is a now-unreachable
defensive assertion. An auto-applied `merge` folds its losers in AS-IS — the prompt's merge shape carries no
synthesized title/statement, so the winner keeps its own framing; refining the merged concept's wording is the
human's call in 3d.

The op's RATIONALE is UNTRUSTED — the exact status of dream's `why`. It rides into the queued proposal (and
an auto-applied edge's NOTE) as the human-facing justification, surfaced to 3d, but it is NEVER a fact the
machine trusts: the faithfulness check — does the rationale hold against the verified evidence — belongs to
the 3d gate, mirroring how review.py checks a takeaway's `why`. The proposer proposes; the human adjudicates.

### 3. `garden_proposal` — an append-only QUEUED artifact, latest-wins, resolved by 3d

A queued proposal is a blob like every other ratchet artifact (ADR-0007): `source_kind="garden_proposal"`,
`source_id` = a DETERMINISTIC proposal id (`gp-` + sha256 of the op's IDENTITY — kind + params, NOT the
rationale), content `{proposal_id, op, params, concept_ids, rationale, stakes, cluster_leader, status,
prompt_version}`, latest-wins. Determinism is the resumability discipline dream's `mint_takeaway_id` uses one
level down: a re-proposal of the SAME structural edit re-versions the SAME proposal (no duplicate), while a
changed rationale is just a new version. `pending_proposals(root)` folds `latest_by_kind('garden_proposal')`
to the entries whose latest version's `status` is "pending" — state is a fold, never a flipped field. The 3d
gate will RESOLVE a proposal by appending a new VERSION flipping `status` (latest-wins), so a resolved
proposal drops out of the fold with no deletion and no separate index — and 3d itself is the next section.

CORRECTNESS does not hinge on the per-cluster done-skip: an auto-applied op is itself idempotent (3c-i
guarantees a byte-identical no-op) and a queued proposal re-versions its deterministic id, so a re-gardened
cluster is safe — the done-skip (keyed on the cluster leader + prompt_version + model) only saves the
expensive sharp call. A `commits_per_item=True` Block, so a kill keeps every completed cluster.

## Consequences

- **Good:** the structural-op DECISION now has the same shape as the rest of ratchet — a cheap pre-gate, a
  rare sharp call, defensive coercion of untrusted output, append-only artifacts, recall-first routing to the
  human. It is purely ADDITIVE: the 3a/3b/3c-i goldens are byte-identical (no proposer run mutates the views;
  `garden_proposal` is a new kind nothing else folds), proven by all prior suites staying green untouched. A
  high-stakes op is INERT until 3d accepts — the concept layer cannot be silently corrupted by a model. The
  deterministic acceptance test (`test_garden_propose`) pins the contract: coercion drops unknown-kind /
  out-of-cluster / nonexistent-id ops, a high-stakes merge is QUEUED (concepts unchanged, no supersedes
  edge), a low-stakes relate AUTO-APPLIES, `pending_proposals` folds the queue, a re-run done-skips, and a
  split at a raised `--auto-max-stakes` still QUEUES (N1 — never stranded on `_apply_op`'s raise).
- **Costs / known limits:** the auto/queue threshold (0.35) and the tension weights are untuned named
  constants pending a gold set — the same caveat ADR-0013/0014/0015 carry.
- **The done-skip is a RECALL trade, not just a cost saver.** It keys on the cluster LEADER, so a concept that
  JOINS an existing-leader cluster is NOT re-proposed against its new neighbours until a prompt bump or a
  leader change. Deliberate low-churn — one Sonnet call per cluster is expensive, unlike 3b's per-concept
  Haiku tagging, so re-proposing on every membership shift would not pay — and re-gardening is anyway safe to
  skip (an auto-applied op is idempotent, a queued proposal re-versions its deterministic id, so no duplicate
  or corruption). The stronger-recall alternative — key the marker on a sorted-MEMBERS fingerprint so any
  membership change re-proposes — is DEFERRED until recall proves insufficient.
- **Tension ignores the concept-side structural signal it could read.** The asserted relations among a
  cluster's members are loaded (and shown to the proposer), but `cluster_tension` recomputes facet cohesion
  ONLY — it does not score those edges. That signal was considered and DROPPED: its direction is ambiguous
  (more asserted structure can mean more still to do, OR a cluster already gardened), and the real
  CONTRADICTION signals live on TAKEAWAYS (ADR-0012), not concepts, so the cluster cannot read them at all.
- **Operational rollout:** start real runs at `--auto-max-stakes 0.15` — that auto-applies only the edge
  asserts (`relate`/`assert_edge` .10) and `merge_tags` (.15), keeping the two most user-visible auto ops,
  `reparent` (.25) and `retire_tag` (.20), QUEUED — and raise the cut as the gardener earns trust.
- The proposer reads the WHOLE cluster in one prompt — fine while clusters stay small (the 3a/3b weights keep
  them so), the same in-prompt assumption dream's catalog and the tag vocabulary rest on. `split` is queued
  ONLY, never auto (N1 — its evidence partition is a human judgment); tag-vocab ops validate slugs against the
  cluster's tags, so a cross-cluster tag merge needs a cluster that surfaces both.

## References

ADR-0015 (the 3c-i op machinery this DRIVES — `op_stakes` the single routing gradient §4, the append-only /
invalidate-don't-delete / trust-chain-on-write discipline the auto-applied ops inherit, the deterministic
minted-id resumability the proposal id reuses). ADR-0013 (the 3a facet substrate — the clusters that are the
cheap pre-gate, `facet_score` the cohesion signal in `cluster_tension`). ADR-0014 (the 3b managed tags — the
second grouping axis the clusters fold in, and the tag ops the proposer may propose). ADR-0011 (the modular
`priority` signal — cluster tension is the gardener's). ADR-0010 (dream v2 — the route/synth cascade this is
one level up from; the in-prompt-catalog no-embeddings discipline; the freeze-at-run-start view; the
defensive `_clean_route` coercion `_clean_op` mirrors; the stable-minted-id resumability). The acceptance
test `tests/test_garden_propose.py`.
