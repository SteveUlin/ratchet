"""review structural-proposal tier tests (3d, ADR-0017): the SECOND human-gate tier, exercised OFFLINE and
deterministically (no LLM — a proposal is queued directly as a `garden_proposal` blob, exactly as 3c-ii does).
Tier 1 promotes takeaways into concepts; tier 2 here gates the gardener's queued STRUCTURAL ops. The
load-bearing checks, all on the blob model:

  SURFACE — `review.pending_proposals` folds the open queue (`garden.open_proposals`) and renders each proposal
    with its op + UNTRUSTED rationale + the CITED concepts' RE-VALIDATED evidence (the trust chain reaches the
    reviewer; the rationale is untrusted, the evidence is ground truth).
  ACCEPT APPLIES — `accept_proposal` calls the 3c-i fn (merge): the concept graph reorganizes (loser
    invalidated, winner unioned, a supersedes lineage edge asserted) and the accept is recorded as a DECISION.
    DECISION-DRIVEN: the proposal blob carries NO status field — the resolve decision (`latest_decision.verb in
    RESOLVE_VERBS`) is the whole lifecycle, so it drops from the open queue with nothing flipped (tier-1 symmetry).
  REJECT SUPPRESSES — `reject_proposal` does NOT apply the op (concepts unchanged), drops it from the open queue
    (the reject decision), and — the L2 loop closing — a subsequent `garden.queue_proposal` of the SAME op does
    NOT re-surface it (`queue_proposal` reads that same decision). An accepted op is suppressed the same way; a
    never-decided op queues.

Run: `python tests/test_review_proposals.py` (throwaway dir)."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-review-proposals-")

from ratchet import blobstore, concepts, config, garden, review, weave  # noqa: E402

R = config.ensure_layout()
RUN = "proposals-test"


# --- synthetic transcripts → cleaned blobs → concepts (mirrors test_garden_propose's harness) -------

def rec(uuid, parent, typ, **kw):
    r = {"type": typ, "uuid": uuid, "parentUuid": parent}
    r.update(kw)
    return r

def amsg_text(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}

def jsonl(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def cleaned(tag):
    """Ingest a one-turn transcript whose assistant text carries a unique ASCII MARKER, weave it → a real
    cleaned blob, and return (cleaned_hash, marker) so a span can anchor onto verbatim bytes."""
    marker = f"MARK{tag.upper()}END"
    recs = [rec(f"{tag}-u", None, "user", message={"role": "user", "content": f"work on {tag}"}),
            rec(f"{tag}-1", f"{tag}-u", "assistant",
                message=amsg_text(f"{tag.upper()}1", f"the {marker} lesson holds across many sessions"))]
    raw_h, _ = blobstore.ingest(jsonl(recs), source_kind="transcript", source_id=f"sess-{tag}",
                                origin_ref={"project": f"proj-{tag}", "session_id": f"sess-{tag}",
                                            "mtime": "2026-06-01T10:00:00+00:00"},
                                fetched_at="2026-06-01T10:00:00+00:00", root=R)
    ch, _, _ = weave.materialize(raw_h, root=R)
    return ch, marker


def ev_for(event_id, ch, needle):
    """An evidence pointer onto the REAL byte offsets of `needle` in the cleaned blob — so the span
    re-validates (review re-resolves it; a guessed offset that split a char would be dropped)."""
    data = blobstore.get(ch, R).encode("utf-8")
    i = data.find(needle.encode("utf-8"))
    assert i >= 0, f"marker {needle!r} not rendered into cleaned blob {ch[:8]}"
    return {"event_id": event_id, "cleaned_hash": ch, "byte_start": i,
            "byte_end": i + len(needle.encode("utf-8")), "quote": needle}


def mint(cid, title, evidence):
    concept = {"id": cid, "title": title, "statement": f"the {title} lesson",
               "evidence": evidence, "source_takeaway": f"t-{cid}"}
    blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=cid,
                     origin_ref={"stage": "test"}, root=R)


def valid_ids():
    return {c["id"] for c in concepts.load_concepts(R)}

def queue_ids():
    return {p["proposal_id"] for p in review.pending_proposals(R)}


# Two concepts citing DISTINCT cleaned blobs — the merge winner (c-a) and loser (c-b). After the merge the
# winner must UNION both evidence spans, so each cites its own blob.
ch_a, mk_a = cleaned("a")
ch_b, mk_b = cleaned("b")
mint("c-a", "version control", [ev_for("ev-a", ch_a, mk_a)])
mint("c-b", "vc again", [ev_for("ev-b", ch_b, mk_b)])

# A merge proposal, queued exactly as 3c-ii would (high-stakes → QUEUED, not auto-applied).
merge_desc = {"op": "merge", "params": {"winner_id": "c-a", "loser_ids": ["c-b"]},
              "concept_ids": ["c-a", "c-b"], "rationale": "c-a and c-b state the same lesson"}
pid = garden.mint_proposal_id(merge_desc)
garden.queue_proposal(merge_desc, cluster_leader="c-a", stakes=0.85, root=R, run_id=RUN, model="test")


# === 1. SURFACE: pending_proposals folds the queue, with cited concepts' RE-VALIDATED evidence ========

q = review.pending_proposals(R)
assert len(q) == 1, f"exactly one proposal is queued: {len(q)}"
p = q[0]
assert p["proposal_id"] == pid and p["op"] == "merge", "the surfaced proposal is the queued merge"
assert p["rationale"] == "c-a and c-b state the same lesson", "the UNTRUSTED rationale rides to the reviewer"
by_cid = {c["concept_id"]: c for c in p["concepts"]}
assert set(by_cid) == {"c-a", "c-b"}, "both cited concepts are presented"
assert by_cid["c-a"]["valid"] and by_cid["c-b"]["valid"], "both cited concepts are still valid (shown to the gate)"
# the trust chain reaches the reviewer: each cited concept's evidence re-resolves to verbatim bytes.
assert by_cid["c-a"]["evidence"] and all(e["verified"] for e in by_cid["c-a"]["evidence"]), "evidence re-validated"
e0 = by_cid["c-a"]["evidence"][0]
assert blobstore.get(e0["cleaned_hash"], R).encode("utf-8")[e0["byte_start"]:e0["byte_end"]].decode() == mk_a, \
    "the presented evidence span resolves to the real verbatim quote (verified real)"
print("OK 1 — pending_proposals folds the open queue; each proposal carries op + untrusted rationale + the "
      "cited concepts' re-validated evidence (the trust chain reaches the reviewer).")


# === 2. ACCEPT APPLIES: the op runs (concept graph reorganizes), the proposal leaves pending ===========

a_before = blobstore.latest_version("c-a", R)
res = review.accept_proposal(pid, root=R, assessment="rationale follows the cited evidence", note="clear merge")
assert res["status"] == "accepted" and res["op"] == "merge" and res["result"] == "c-a", \
    f"accept_proposal applied the merge and returned the winner: {res}"
# the concept graph reorganized per 3c-i: loser invalidated, winner unioned + re-versioned.
assert "c-b" not in valid_ids(), "the merge loser left the valid set (invalidate-don't-delete)"
assert "c-a" in valid_ids(), "the merge winner stays valid"
assert blobstore.latest_version("c-a", R) != a_before, "the winner concept was re-versioned (the merge applied)"
winner = [c for c in concepts.load_concepts(R) if c["id"] == "c-a"][0]
assert len(winner["evidence"]) == 2, f"the winner unioned BOTH concepts' evidence: {len(winner['evidence'])}"
assert any(e["src"] == "c-b" and e["kind"] == "supersedes" and e["dst"] == "c-a" for e in garden.asserted_edges(R)), \
    "the merge asserted the supersedes lineage edge loser→winner"
# DECISION-DRIVEN: the accept DECISION is the proposal's lifecycle — it drops from the open queue with NO
# status field flipped (byte-symmetric with tier-1's `pending`).
assert pid not in queue_ids(), "the accepted proposal left the open queue (a resolve decision drops it)"
assert pid not in {p["proposal_id"] for p in garden.open_proposals(R)}, \
    "garden.open_proposals also drops it — the decision-sourced fold, not a status field"
# the accept is recorded with reviewer + assessment (audited provenance) — and that decision IS the lifecycle.
d = blobstore.latest_decision(pid, R)
assert d["verb"] == "accept_proposal" and d["assessment"].startswith("rationale follows") and d["op"] == "merge", \
    f"the accept decision records the op + reviewer assessment: {d}"
print("OK 2 — accept_proposal APPLIES the op via the 3c-i machinery (loser invalidated, winner unioned, "
      "supersedes lineage asserted); the accept DECISION drops it from the open queue (no status field).")


# === 3. REJECT SUPPRESSES: not applied, drops from pending, and never re-surfaces (the L2 loop closing) =

ch_r, mk_r = cleaned("r")
mint("c-r", "still supported", [ev_for("ev-r", ch_r, mk_r)])
retire_desc = {"op": "retire", "params": {"concept_id": "c-r"}, "concept_ids": ["c-r"],
               "rationale": "claims c-r is stale"}
rpid = garden.mint_proposal_id(retire_desc)
garden.queue_proposal(retire_desc, cluster_leader="c-r", stakes=0.80, root=R, run_id=RUN, model="test")
assert rpid in queue_ids(), "the retire proposal is queued (the suppression check is non-vacuous)"

rej = review.reject_proposal(rpid, root=R, reason="c-r is still supported", assessment="rationale overreaches")
assert rej["status"] == "rejected" and rej["op"] == "retire", f"reject_proposal records the verdict: {rej}"
# the op was NOT applied — the concept graph is untouched.
assert "c-r" in valid_ids(), "reject did NOT apply the retire — the concept stays valid (graph unchanged)"
assert rpid not in queue_ids(), "the rejected proposal left the open queue (the reject decision drops it)"
d = blobstore.latest_decision(rpid, R)
assert d["verb"] == "reject_proposal" and d["reason"].startswith("c-r is still"), \
    "the reject DECISION is recorded — the proposal's lifecycle (no status field flipped)"

# THE L2 LOOP CLOSING: re-queuing the SAME op (deterministic id) must NOT re-open it — `queue_proposal` reads
# the resolve DECISION (latest_decision.verb in RESOLVE_VERBS), not a stored status.
_, written = garden.queue_proposal(retire_desc, cluster_leader="c-r", stakes=0.80, root=R, run_id=RUN, model="test")
assert written is False, "re-queuing a rejected op writes NOTHING (the gardener remembers the dismissal)"
assert blobstore.latest_decision(rpid, R)["verb"] == "reject_proposal", "the reject stands, not re-opened to open"
assert rpid not in queue_ids(), "a re-proposed identical op does NOT re-surface — rejection is remembered (L2 close)"
print("OK 3 — reject_proposal leaves the concept graph UNCHANGED, drops the proposal from pending, and "
      "suppresses re-surfacing — a re-proposed identical op stays rejected (the L2 feedback closing).")


# === 4. suppression is SPECIFIC: accepted is remembered too; a never-decided op queues normally =========

# re-queuing the ACCEPTED merge is suppressed too (already applied — don't re-open as pending).
_, wa = garden.queue_proposal(merge_desc, cluster_leader="c-a", stakes=0.85, root=R, run_id=RUN, model="test")
assert wa is False and blobstore.latest_decision(pid, R)["verb"] == "accept_proposal", \
    "re-queuing an accepted op is suppressed (the gardener remembers the verdict — decision-sourced)"
# a BRAND-NEW op (never decided) queues normally — suppression is specific to resolved proposals.
ch_n1, mk_n1 = cleaned("n1")
ch_n2, mk_n2 = cleaned("n2")
mint("c-n1", "fresh one", [ev_for("ev-n1", ch_n1, mk_n1)])
mint("c-n2", "fresh two", [ev_for("ev-n2", ch_n2, mk_n2)])
new_desc = {"op": "merge", "params": {"winner_id": "c-n1", "loser_ids": ["c-n2"]},
            "concept_ids": ["c-n1", "c-n2"], "rationale": "a never-decided merge"}
_, wn = garden.queue_proposal(new_desc, cluster_leader="c-n1", stakes=0.85, root=R, run_id=RUN, model="test")
assert wn is True and garden.mint_proposal_id(new_desc) in queue_ids(), \
    "a never-decided op queues normally (suppression fires ONLY on a rejected/accepted verdict)"
print("OK 4 — suppression is specific to resolved proposals: an accepted op is remembered too, a "
      "never-decided op queues normally.")

print("\nall review-proposals tests passed.")
