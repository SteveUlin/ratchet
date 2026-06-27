"""review tests: the backend is pure and deterministic (Claude lives in the SKILL, not here), so the
suite runs offline. Load-bearing checks: the queue is a derived query over references (no stored
list); evidence re-resolves + RE-VALIDATES against the immutable blobs ("verified real"); accept mints
a concept and CLOSES THE LOOP (dream.load_concepts then sees it); decisions are append-only and drive
every state transition (accept/reject/snooze/retire), latest decision wins; edit captures before/after;
retire drops a concept from the valid set. Run: `python tests/test_review.py`."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-review-")

from ratchet import blobstore, chunk, config, dream, glean, review  # noqa: E402
from ratchet.completer import Completion  # noqa: E402

ROOT = config.ensure_layout()

JJ = "always commit with jj and never use git for version control"
NIX = "run python only through nix develop because python3 is not on the path"


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}

def make_session(sid, line):
    records = [rec("u0", None, "user", message={"role": "user", "content": f"session {sid} kickoff"})]
    parent = "u0"
    for i in range(4):
        body = f"step {i}: " + ("λ wörk ✓ " * 20)
        if i == 2:
            body = f"step 2: {line} — " + ("λ wörk ✓ " * 20)
        records.append(rec(f"{sid}a{i}", parent, "assistant", message=amsg(f"{sid}M{i}", body)))
        parent = f"{sid}a{i}"
    blob = "\n".join(json.dumps(r) for r in records) + "\n"
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid, origin_ref={"session_id": sid})
    cs, _, _ = chunk.materialize(raw_h, budget=600)
    return cs


class GleanFake:
    def __call__(self, system, user):
        cands = [{"quote": ln, "summary": f"sum: {ln[:20]}", "markers": {"insight": 0.7}, "confidence": 0.85}
                 for ln in (JJ, NIX)]
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class DreamFake:
    def __init__(self, *, relation=None, title="A durable theme", why="The principle."):
        self.relation, self.title, self.why = relation, title, why

    def __call__(self, system, user):
        ids = re.findall(r"- id (\w+):", user)
        obj = {"title": self.title, "why": self.why, "cites": ids, "confidence": 0.8}
        if self.relation is not None:
            obj["relation"] = self.relation
        return Completion(text=json.dumps(obj), model="fake", cost_usd=0.01)


def seed_takeaway(*, id, title, why, relation, evidence, support):
    rec = {"id": id, "cluster_signature": id, "title": title, "why": why, "relation": relation,
           "member_events": [], "cites": [], "evidence": evidence, "support": support,
           "markers": {}, "confidence": 0.8, "supersedes": []}
    blobstore.ingest(blobstore.canonical_json(rec), source_kind="takeaway", source_id=id,
                     origin_ref={"stage": "dream", "model": "seed"})
    return id


# Build real takeaways: JJ (2 sessions) + NIX (1).
for sid, line in [("rev-s1", JJ), ("rev-s2", JJ), ("rev-s3", NIX)]:
    glean.run([make_session(sid, line)], GleanFake(), model="fake")
rep = dream.run(DreamFake(title="jj over git", why="sulin uses jj; git annoys him."), model="fake")
assert rep.takeaways, "dream produced takeaways to review"
jj_tk = [t for t in rep.takeaways if t["support"]["events"] == 2][0]
nix_tk = [t for t in rep.takeaways if t["support"]["events"] == 1][0]


# --- 1. the queue is a derived query; evidence is re-resolved + RE-VALIDATED ("verified real") ---

q = review.pending(ROOT)
assert {t["takeaway_id"] for t in q} == {jj_tk["id"], nix_tk["id"]}, "queue = current takeaways with no decision"
jj_q = [t for t in q if t["takeaway_id"] == jj_tk["id"]][0]
assert jj_q["title"] == "jj over git" and jj_q["support"]["events"] == 2
assert jj_q["evidence"] and all(e["verified"] for e in jj_q["evidence"]), "evidence is resolved + verified"
for e in jj_q["evidence"]:
    data = blobstore.get(e["cleaned_hash"]).encode("utf-8")
    assert data[e["byte_start"]:e["byte_end"]].decode() == e["quote"] == JJ, "the verified quote resolves to real bytes"
    assert "context" in e and JJ in e["context"], "a surrounding-context window is provided for the deep path"
print("OK — queue: a derived query over references; evidence re-resolved + re-validated against immutable blobs.")


# --- 2. accept mints a concept and CLOSES THE LOOP (dream.load_concepts then sees it) ------------

assert dream.load_concepts(ROOT) == [], "no concepts before any accept"
cid = review.accept(jj_tk["id"], assessment="why follows the 2 cited quotes", note="clear accept")
assert cid.startswith("c-")
concepts = dream.load_concepts(ROOT)
assert [c["id"] for c in concepts] == [cid], "the accepted takeaway is now a concept dream reads (loop closed)"
c = concepts[0]
assert c["title"] == "jj over git" and c["statement"] == "sulin uses jj; git annoys him.", "concept carries the synthesis"
# the concept's evidence must itself RE-RESOLVE to real bytes (the trust chain reaches the concept,
# not just "the blob exists") — accept stores only spans that re-validated at decision time
ce = c["evidence"][0]
assert blobstore.get(ce["cleaned_hash"]).encode("utf-8")[ce["byte_start"]:ce["byte_end"]].decode() == JJ, \
    "the concept's stored evidence span resolves to the verbatim quote"
assert jj_tk["id"] not in {t["takeaway_id"] for t in review.pending(ROOT)}, "an accepted takeaway leaves the queue"
# the accept decision references both the takeaway and the minted concept
d = blobstore.latest_decision(jj_tk["id"], ROOT)
assert d["verb"] == "accept" and d["concept"] == cid and d["assessment"].startswith("why follows")
print("OK — accept: takeaway → concept blob; dream.load_concepts sees it (loop closed); decision records provenance.")


# --- 3. accept of a strengthens/refines takeaway UPDATES the named concept (new version, same id) -

before_h = blobstore.latest_version(cid, ROOT)
seed_takeaway(id="grow-jj", title="jj over git (more)", why="more evidence sulin uses jj.",
              relation={"kind": "strengthens", "concept_id": cid}, evidence=jj_tk["evidence"],
              support={"events": 3, "sessions": 3})
cid2 = review.accept("grow-jj")
assert cid2 == cid, "a strengthens accept reuses the concept id from the relation (does not mint a new one)"
assert blobstore.latest_version(cid, ROOT) != before_h, "the concept gained a NEW version (refinement)"
assert dream.load_concepts(ROOT)[0]["statement"] == "more evidence sulin uses jj.", "latest version wins"
assert len([c for c in dream.load_concepts(ROOT) if c["id"] == cid]) == 1, "still one valid concept per id"
print("OK — accept (strengthens): updates the named concept as a new version; latest wins; one per id.")


# --- 4. reject removes from the queue; the decision is a label (negative signal) -----------------

review.reject(nix_tk["id"], reason="not durable", assessment="one-off")
assert nix_tk["id"] not in {t["takeaway_id"] for t in review.pending(ROOT)}, "a rejected takeaway leaves the queue"
assert blobstore.latest_decision(nix_tk["id"], ROOT)["verb"] == "reject"
print("OK — reject: leaves the queue; persisted as a label for future negative few-shot.")


# --- 5. snooze defers until a concrete time; an unparseable trigger is refused (no graveyard) ----

sn = seed_takeaway(id="snoozeme", title="maybe", why="weak so far.", relation={"kind": "new", "concept_id": None},
                   evidence=jj_tk["evidence"], support={"events": 1, "sessions": 1})
review.snooze(sn, until="2999-01-01T00:00:00+00:00")
assert sn not in {t["takeaway_id"] for t in review.pending(ROOT)}, "a future-snoozed takeaway is out of the queue"
review.snooze(sn, until="2000-01-01T00:00:00+00:00")        # a newer decision whose time has passed
assert sn in {t["takeaway_id"] for t in review.pending(ROOT)}, "a due snooze re-surfaces (latest decision wins)"
for bad in (None, "", "next week", "2026-13-99"):           # an unparseable/empty trigger would be a graveyard
    try:
        review.snooze(sn, until=bad)
        assert False, f"snooze --until {bad!r} must be refused"
    except ValueError:
        pass
print("OK — snooze: time trigger defers + re-surfaces (latest wins); an unparseable --until is refused.")


# --- 6. edit captures before/after; the concept uses the corrected content -----------------------

ed = seed_takeaway(id="editme", title="loose claim", why="overgeneralized.",
                   relation={"kind": "new", "concept_id": None}, evidence=jj_tk["evidence"],
                   support={"events": 2, "sessions": 2})
ecid = review.accept(ed, edited={"title": "tight claim", "why": "narrowed to what the evidence supports."},
                     assessment="why overreached; narrowed it")
econcept = [c for c in dream.load_concepts(ROOT) if c["id"] == ecid][0]
assert econcept["title"] == "tight claim" and econcept["statement"].startswith("narrowed"), "concept uses the edit"
edec = blobstore.latest_decision(ed, ROOT)
assert edec["verb"] == "edit" and edec["edited"]["before"]["title"] == "loose claim", "edit captures before/after"
print("OK — edit: the corrected synthesis becomes the concept; before/after captured as the correction signal.")


# --- 7. retire drops a concept from the valid set (not a deletion) -------------------------------

assert cid in {c["id"] for c in dream.load_concepts(ROOT)}, "concept is valid before retire"
review.retire(cid, reason="superseded by a clearer concept")
assert cid not in {c["id"] for c in dream.load_concepts(ROOT)}, "a retired concept leaves the valid set"
assert blobstore.has(blobstore.latest_version(cid, ROOT), ROOT), "but the concept blob + history remain (no deletion)"
print("OK — retire: concept leaves the valid set via the latest decision; the immutable history is kept.")


# --- 8. evidence hardening: a malformed/foreign span is never shown 'verified' ------------------

bad = seed_takeaway(id="badspan", title="x", why="y", relation={"kind": "new", "concept_id": None},
                    evidence=[{"event_id": "z", "cleaned_hash": jj_tk["evidence"][0]["cleaned_hash"],
                               "byte_start": None, "byte_end": 999999}], support={"events": 1, "sessions": 1})
present = review.context_for(bad, ROOT)
assert present["evidence"] == [], "a null/overshoot span resolves to nothing — never the whole blob as 'verified'"
print("OK — evidence: malformed/foreign spans rejected at the review read boundary (trust chain intact).")


# --- 9. regression (review-battery fixes) -------------------------------------------------------

# (a) accept re-validates evidence into the CONCEPT — a malformed span is filtered out, not baked in.
real_ev = jj_tk["evidence"][0]
mix = seed_takeaway(id="mixev", title="mixed", why="one good, one bad span.",
                    relation={"kind": "new", "concept_id": None},
                    evidence=[real_ev, {"event_id": "bad", "cleaned_hash": real_ev["cleaned_hash"],
                                        "byte_start": None, "byte_end": 999999}],
                    support={"events": 1, "sessions": 1})
mcid = review.accept(mix, assessment="kept the verifiable span")
mconcept = [c for c in dream.load_concepts(ROOT) if c["id"] == mcid][0]
assert len(mconcept["evidence"]) == 1, "the malformed span is filtered OUT of the concept (only verified spans)"
me = mconcept["evidence"][0]
assert blobstore.get(me["cleaned_hash"]).encode("utf-8")[me["byte_start"]:me["byte_end"]].decode() == JJ

# (b) a takeaway with NO resolvable evidence is refused (a belief with no anchor) unless overridden.
noev = seed_takeaway(id="noev", title="floats", why="no quotes back this.",
                     relation={"kind": "new", "concept_id": None}, evidence=[], support={"events": 1, "sessions": 1})
try:
    review.accept(noev)
    assert False, "accept of a no-evidence takeaway must be refused"
except ValueError:
    pass
assert review.accept(noev, allow_no_evidence=True), "the override mints it deliberately"

# (c) a strengthens/refines pointing at a NON-EXISTENT concept id mints fresh (no phantom concept).
ph = seed_takeaway(id="phantom", title="p", why="claims to strengthen a ghost.",
                   relation={"kind": "strengthens", "concept_id": "c-doesnotexist"},
                   evidence=jj_tk["evidence"], support={"events": 1, "sessions": 1})
pcid = review.accept(ph)
assert pcid != "c-doesnotexist", "a stale relation.concept_id is ignored — a fresh concept is minted"

# (d) THE RESURRECTION BUG: a dream 'processed' marker shares the takeaway's id (= cluster_signature);
#     a later marker must NOT shadow the accept and bring the takeaway back into the queue.
acc = seed_takeaway(id="resurrect", title="r", why="accept me.", relation={"kind": "new", "concept_id": None},
                    evidence=jj_tk["evidence"], support={"events": 1, "sessions": 1})
review.accept(acc, assessment="ok")
assert acc not in {t["takeaway_id"] for t in review.pending(ROOT)}, "accepted → out of the queue"
# simulate a later dream run writing a processed marker targeting the same id (newer fetched_at)
import time as _t
blobstore.ingest(blobstore.canonical_json({"verb": "processed", "target": acc, "stage": "dream", "x": 1}),
                 source_kind="decision", source_id="proc-resurrect", prev=None,
                 origin_ref={"stage": "dream"}, fetched_at=config.now())
assert acc not in {t["takeaway_id"] for t in review.pending(ROOT)}, \
    "a later 'processed' marker must NOT resurrect an accepted takeaway (review fold excludes producer markers)"
print("OK — regression: accept re-validates evidence into the concept; no-evidence refused; stale concept_id")
print("     mints fresh; producer 'processed' markers can't resurrect a reviewed takeaway.")

print("\nall review tests passed.")
