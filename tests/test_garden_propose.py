"""garden ops-proposer tests (3c-ii, ADR-0016): the SHARP-model structural-ops Block, exercised OFFLINE
with a FAKE proposer (no network, no API key) so the suite is deterministic. The proposer reads ONE
high-tension concept cluster and emits structural ops; low-stakes ops AUTO-APPLY the 3c-i machinery,
high-stakes ones are QUEUED as append-only `garden_proposal` blobs for the 3d human gate. The
load-bearing checks, all on the blob model:

  COERCION — `_clean_op` (mirror dream's `_clean_route`) drops an op of an unknown kind, an op citing a
    concept NOT in this cluster (a valid singleton outside it), and an op citing a NONEXISTENT id.
  ROUTE — a HIGH-stakes `merge` is QUEUED (a `garden_proposal`) and NOT applied (the concepts stay valid,
    the winner blob unchanged, no supersedes edge); a LOW-stakes `relate` (a relates-to edge) is AUTO-APPLIED
    (the 3c-i effect lands).
  QUEUE — `open_proposals` folds the open queue; the proposal carries op + cited ids + rationale + stakes.
  IDEMPOTENT — re-running done-skips a cluster already processed against the same prompt version/model
    (ZERO proposer calls).
  N1 / SPLIT — a `split` is NEVER auto-applicable (its per-part evidence partition is the human's), so even
    at a HIGH `--auto-max-stakes` it is QUEUED, never routed into `_apply_op`'s raise — neither auto-applied
    nor errored/stranded.

Run: `python tests/test_garden_propose.py` (throwaway dir)."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-garden-propose-")

from ratchet import blobstore, config, dream, garden, weave  # noqa: E402
from ratchet.completer import Completion  # noqa: E402

R = config.ensure_layout()
RUN = "propose-test"


# --- synthetic transcripts → cleaned blobs → concepts (mirrors test_garden_ops' harness) ----------

def rec(uuid, parent, typ, **kw):
    r = {"type": typ, "uuid": uuid, "parentUuid": parent}
    r.update(kw)
    return r

def amsg(mid, *blocks):
    return {"role": "assistant", "id": mid, "content": list(blocks)}

def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def jsonl(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def cleaned(tag):
    """Ingest a one-edit transcript and weave it → a real, non-empty cleaned blob a span can anchor into."""
    recs = [rec(f"{tag}-u", None, "user", message={"role": "user", "content": f"edit {tag}"}),
            rec(f"{tag}-1", f"{tag}-u", "assistant",
                message=amsg(f"{tag.upper()}1", tool_use(f"{tag}t1", "Edit", file_path=f"/{tag}/x.py",
                                                          old_string="alpha", new_string="beta")))]
    raw_h, _ = blobstore.ingest(jsonl(recs), source_kind="transcript", source_id=f"sess-{tag}",
                                origin_ref={"project": f"proj-{tag}", "session_id": f"sess-{tag}",
                                            "mtime": "2026-06-01T10:00:00+00:00"},
                                fetched_at="2026-06-01T10:00:00+00:00", root=R)
    ch, _, _ = weave.materialize(raw_h, root=R)
    return ch


def ev_ptr(event_id, cleaned_hash, *, start=0, end=3):
    return {"event_id": event_id, "cleaned_hash": cleaned_hash, "byte_start": start, "byte_end": end,
            "quote": "q", "context": "q"}


def mint(cid, title, evidence):
    concept = {"id": cid, "title": title, "statement": f"the {title} lesson",
               "evidence": evidence, "source_takeaway": f"t-{cid}"}
    blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=cid,
                     origin_ref={"stage": "test"}, root=R)


def valid_ids():
    return {c["id"] for c in dream.load_concepts(R)}

def active_edge(src, kind, dst):
    return any(e["src"] == src and e["kind"] == kind and e["dst"] == dst for e in garden.asserted_edges(R))


# Three concepts citing the SAME cleaned blob (a shared file → facet_score >= CLUSTER_THRESHOLD) cluster
# under leader c-k1; a fourth concept on a DISJOINT blob stays a singleton (filtered out, size 1) — a VALID
# concept that is NOT in the cluster, so an op citing it must be coercion-dropped.
ch_k = cleaned("k")
ch_o = cleaned("o")
mint("c-k1", "lesson one", [ev_ptr("ev-k1", ch_k)])
mint("c-k2", "lesson two", [ev_ptr("ev-k2", ch_k)])
mint("c-k3", "lesson three", [ev_ptr("ev-k3", ch_k)])
mint("c-out", "outside lesson", [ev_ptr("ev-o", ch_o)])


# --- the FAKE proposer: scripts ops for the cluster (detects which member ids are in the prompt) ----

class ProposerFake:
    def __init__(self, ops, *, cost=0.002):
        self.ops = ops
        self.cost, self.calls = cost, 0

    def __call__(self, system, user):
        self.calls += 1
        # only fire the script for the c-k cluster (the one prompt that names c-k1).
        ops = self.ops if "c-k1" in user else []
        return Completion(text=json.dumps({"ops": ops}), model="proposer-fake", cost_usd=self.cost)


SCRIPT = [
    # HIGH-stakes merge (~0.85 > AUTO_APPLY_MAX_STAKES) → QUEUED, NOT applied.
    {"op": "merge", "winner_id": "c-k1", "loser_ids": ["c-k2"], "rationale": "k1 and k2 say the same thing"},
    # LOW-stakes relate (assert_edge ~0.10) → AUTO-APPLIED.
    {"op": "relate", "src": "c-k1", "dst": "c-k3", "rationale": "k1 is associated with k3"},
    # COERCION drops, three ways:
    {"op": "merge", "winner_id": "c-out", "loser_ids": ["c-k1"],     # c-out is valid but NOT in this cluster
     "rationale": "cites a concept outside the cluster"},
    {"op": "relate", "src": "c-k1", "dst": "c-ghost",                # c-ghost is not a real concept
     "rationale": "cites a nonexistent id"},
    {"op": "frobnicate", "rationale": "an unknown op kind"},          # unknown kind
]


# === 0. pure coercion + stakes routing (no LLM) =====================================================

members, valid = {"c-k1", "c-k2", "c-k3"}, valid_ids()
assert garden._clean_op(SCRIPT[0], member_ids=members, valid_ids=valid, cluster_tags=set())["op"] == "merge", \
    "a well-formed merge over in-cluster valid ids survives coercion"
assert garden._clean_op(SCRIPT[2], member_ids=members, valid_ids=valid, cluster_tags=set()) is None, \
    "a merge whose winner is a VALID concept OUTSIDE the cluster is dropped"
assert garden._clean_op(SCRIPT[3], member_ids=members, valid_ids=valid, cluster_tags=set()) is None, \
    "a relate citing a nonexistent id is dropped"
assert garden._clean_op(SCRIPT[4], member_ids=members, valid_ids=valid, cluster_tags=set()) is None, \
    "an unknown op kind is dropped"
# the stakes gradient routes merge HIGH (queue), relate LOW (auto) around the recall-first threshold.
merge_desc = garden._clean_op(SCRIPT[0], member_ids=members, valid_ids=valid, cluster_tags=set())
relate_desc = garden._clean_op(SCRIPT[1], member_ids=members, valid_ids=valid, cluster_tags=set())
assert garden._op_stakes_of(merge_desc) > garden.AUTO_APPLY_MAX_STAKES, "merge is above the auto cut → queues"
assert garden._op_stakes_of(relate_desc) <= garden.AUTO_APPLY_MAX_STAKES, "relate is at/below the cut → auto"
# the proposal id is deterministic on the op IDENTITY (kind + params), not the rationale.
other = dict(merge_desc, rationale="a totally different justification")
assert garden.mint_proposal_id(merge_desc) == garden.mint_proposal_id(other), \
    "the proposal id keys on the op identity, so a re-proposal with a new rationale re-versions the same id"
print("OK §0 — coercion drops unknown-kind / out-of-cluster / nonexistent-id ops; stakes route merge>cut>relate.")


# === 1. run the Block: high-stakes merge QUEUED (not applied), low-stakes relate AUTO-APPLIED ========

k1_before = blobstore.latest_version("c-k1", R)
proposer = ProposerFake(SCRIPT)
run1 = garden.run_propose(proposer, root=R)

# exactly ONE cluster gardened (the c-k cluster; c-out is a filtered singleton), ONE proposer call.
assert run1.n_clusters == 1 and run1.examined == 1 and run1.processed == 1, \
    f"one cluster gardened: clusters={run1.n_clusters} examined={run1.examined} processed={run1.processed}"
assert proposer.calls == 1, "exactly one sharp proposer call (one per cluster)"
# two surviving ops: one queued (merge), one auto-applied (relate); three coercion-dropped.
assert run1.n_proposed == 2 and run1.n_queued == 1 and run1.n_applied == 1, \
    f"two surviving ops: {run1.n_proposed} proposed, {run1.n_queued} queued, {run1.n_applied} applied"

# the HIGH-stakes merge is QUEUED, NOT applied: both concepts stay valid, the winner blob is UNCHANGED,
# and no supersedes lineage edge was asserted (the merge never ran).
assert "c-k1" in valid_ids() and "c-k2" in valid_ids(), "the queued merge did NOT invalidate either concept"
assert blobstore.latest_version("c-k1", R) == k1_before, "the merge winner blob was NOT re-versioned (not applied)"
assert not active_edge("c-k2", "supersedes", "c-k1"), "no supersedes edge — the merge is queued, not applied"

# the LOW-stakes relate is AUTO-APPLIED: the relates-to edge landed (the 3c-i effect).
assert active_edge("c-k1", "relates-to", "c-k3"), "the low-stakes relate auto-applied (the asserted edge landed)"
# the dropped ops left NO trace: no edge to the ghost, no proposal for the out-of-cluster merge.
assert not active_edge("c-k1", "relates-to", "c-ghost"), "the nonexistent-id relate was dropped (no edge)"

# REGRESSION (--show crash): `proposals` holds the queued op DESCRIPTORS (dicts, mirroring `applied`), never
# blob hashes — `propose_main --show` formats p['op']/p['stakes']/p['concept_ids']/p['rationale'] off them.
assert len(run1.proposals) == 1 and isinstance(run1.proposals[0], dict), \
    f"proposals holds the queued op dicts, not hashes: {run1.proposals}"
qd = run1.proposals[0]
assert (qd["op"] == "merge" and qd["concept_ids"] == ["c-k1", "c-k2"] and qd["rationale"]
        and qd["stakes"] > garden.AUTO_APPLY_MAX_STAKES and qd["proposal_id"].startswith("gp-")), \
    f"the queued descriptor carries op/stakes/concept_ids/rationale/proposal_id: {qd}"
# the exact --show line renders (the TypeError this regression guards).
_ = f"  queued   [{qd['op']} · stakes {qd['stakes']:.2f}]  {qd['concept_ids']}  {qd['rationale']!r}"
print("OK §1 — high-stakes merge QUEUED (concepts unchanged); low-stakes relate AUTO-APPLIED; drops leave no trace.")


# === 2. open_proposals folds the open queue; the proposal carries the op + ids + rationale + stakes

q = garden.open_proposals(R)
assert len(q) == 1, f"exactly one proposal is queued (the merge): {len(q)}"
p = q[0]
assert p["op"] == "merge" and set(p["concept_ids"]) == {"c-k1", "c-k2"}, \
    f"the queued proposal is the merge over c-k1/c-k2: {p['op']} {p['concept_ids']}"
assert "status" not in p, "a queued proposal carries NO status field — the resolve decision is its lifecycle"
assert p["rationale"] == "k1 and k2 say the same thing", "the UNTRUSTED rationale rides into the proposal"
assert p["stakes"] > garden.AUTO_APPLY_MAX_STAKES, "the queued op's recorded stakes are above the auto cut"
assert p["proposal_id"] == garden.mint_proposal_id(merge_desc), "the proposal id is the op's deterministic identity"
# the queued op did NOT touch the concept layer (re-assert: a garden_proposal is inert until 3d accepts).
assert blobstore.latest_version("c-k1", R) == k1_before, "queuing a proposal mutates nothing in the concept layer"
print("OK §2 — open_proposals folds the open queue; the proposal carries op + cited ids + rationale + stakes.")


# === 3. idempotency: re-running done-skips the already-gardened cluster (ZERO proposer calls) ========

calls_before = proposer.calls
run2 = garden.run_propose(proposer, root=R)
assert run2.examined == 1 and run2.skipped == 1 and run2.processed == 0, \
    f"the cluster (same prompt/model) is done-skipped: examined={run2.examined} skipped={run2.skipped}"
assert proposer.calls == calls_before, "an idempotent re-run makes ZERO proposer calls"
assert len(garden.open_proposals(R)) == 1, "the queue is unchanged (no duplicate proposal)"
assert "c-k2" in valid_ids() and active_edge("c-k1", "relates-to", "c-k3"), "the prior run's effects are stable"
print("OK §3 — a cluster gardened against the current prompt/model is done-skipped; no churn, no duplicate.")


# === 4. N1: a split is NEVER auto-applied — at a HIGH --auto-max-stakes it QUEUES, never strands ======
# split's per-part EVIDENCE PARTITION is the human's to choose, so `_apply_op` can't apply it. WITHOUT N1, a
# manually-raised threshold (>= 0.85) would send op_stakes .87 down the auto path INTO `_apply_op`'s raise —
# erroring the whole cluster and stranding the split (neither applied nor queued). N1 routes a non-auto-
# applicable op to the QUEUE unconditionally, so the split lands in open_proposals instead.
SPLIT_OP = {"op": "split", "concept_id": "c-k1",
            "parts": [{"title": "split part a", "statement": "the narrower lesson a"},
                      {"title": "split part b", "statement": "the narrower lesson b"}],
            "rationale": "c-k1 conflates two distinct lessons"}
split_desc = garden._clean_op(SPLIT_OP, member_ids={"c-k1", "c-k2", "c-k3"}, valid_ids=valid_ids(),
                              cluster_tags=set())
assert split_desc is not None and garden._op_stakes_of(split_desc) > garden.AUTO_APPLY_MAX_STAKES, \
    "the split survives coercion and scores HIGH on the stakes gradient"

split_proposer = ProposerFake([SPLIT_OP])
# a DISTINCT model changes the done-key (leader, prompt, model), so the already-gardened cluster re-proposes;
# auto_max_stakes=1.0 lifts the cut ABOVE split's stakes — the ONLY thing keeping it off the auto path is N1.
run4 = garden.run_propose(split_proposer, model="proposer-fake-2", auto_max_stakes=1.0, root=R)
assert run4.errored == 0, "the split routed cleanly — N1 means it never reached `_apply_op`'s raise (no error)"
assert run4.n_applied == 0 and run4.n_queued == 1, \
    f"the split was QUEUED, not auto-applied: applied={run4.n_applied} queued={run4.n_queued}"

split_pid = garden.mint_proposal_id(split_desc)
queue_ids = {p["proposal_id"] for p in garden.open_proposals(R)}
assert split_pid in queue_ids, "the split landed in the pending queue (queued, not stranded)"
# and it did NOT apply: c-k1 stays valid (no `split` decision invalidated it) — inert until 3d accepts.
assert "c-k1" in valid_ids(), "the queued split did NOT invalidate c-k1 (a proposal is inert until 3d accepts)"
print("OK §4 — a split at a HIGH --auto-max-stakes is QUEUED (never auto-applied, never errored/stranded).")

print("test_garden_propose: all assertions passed")
