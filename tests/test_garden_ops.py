"""garden structural-op tests (3c-i, ADR-0015): the DETERMINISTIC gardener machinery — NO LLM. Fabricate
concepts directly over real cleaned blobs (so evidence spans re-validate), call EACH op, and assert the
APPEND-ONLY effects. The load-bearing checks, all on the blob model (state = a fold over append-only
blobs, never a flipped field):

  ASSERTED EDGES — `assert_edge`/`retract_edge` write `concept_edge` blobs keyed on `src|kind|dst`,
    latest-wins; `asserted_edges` folds the live (active) set; retract = a new version with active:false
    (invalidate-don't-delete). `concept_graph` folds them in alongside the derived edges (3a); a
    `generalizes` edge defines the hierarchy spine (`concept_hierarchy`).
  MERGE — a WINNER concept VERSION unions the losers' evidence (re-validated, deduped — a BAD span is
    dropped on write); each loser is INVALIDATED-NOT-DELETED (its blob still retrievable, absent from
    `valid_concepts`); a `supersedes` edge loser→winner records the lineage; a loser's relational edge is
    CARRIED to the winner. Re-running is a byte-identical no-op (idempotent).
  SPLIT — parts minted with SUBSET evidence; the original invalidated-not-deleted; `supersedes` edges
    original→part. ABSTRACT — a parent minted (UNION evidence); `generalizes` edges to children;
    children STAY valid. REPARENT — the old `generalizes` edge retracted (active:false), the new one
    active; re-running is a no-op. RETIRE — dropped from `valid_concepts`, blob retained (review.retire).
  TAG CURATION — `merge_tags` redirects a loser slug to its winner, `retire_tag` drops one; the
    `vocabulary` fold reflects it and `concept_tags` re-points/drops at READ — NO concept/tag blob rewritten.
  TRUST CHAIN — every minted/versioned concept's evidence RE-VALIDATES (review.resolve_evidence). STAKES —
    `op_stakes` is a fuzzy gradient: concept-altering ops outrank edge-only/tag-curation ones.

Run: `python tests/test_garden_ops.py` (throwaway dir)."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-garden-ops-")

from ratchet import blobstore, concepts, config, dream, garden, review, weave  # noqa: E402

R = config.ensure_layout()
RUN = "op-test"


# --- synthetic transcripts → cleaned blobs → concepts (mirrors test_garden's harness) -------------

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
                                                          old_string="a", new_string="b")))]
    raw_h, _ = blobstore.ingest(jsonl(recs), source_kind="transcript", source_id=f"sess-{tag}",
                                origin_ref={"project": f"proj-{tag}", "session_id": f"sess-{tag}",
                                            "mtime": "2026-06-01T10:00:00+00:00"},
                                fetched_at="2026-06-01T10:00:00+00:00", root=R)
    ch, _, _ = weave.materialize(raw_h, root=R)
    return ch


def ev_ptr(event_id, cleaned_hash, *, start=0, end=1):
    """One evidence pointer in the concept/takeaway shape — span (start,end) anchored into a cleaned blob."""
    return {"event_id": event_id, "cleaned_hash": cleaned_hash, "byte_start": start, "byte_end": end,
            "quote": "q", "context": "q"}


def mint(cid, title, evidence):
    """Mint a concept blob directly (the review.accept shape), citing the given evidence pointers."""
    concept = {"id": cid, "title": title, "statement": f"the {title} lesson",
               "evidence": evidence, "source_takeaway": f"t-{cid}"}
    blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=cid,
                     origin_ref={"stage": "test"}, root=R)


def valid_ids():
    return {c["id"] for c in dream.load_concepts(R)}

def concept_evidence(cid):
    """The cleaned_hashes a (latest version of a) concept currently cites — a set, for union/subset checks."""
    obj = garden._concept_blob(cid, R)
    return {e["cleaned_hash"] for e in (obj.get("evidence") or [])} if obj else set()

def active_edge(src, kind, dst):
    return any(e["src"] == src and e["kind"] == kind and e["dst"] == dst for e in garden.asserted_edges(R))

def trust_chain_holds(cid):
    """Every evidence pointer of a concept re-validates against its immutable blob (review's verified gate)."""
    obj = garden._concept_blob(cid, R)
    resolved = review.resolve_evidence({"evidence": obj.get("evidence") or []}, R)
    return len(resolved) == len(obj.get("evidence") or []) and all(e["verified"] for e in resolved)


# === 0. asserted edges: write, fold, retract = active:false (NO deletion) ===========================

ch_e1, ch_e2 = cleaned("e1"), cleaned("e2")
mint("c-ed-a", "edge a", [ev_ptr("ev-a", ch_e1)])
mint("c-ed-b", "edge b", [ev_ptr("ev-b", ch_e2)])

assert garden.edge_id("c-ed-a", "relates-to", "c-ed-b") == "c-ed-a|relates-to|c-ed-b", "edge identity"
_, written = garden.assert_edge("c-ed-a", "relates-to", "c-ed-b", run_id=RUN)
assert written and active_edge("c-ed-a", "relates-to", "c-ed-b"), "an asserted edge folds into the live set"
# re-asserting the identical edge is a byte-identical no-op (content run-invariant → idempotent).
_, written2 = garden.assert_edge("c-ed-a", "relates-to", "c-ed-b", run_id="other-run")
assert not written2, "re-asserting an identical edge is a no-op (the live set never churns)"
# retract = a NEW version with active:false — the blob is RETAINED (still foldable with active_only=False).
garden.retract_edge("c-ed-a", "relates-to", "c-ed-b", run_id=RUN)
assert not active_edge("c-ed-a", "relates-to", "c-ed-b"), "a retracted edge leaves the live set"
allv = garden.asserted_edges(R, active_only=False)
assert any(e["src"] == "c-ed-a" and e["dst"] == "c-ed-b" and not e["active"] for e in allv), \
    "retract is invalidate-don't-delete — the edge blob is retained, just inactive"
print("OK §0 — asserted edges fold latest-wins; retract is a new active:false version, never a deletion.")


# === 1. MERGE: winner version unions evidence; losers invalidated-not-deleted; supersedes + carry ===

ch_w, ch_l1, ch_l2 = cleaned("w"), cleaned("l1"), cleaned("l2")
mint("c-win", "winner", [ev_ptr("ev-w", ch_w)])
mint("c-lose1", "loser one", [ev_ptr("ev-l1", ch_l1)])
# loser2 cites a VALID span AND a malformed one (out-of-bounds) — the bad span must be DROPPED on write.
mint("c-lose2", "loser two", [ev_ptr("ev-l2", ch_l2), ev_ptr("ev-bad", ch_l2, start=0, end=999999)])
mint("c-other", "a relative", [ev_ptr("ev-o", ch_e1)])

# a loser's relational edge must be CARRIED to the winner (carry-don't-drop), not lost in the merge.
garden.assert_edge("c-lose1", "relates-to", "c-other", run_id=RUN)

garden.merge(["c-lose1", "c-lose2"], "c-win", run_id=RUN)

# the winner VERSION unions the losers' evidence; the malformed loser span was re-validated away.
assert concept_evidence("c-win") == {ch_w, ch_l1, ch_l2}, \
    f"winner unions evidence (bad span dropped on re-validation): {concept_evidence('c-win')}"
assert trust_chain_holds("c-win"), "the merged winner's evidence re-validates — the trust chain reaches it"
# each loser is INVALIDATED-NOT-DELETED: blob still retrievable, but absent from valid_concepts.
for lid in ("c-lose1", "c-lose2"):
    assert lid not in valid_ids(), f"{lid} dropped from valid_concepts by its supersede decision"
    h = blobstore.latest_version(lid, R)
    assert h and json.loads(blobstore.get(h, R))["id"] == lid, f"{lid}'s blob is RETAINED (not deleted)"
# a supersedes edge loser→winner records the lineage; the carried relation now points winner→c-other.
assert active_edge("c-lose1", "supersedes", "c-win") and active_edge("c-lose2", "supersedes", "c-win"), \
    "each loser→winner supersedes edge is asserted"
assert active_edge("c-win", "relates-to", "c-other"), "the loser's relation was CARRIED to the winner"
assert not active_edge("c-lose1", "relates-to", "c-other"), "the loser's original edge was retracted"
assert "c-win" in valid_ids(), "the winner stays valid"

# IDEMPOTENT: re-running merge re-ingests the byte-identical winner version (no churn) — same latest hash.
win_hash = blobstore.latest_version("c-win", R)
garden.merge(["c-lose1", "c-lose2"], "c-win", run_id="rerun")
assert blobstore.latest_version("c-win", R) == win_hash, "re-running merge is a byte-identical no-op"
assert "c-lose1" not in valid_ids() and "c-lose2" not in valid_ids(), "losers stay invalidated on re-run"
print("OK §1 — merge unions+re-validates evidence, invalidates-not-deletes losers, supersedes+carries edges, idempotent.")


# === 2. SPLIT: parts carry SUBSET evidence; original invalidated-not-deleted; supersedes edges ======

ch_s1, ch_s2, ch_s3 = cleaned("s1"), cleaned("s2"), cleaned("s3")
mint("c-split", "splittable", [ev_ptr("ev-s1", ch_s1), ev_ptr("ev-s2", ch_s2), ev_ptr("ev-s3", ch_s3)])
orig_ev = garden._concept_blob("c-split", R)["evidence"]
parts = [{"title": "part one", "statement": "first half", "evidence": [orig_ev[0]]},          # subset {s1}
         {"title": "part two", "statement": "second half", "evidence": orig_ev[1:]}]           # subset {s2,s3}

new_ids = garden.split("c-split", parts, run_id=RUN)
assert len(new_ids) == 2 and new_ids == sorted(set(new_ids), key=new_ids.index), "two distinct parts minted"
assert "c-split" not in valid_ids(), "the original is dropped from valid_concepts by its split decision"
h = blobstore.latest_version("c-split", R)
assert h and json.loads(blobstore.get(h, R))["id"] == "c-split", "the original blob is RETAINED (not deleted)"
assert concept_evidence(new_ids[0]) == {ch_s1}, f"part one carries the {{s1}} subset: {concept_evidence(new_ids[0])}"
assert concept_evidence(new_ids[1]) == {ch_s2, ch_s3}, f"part two carries the {{s2,s3}} subset"
for pid in new_ids:
    assert pid in valid_ids(), f"the minted part {pid} is valid"
    assert active_edge("c-split", "supersedes", pid), f"a supersedes edge original→{pid} records the lineage"
    assert trust_chain_holds(pid), f"part {pid} evidence re-validates — the trust chain reaches each part"
# determinism: the part ids are a deterministic function of the op inputs (incl. the part INDEX —
# resumable crash-retry re-mints the same ids).
assert garden._mint_op_concept_id("c-split|split|0|part one") == new_ids[0], "part ids are deterministic"
assert garden._mint_op_concept_id("c-split|split|1|part two") == new_ids[1], "the index folds into the id"
# safety: re-splitting a now-INVALID original is refused cleanly (no double-split, state unchanged).
try:
    garden.split("c-split", parts, run_id="rerun")
    raise AssertionError("re-splitting an invalidated concept must be refused")
except ValueError:
    pass
print("OK §2 — split mints subset-evidence parts, invalidates-not-deletes the original, links lineage, deterministic.")


# === 3. ABSTRACT: parent minted (UNION evidence); generalizes edges; children STAY valid =============

ch_a1, ch_a2 = cleaned("a1"), cleaned("a2")
mint("c-child-a", "child a", [ev_ptr("ev-a1", ch_a1)])
mint("c-child-b", "child b", [ev_ptr("ev-a2", ch_a2)])

parent = garden.abstract(["c-child-a", "c-child-b"], "the parent", "generalizes both children", run_id=RUN)
assert parent in valid_ids(), "the abstracted parent is a valid concept"
assert "c-child-a" in valid_ids() and "c-child-b" in valid_ids(), "the children STAY valid (abstract adds, never removes)"
assert concept_evidence(parent) == {ch_a1, ch_a2}, f"the parent unions the children's evidence: {concept_evidence(parent)}"
assert trust_chain_holds(parent), "the parent's evidence re-validates — the trust chain reaches it"
assert active_edge(parent, "generalizes", "c-child-a") and active_edge(parent, "generalizes", "c-child-b"), \
    "a generalizes edge parent→child for each child"
assert concepts.concept_hierarchy(R)[parent] == ["c-child-a", "c-child-b"], "the hierarchy spine reflects the abstraction"
print("OK §3 — abstract mints a union-evidence parent with generalizes edges; children stay valid.")


# === 4. REPARENT: old generalizes edge retracted (active:false), new one active; idempotent ==========

ch_r = cleaned("r")
mint("c-rep-child", "reparent child", [ev_ptr("ev-r", ch_r)])
mint("c-rep-old", "old parent", [ev_ptr("ev-ro", ch_a1)])
mint("c-rep-new", "new parent", [ev_ptr("ev-rn", ch_a2)])
garden.assert_edge("c-rep-old", "generalizes", "c-rep-child", run_id=RUN)   # establish the old parent

garden.reparent("c-rep-child", "c-rep-new", run_id=RUN)
assert not active_edge("c-rep-old", "generalizes", "c-rep-child"), "the OLD generalizes edge is retracted"
assert active_edge("c-rep-new", "generalizes", "c-rep-child"), "the NEW generalizes edge is active"
# the old edge blob is RETAINED, just inactive (invalidate-don't-delete).
allv = garden.asserted_edges(R, active_only=False)
assert any(e["src"] == "c-rep-old" and e["dst"] == "c-rep-child" and not e["active"] for e in allv), \
    "the old edge is retained as inactive, not deleted"
hier = concepts.concept_hierarchy(R)
assert hier.get("c-rep-new") == ["c-rep-child"] and "c-rep-old" not in hier, "the spine reflects the reparent"
# IDEMPOTENT: re-running reparent leaves exactly one active parent edge, the old still inactive.
garden.reparent("c-rep-child", "c-rep-new", run_id="rerun")
parents = [e["src"] for e in garden.asserted_edges(R) if e["kind"] == "generalizes" and e["dst"] == "c-rep-child"]
assert parents == ["c-rep-new"], f"re-running reparent keeps exactly one active parent: {parents}"
print("OK §4 — reparent retracts the old generalizes edge and asserts the new; idempotent.")


# === 5. RETIRE: dropped from valid_concepts, blob retained (reuses review.retire) ===================

ch_ret = cleaned("ret")
mint("c-retire", "retire me", [ev_ptr("ev-ret", ch_ret)])
assert "c-retire" in valid_ids(), "the concept starts valid"
garden.retire("c-retire", reason="contradicted", root=R)
assert "c-retire" not in valid_ids(), "retire drops the concept from valid_concepts"
h = blobstore.latest_version("c-retire", R)
assert h and json.loads(blobstore.get(h, R))["id"] == "c-retire", "the retired concept's blob is RETAINED"
print("OK §5 — retire drops the concept from valid_concepts; its blob is retained (invalidate-don't-delete).")


# === 6. TAG CURATION: merge_tags redirects, retire_tag drops — the vocab fold reflects it ============

garden.add_tag("jj", "the jj theme", R, run_id=RUN, model="seed")
garden.add_tag("version-control", "the version-control theme", R, run_id=RUN, model="seed")
garden.add_tag("dead-tag", "a tag to retire", R, run_id=RUN, model="seed")
fp = garden.vocab_fingerprint(garden.vocabulary(R))
garden.assign_tags("c-child-a", ["jj"], fp, R, run_id=RUN, model="seed")              # tagged with the LOSER
garden.assign_tags("c-child-b", ["version-control"], fp, R, run_id=RUN, model="seed") # tagged with the WINNER

assert set(garden.vocabulary(R)) >= {"jj", "version-control", "dead-tag"}, "the three tags are in the vocab"
assert garden.concept_tags("c-child-a", R) == ["jj"], "the concept starts tagged with the loser slug"

# merge_tags(jj → version-control): the loser leaves the vocab; assignments redirect to the winner at READ.
garden.merge_tags("jj", "version-control", root=R, run_id=RUN)
assert "jj" not in garden.vocabulary(R), "the merged loser leaves the controlled vocabulary"
assert "version-control" in garden.vocabulary(R), "the winner stays in the vocabulary"
assert garden.concept_tags("c-child-a", R) == ["version-control"], "the loser-tagged concept now resolves to the winner"
assert garden.all_concept_tags(R)["c-child-a"] == ["version-control"], "the batch fold redirects too"
# the concept's ASSIGNMENT blob was NOT rewritten — the redirect is a fold at read, the blob still says 'jj'.
ah = blobstore.latest_version(garden.ASSIGN_PREFIX + "c-child-a", R)
assert json.loads(blobstore.get(ah, R))["tags"] == ["jj"], "the assignment blob is UNTOUCHED (redirect folds at read)"

# retire_tag(dead-tag): the slug leaves the vocab entirely.
garden.retire_tag("dead-tag", root=R, run_id=RUN)
assert "dead-tag" not in garden.vocabulary(R), "a retired tag leaves the vocabulary"
# the curation is reversible/append-only: the fold records the redirect, no tag blob was deleted.
assert garden.tag_curation(R)["jj"] == "version-control" and garden.tag_curation(R)["dead-tag"] is None, \
    "the tag_curation fold records the merge redirect + the retire (winner None)"
print("OK §6 — merge_tags redirects a slug to its winner, retire_tag drops one; the vocab fold reflects both, no blob rewritten.")


# === 7. concept_graph folds asserted edges + the hierarchy; supersedes-of-invalid drops out =========

graph = concepts.concept_graph(R)
asserted = [e for e in graph["edges"] if e.get("asserted")]
# the abstract/reparent generalizes edges (both endpoints valid) appear in the graph, marked asserted.
assert any(e["source"] == parent and e["target"] == "c-child-a" and e["kind"] == "generalizes" for e in asserted), \
    "concept_graph folds in the asserted generalizes edges"
# a supersedes loser→winner edge does NOT render (the loser is invalid → not a node) — lineage lives in the store.
assert not any(e["kind"] == "supersedes" for e in asserted), \
    "a supersedes edge to an invalidated loser drops out of the live graph (lineage stays in the asserted-edge store)"
assert active_edge("c-lose1", "supersedes", "c-win"), "...but the supersedes edge is still asserted in the store"
# the derived 3a edges still flow (graph is additive over 3a).
assert any("shared" in e for e in graph["edges"]) or all("asserted" in e for e in graph["edges"]), \
    "derived edges keep their `shared` shape alongside asserted ones"
# the trust chain re-validates for EVERY valid concept (no minted/versioned concept floats free of evidence).
assert all(trust_chain_holds(c["id"]) for c in dream.load_concepts(R)), \
    "every valid concept's evidence re-validates against its immutable blobs"
print("OK §7 — concept_graph folds asserted edges + the hierarchy; supersedes-of-invalid drops out; trust chain holds.")


# === 8. op_stakes: the fuzzy gradient concept-altering > edge-only/tag-curation =====================

assert garden.op_stakes("merge") > garden.op_stakes("reparent") > garden.op_stakes("merge_tags"), \
    "concept-altering ops outrank edge-only, which outrank tag-curation"
assert garden.op_stakes("split") > garden.op_stakes("assert_edge"), "split (changes what exists) >> a bare edge"
assert 0.0 <= garden.op_stakes("anything-unknown") <= 1.0, "stakes are clamped to [0,1]"
assert garden.op_stakes("anything-unknown") == 0.5, "an unknown op lands mid-gradient (route to review, fail-safe)"
# breadth nudges a merge up (fuzzy gradient, not a hard line) but stays clamped.
assert garden.op_stakes({"op": "merge", "loser_ids": ["a", "b", "c", "d"]}) > garden.op_stakes("merge"), \
    "a wider merge is a touch higher-stakes"
assert garden.op_stakes({"op": "merge", "loser_ids": ["a"] * 99}) <= 1.0, "breadth stays clamped to 1.0"
print("OK §8 — op_stakes is a clamped fuzzy gradient: concept-altering > edge-only > tag-curation.")


# === 9. ZERO-EVIDENCE FLOOR: merge/split/abstract REFUSE an empty re-validation (allow_no_evidence) ==
# cleaned spans are TTL-eligible, so an unbacked re-validation must not silently become a curated belief.
ch_z = cleaned("z")
def BAD(eid):                              # an out-of-bounds span — re-validates to NOTHING (dropped on write)
    return ev_ptr(eid, ch_z, start=0, end=999999)

# merge: a winner whose evidence re-validates to empty (no losers) is refused; the override versions it.
mint("c-zero-merge", "zero merge", [BAD("ev-zm")])
try:
    garden.merge([], "c-zero-merge", run_id=RUN)
    raise AssertionError("merge of a winner with no re-validating evidence must be refused")
except ValueError:
    pass
garden.merge([], "c-zero-merge", run_id=RUN, allow_no_evidence=True)
assert "c-zero-merge" in valid_ids() and concept_evidence("c-zero-merge") == set(), \
    "allow_no_evidence=True versions the merge winner with empty evidence (the recorded escape hatch)"

# split: a PER-PART floor — a part whose evidence re-validates to empty is refused; a refused split makes
# NO partial mutation (the original stays valid, the decision never written).
mint("c-zero-split", "zero split", [ev_ptr("ev-zs", ch_z, start=0, end=1)])
zsp = [{"title": "p", "statement": "s", "evidence": [BAD("ev-zsp")]}]
try:
    garden.split("c-zero-split", zsp, run_id=RUN)
    raise AssertionError("split part with no re-validating evidence must be refused")
except ValueError:
    pass
assert "c-zero-split" in valid_ids(), "a refused split leaves the original valid (no partial mutation)"
zids = garden.split("c-zero-split", zsp, run_id=RUN, allow_no_evidence=True)
assert len(zids) == 1 and concept_evidence(zids[0]) == set(), "allow_no_evidence=True mints the empty part"

# abstract: a parent whose UNION re-validates to empty is refused; the override mints the empty parent.
mint("c-zero-ca", "zero child a", [BAD("ev-zca")])
mint("c-zero-cb", "zero child b", [BAD("ev-zcb")])
try:
    garden.abstract(["c-zero-ca", "c-zero-cb"], "zero parent", "s", run_id=RUN)
    raise AssertionError("abstract parent with no re-validating evidence must be refused")
except ValueError:
    pass
zp = garden.abstract(["c-zero-ca", "c-zero-cb"], "zero parent", "s", run_id=RUN, allow_no_evidence=True)
assert zp in valid_ids() and concept_evidence(zp) == set(), "allow_no_evidence=True mints the empty parent"
print("OK §9 — merge/split/abstract refuse an empty re-validated evidence pool; allow_no_evidence overrides.")


# === 10. SPLIT part-id collision: same/empty-title parts mint DISTINCT ids (the index disambiguates) ==
ch_c1, ch_c2 = cleaned("col1"), cleaned("col2")
mint("c-collide", "collidable", [ev_ptr("ev-c1", ch_c1), ev_ptr("ev-c2", ch_c2)])
oev = garden._concept_blob("c-collide", R)["evidence"]
# two parts with the SAME (empty) title: without the part INDEX in the mint material the second collides
# onto the first's id and silently overwrites it, losing a part.
cparts = [{"title": "", "statement": "first", "evidence": [oev[0]]},
          {"title": "", "statement": "second", "evidence": [oev[1]]}]
cids = garden.split("c-collide", cparts, run_id=RUN)
assert len(cids) == 2 and cids[0] != cids[1], "same-title parts mint TWO distinct concepts (index disambiguates)"
assert concept_evidence(cids[0]) == {ch_c1} and concept_evidence(cids[1]) == {ch_c2}, \
    "no evidence loss — each same-title part keeps its OWN subset"
print("OK §10 — a split into same/empty-title parts yields two distinct concepts, no evidence loss.")


# === 11. MERGE loser-validity: an already-invalidated loser is SKIPPED (no moved-away evidence revived) =
ch_iv = cleaned("iv")
mint("c-mw", "merge winner 2", [ev_ptr("ev-mw", ch_e1)])
mint("c-invalid-loser", "already gone", [ev_ptr("ev-il", ch_iv)])
garden.retire("c-invalid-loser", reason="gone", root=R)               # the loser is now INVALID
assert "c-invalid-loser" not in valid_ids(), "the loser starts already-invalidated"
garden.merge(["c-invalid-loser"], "c-mw", run_id=RUN)
assert ch_iv not in concept_evidence("c-mw"), \
    "an invalidated loser's evidence is NOT re-introduced (a prior invalidation moved it away on purpose)"
assert not active_edge("c-invalid-loser", "supersedes", "c-mw"), \
    "the invalid loser is skipped entirely — no supersedes edge, no re-invalidation"
print("OK §11 — merge skips an already-invalidated loser, never re-introducing moved-away evidence.")


# === 12. assert_edge REJECTS an unknown kind (fail-safe over the append-only trust-critical store) =====
assert "specializes" not in garden.ASSERTED_EDGE_KINDS, "the dead `specializes` kind is dropped (one canonical direction)"
for bad_kind in ("specializes", "totally-made-up"):
    try:
        garden.assert_edge("c-mw", bad_kind, "c-other", run_id=RUN)
        raise AssertionError(f"assert_edge must reject the unknown kind {bad_kind!r}")
    except ValueError:
        pass
print("OK §12 — assert_edge rejects a kind outside ASSERTED_EDGE_KINDS; the dead `specializes` kind is gone.")

print("test_garden_ops: all assertions passed")
