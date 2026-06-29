"""concept_digest tests (4a, ADR-0018): fabricate a small concept graph with KNOWN provenance (so the
concepts cluster), managed TAGS (3b), and asserted EDGES (3c — generalizes/relates-to), then assert
`concepts.concept_digest` renders the STRUCTURE (cluster grouping, tags, relations), is BOUNDED (a small
budget keeps the most-entrenched + emits the `…(+N more)` marker), and the empty store yields the sentinel.
This is the structured replacement for dream's flat `_render_concepts`.

Run: `python tests/test_concept_digest.py` (throwaway dir)."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-digest-")

from ratchet import blobstore, concepts, config, garden, weave  # noqa: E402

R = config.ensure_layout()


# --- synthetic transcripts (one record per content block, like Claude Code writes) ----------------

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


def edit_session(sid, path):
    """One assistant turn that Edits `path` — the provenance facet the cluster keys on."""
    return [rec(f"{sid}-u", None, "user", message={"role": "user", "content": "edit"}),
            rec(f"{sid}-1", f"{sid}-u", "assistant",
                message=amsg(f"{sid}M", tool_use(f"{sid}t", "Edit", file_path=path,
                                                 old_string="x", new_string="y")))]

def add_session(records, *, project, sid, mtime):
    raw_h, _ = blobstore.ingest(jsonl(records), source_kind="transcript", source_id=sid,
                                origin_ref={"project": project, "session_id": sid, "mtime": mtime,
                                            "path": f"/store/{project}/{sid}.jsonl"},
                                fetched_at=mtime, root=R)
    cleaned_h, _, _ = weave.materialize(raw_h, root=R)
    return cleaned_h

def mint_concept(cid, title, statement, cleaned_hashes):
    evidence = [{"event_id": f"e-{cid}-{i}", "cleaned_hash": ch, "byte_start": 0, "byte_end": 1,
                 "quote": "q", "context": "q"} for i, ch in enumerate(cleaned_hashes)]
    concept = {"id": cid, "title": title, "statement": statement,
               "evidence": evidence, "source_takeaway": f"t-{cid}"}
    blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=cid,
                     origin_ref={"stage": "test"}, root=R)


# Three alpha sessions all Edit /repo/foo.py (a shared FILE clears the cluster bar), one beta session is the
# disjoint outlier a month later → its own cluster.
T1 = "2026-06-01T10:00:00+00:00"
T2 = "2026-06-02T10:00:00+00:00"
T3 = "2026-07-01T10:00:00+00:00"
ch1 = add_session(edit_session("s1", "/repo/foo.py"), project="alpha", sid="s1", mtime=T1)
ch2 = add_session(edit_session("s2", "/repo/foo.py"), project="alpha", sid="s2", mtime=T2)
ch3 = add_session(edit_session("s3", "/other/baz.py"), project="beta", sid="s3", mtime=T3)

# c-a spans TWO alpha sessions (the MOST entrenched); c-b/c-c one alpha session each (same file → all three
# cluster); c-d is the disjoint beta outlier (its own cluster), one session.
mint_concept("c-a", "foo span", "edits to foo across two sessions", [ch1, ch2])
mint_concept("c-b", "foo one", "a single foo edit", [ch1])
mint_concept("c-c", "foo two", "another foo edit", [ch2])
mint_concept("c-d", "beta baz", "the disjoint beta lesson", [ch3])

# managed tags (3b) on the most-entrenched concept; asserted edges (3c) — the hierarchy + an association.
garden.assign_tags("c-a", ["editing", "version-control"], "fp", R, run_id="t", model="test")
garden.assert_edge("c-a", "generalizes", "c-b", note="foo span covers the single edit", root=R, run_id="t")
garden.assert_edge("c-a", "relates-to", "c-c", note="same file", root=R, run_id="t")


# === 1. the FULL digest: cluster grouping + tags + asserted relations all rendered ==================

d = concepts.concept_digest(R)
assert "[c-a] cluster" in d, f"grouped by the facet cluster (c-a leads the alpha cluster):\n{d}"
assert "c-a: foo span — edits to foo across two sessions" in d, d
assert "tags: editing, version-control" in d, f"the managed tags are shown:\n{d}"
assert "generalizes → c-b" in d, f"the hierarchy spine appears on the parent's line:\n{d}"
assert "relates-to → c-c (same file)" in d, f"an association with its note is shown:\n{d}"
# the three alpha concepts share one cluster header; the disjoint beta concept stands in its own.
assert d.count("] cluster (") == 2, f"two clusters (alpha trio + beta outlier):\n{d}"
assert "[c-d] cluster" in d and "c-d: beta baz" in d, d
assert concepts.DIGEST_EMPTY not in d, "a non-empty store never renders the sentinel"


# === 2. BOUNDED: a small budget keeps the most-entrenched + emits the +N marker =====================

d2 = concepts.concept_digest(R, budget=2)
assert "c-a:" in d2 and "c-b:" in d2, f"the two most-entrenched concepts survive budget=2:\n{d2}"
assert "c-c:" not in d2 and "c-d:" not in d2, f"the single-session tail is dropped first:\n{d2}"
assert "…(+2 more" in d2, f"the marker tells the model the view is PARTIAL, not that those vanished:\n{d2}"

# budget=1 keeps ONLY c-a (sessions=2 outranks every single-session concept); the rest are the +N tail.
d1 = concepts.concept_digest(R, budget=1)
assert "c-a:" in d1 and "c-b:" not in d1 and "…(+3 more" in d1, d1

# budget<=0 disables the cap — everything renders, no truncation marker.
dall = concepts.concept_digest(R, budget=0)
assert all(f"{c}:" in dall for c in ("c-a", "c-b", "c-c", "c-d")) and "more" not in dall, dall


# === 3. the empty store → the clear sentinel ========================================================

empty = config.ensure_layout(Path(tempfile.mkdtemp(prefix="ratchet-test-digest-empty-")))
assert concepts.concept_digest(empty) == "(no concepts yet — treat everything as new)", \
    "an empty concept set yields the sentinel so a stage never relates against nothing"

print("test_concept_digest: all assertions passed")
