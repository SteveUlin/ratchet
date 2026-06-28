"""concepts tests: the metadata-facet graph substrate (3a, ADR-0013). Fabricate four sessions with
KNOWN, deliberately-overlapping provenance (projects / files / tools / times), weave them (the cleaned
blobs store NO facets — facets RECOMPUTE from the raw on read), mint concepts directly over those
cleaned blobs, and assert the whole rebuildable graph — per-concept facets, the derived edges (a shared
file → a `shares-file` edge; a disjoint concept → no edge), and the leader clusters — against a committed
golden with a legible diff. Also covers the recompute path (`_cleaned_facets` / `session_facts`:
Edit/Write/MultiEdit/NotebookEdit write files, a Read does not; union across sessions) and pins that no
cleaned/chunkset sidecar stores `session_meta` (the stored-facets puncture is gone).

Run: `python tests/test_concepts.py` (throwaway dir). Regenerate the golden: `RATCHET_REGEN_GOLDEN=1
python tests/test_concepts.py`."""
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-concepts-")

from ratchet import blobstore, chunk, concepts, config, weave  # noqa: E402

R = config.ensure_layout()
GOLDEN = Path(__file__).resolve().parent / "golden" / "concept_graph.json"


# --- synthetic transcripts: one record per content block, like Claude Code writes ----------------

def rec(uuid, parent, typ, **kw):
    r = {"type": typ, "uuid": uuid, "parentUuid": parent}
    r.update(kw)
    return r

def umsg(text):
    return {"role": "user", "content": text}

def amsg(mid, *blocks):
    return {"role": "assistant", "id": mid, "content": list(blocks)}

def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def jsonl(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def add_session(records, *, project, sid, mtime):
    """Ingest the transcript as a raw blob with a KNOWN origin (project/session/mtime) and weave it. The
    cleaned sidecar stores NO facets; return the cleaned hash + the facets `_cleaned_facets` RECOMPUTES
    from the raw (one derived_from hop → origin_ref + a re-parse — the per-session checks below read these)."""
    raw_h, _ = blobstore.ingest(jsonl(records), source_kind="transcript", source_id=sid,
                                origin_ref={"project": project, "session_id": sid, "mtime": mtime,
                                            "path": f"/store/{project}/{sid}.jsonl"},
                                fetched_at=mtime, root=R)
    cleaned_h, _, _ = weave.materialize(raw_h, root=R)
    return cleaned_h, concepts._cleaned_facets(cleaned_h, R)


def mint_concept(cid, title, cleaned_hashes):
    """Mint a concept blob directly (simpler than driving glean→dream→accept), citing each cleaned blob
    as one evidence pointer — exactly the `cleaned_hash` the facet walk reads."""
    evidence = [{"event_id": f"e-{cid}-{i}", "cleaned_hash": ch, "byte_start": 0, "byte_end": 1,
                 "quote": "q", "context": "q"} for i, ch in enumerate(cleaned_hashes)]
    concept = {"id": cid, "title": title, "statement": f"the {title} lesson",
               "evidence": evidence, "source_takeaway": f"t-{cid}"}
    blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=cid,
                     origin_ref={"stage": "test"}, root=R)


# Four sessions: alpha shares files/tools/time; beta is the disjoint outlier a month later.
T_A = "2026-06-01T10:00:00+00:00"
T_B = "2026-06-01T11:00:00+00:00"
T_D = "2026-06-01T12:00:00+00:00"
T_C = "2026-07-01T10:00:00+00:00"

SESS_A = [  # alpha: Edit foo.py, Read readonly.py (NOT an edit), Bash
    rec("a-u", None, "user", message=umsg("edit foo")),
    rec("a-1", "a-u", "assistant", message=amsg("A1", tool_use("at1", "Edit", file_path="/repo/foo.py",
                                                               old_string="x", new_string="y"))),
    rec("a-2", "a-1", "assistant", message=amsg("A2", tool_use("at2", "Read", file_path="/repo/readonly.py"))),
    rec("a-3", "a-2", "assistant", message=amsg("A3", tool_use("at3", "Bash", command="ls"))),
]
SESS_B = [  # alpha: Edit foo.py (shared with A) + bar.py, Bash
    rec("b-u", None, "user", message=umsg("edit foo and bar")),
    rec("b-1", "b-u", "assistant", message=amsg("B1", tool_use("bt1", "Edit", file_path="/repo/foo.py",
                                                               old_string="x", new_string="z"))),
    rec("b-2", "b-1", "assistant", message=amsg("B2", tool_use("bt2", "Edit", file_path="/repo/bar.py",
                                                               old_string="p", new_string="q"))),
    rec("b-3", "b-2", "assistant", message=amsg("B3", tool_use("bt3", "Bash", command="jj st"))),
]
SESS_C = [  # beta: Write baz.py, Grep — disjoint repo/files/tools, far in time
    rec("c-u", None, "user", message=umsg("write baz")),
    rec("c-1", "c-u", "assistant", message=amsg("C1", tool_use("ct1", "Write", file_path="/other/baz.py",
                                                               content="hi"))),
    rec("c-2", "c-1", "assistant", message=amsg("C2", tool_use("ct2", "Grep", pattern="TODO"))),
]
SESS_D = [  # alpha: MultiEdit bar.py (shared with B), Bash
    rec("d-u", None, "user", message=umsg("multiedit bar")),
    rec("d-1", "d-u", "assistant", message=amsg("D1", tool_use("dt1", "MultiEdit", file_path="/repo/bar.py",
                                                               edits=[{"old_string": "a", "new_string": "b"}]))),
    rec("d-2", "d-1", "assistant", message=amsg("D2", tool_use("dt2", "Bash", command="ls"))),
]

ch_a, f_a = add_session(SESS_A, project="alpha", sid="sess-a", mtime=T_A)
ch_b, f_b = add_session(SESS_B, project="alpha", sid="sess-b", mtime=T_B)
ch_c, f_c = add_session(SESS_C, project="beta", sid="sess-c", mtime=T_C)
ch_d, f_d = add_session(SESS_D, project="alpha", sid="sess-d", mtime=T_D)


# === 1. facets RECOMPUTED from the raw: Edit/Write/MultiEdit write files; Read does not ==============
# `_cleaned_facets` re-derives {repo, session, files, tools, time} from the raw — files/tools are SETS.

assert f_a["files"] == {"/repo/foo.py"} and f_a["tools"] == {"Bash", "Edit", "Read"}, \
    f"session A: Edit writes foo.py, Read of readonly.py is NOT a file edit; got {f_a}"
assert f_b["files"] == {"/repo/bar.py", "/repo/foo.py"} and f_b["tools"] == {"Bash", "Edit"}, f_b
assert f_c["files"] == {"/other/baz.py"} and f_c["tools"] == {"Grep", "Write"}, f_c
assert f_d["files"] == {"/repo/bar.py"} and f_d["tools"] == {"Bash", "MultiEdit"}, f_d
# repo/session/session-time come straight off the raw's origin_ref (the one derived_from hop).
assert f_a["repo"] == "alpha" and f_a["session"] == "sess-a" and f_a["time"] == T_A, f_a

# the stored-facets puncture is GONE: no cleaned sidecar carries session_meta (facets recompute from raw).
for ch in (ch_a, ch_b, ch_c, ch_d):
    assert "session_meta" not in blobstore.get_meta(ch, R), f"cleaned sidecar must not store session_meta: {ch}"
# nor does a chunkset sidecar — put_derived lost its session_meta param entirely (back to pre-3a).
cs_h, _, _ = chunk.materialize(blobstore.get_meta(ch_a, R)["derived_from"], root=R)
assert "session_meta" not in blobstore.get_meta(cs_h, R), "chunkset sidecar must not store session_meta"


# === 1b. NotebookEdit writes notebook_path (a file edit); a session OFF the golden — facets unchanged
SESS_NB = [  # NotebookEdit names its path `notebook_path`, not `file_path`; the Read must NOT count
    rec("nb-u", None, "user", message=umsg("edit a notebook")),
    rec("nb-1", "nb-u", "assistant", message=amsg("NB1", tool_use("nbt1", "NotebookEdit",
                                                                   notebook_path="/repo/analysis.ipynb",
                                                                   new_source="cells"))),
    rec("nb-2", "nb-1", "assistant", message=amsg("NB2", tool_use("nbt2", "Read", file_path="/repo/x.py"))),
]
_, f_nb = add_session(SESS_NB, project="nb", sid="sess-nb", mtime=T_A)
assert f_nb["files"] == {"/repo/analysis.ipynb"}, f"NotebookEdit's notebook_path is a file edit: {f_nb}"
assert f_nb["tools"] == {"NotebookEdit", "Read"}, f_nb


# === 2. concept_facets: union over a concept's cited cleaned blobs ===================================

mint_concept("c1-foo", "foo edits", [ch_a])               # one session
mint_concept("c2-foobar", "foo and bar", [ch_b])          # one session, shares foo with c1
mint_concept("c3-beta", "beta baz", [ch_c])               # the disjoint outlier
mint_concept("c4-span", "alpha spanning", [ch_a, ch_d])   # TWO sessions → facets union across them

from ratchet.dream import load_concepts  # noqa: E402
by_id = {c["id"]: c for c in load_concepts(R)}
f4 = concepts.concept_facets(by_id["c4-span"], R)
assert f4["sessions"] == ["sess-a", "sess-d"], f"a concept spans the sessions it cites: {f4['sessions']}"
assert f4["files"] == ["/repo/bar.py", "/repo/foo.py"], f"files union across A+D: {f4['files']}"
assert f4["tools"] == ["Bash", "Edit", "MultiEdit", "Read"], f4["tools"]
assert f4["repos"] == ["alpha"] and f4["time_range"] == [T_A, T_D], (f4["repos"], f4["time_range"])


# === 3. the derived graph — facets, edges, clusters — vs the committed golden ========================

graph = concepts.concept_graph(R)

# targeted invariants (the edges/clusters the golden encodes — hand-verified here so the golden is not
# self-certifying): a shared file makes an edge; the disjoint, month-apart concept makes none.
def has_edge(src, dst, kind):
    return any(e["source"] == src and e["target"] == dst and e["kind"] == kind for e in graph["edges"])

assert has_edge("c1-foo", "c2-foobar", "shares-file"), "c1 and c2 both edited /repo/foo.py"
assert has_edge("c1-foo", "c2-foobar", "shares-tool"), "c1 and c2 both ran Bash + Edit"
assert has_edge("c2-foobar", "c4-span", "shares-file"), "c2 and c4 both touched bar.py + foo.py"
assert has_edge("c1-foo", "c4-span", "temporal-proximity"), "c1 (10:00) sits inside c4's [10:00,12:00]"
assert not any("c3-beta" in (e["source"], e["target"]) for e in graph["edges"]), \
    "the beta concept shares no repo/file/tool and is a month away → NO edge"

clusters = {cl["leader"]: cl["members"] for cl in graph["clusters"]}
assert clusters == {"c1-foo": ["c1-foo", "c2-foobar", "c4-span"], "c3-beta": ["c3-beta"]}, \
    f"the three alpha concepts cluster; beta stands alone: {clusters}"

# the whole graph against the committed golden, with a legible unified diff on mismatch.
if os.environ.get("RATCHET_REGEN_GOLDEN"):
    GOLDEN.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"REGENERATED {GOLDEN}")
assert GOLDEN.exists(), f"missing golden — regenerate with RATCHET_REGEN_GOLDEN=1: {GOLDEN}"
golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
if graph != golden:
    e = json.dumps(golden, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    a = json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    raise AssertionError("concept graph != golden:\n"
                         + "\n".join(difflib.unified_diff(e, a, "golden", "actual", lineterm="")))


# === 4. concept_clusters is the same cluster view; an empty store is empty, never fatal =============

assert concepts.concept_clusters(R) == graph["clusters"], "concept_clusters mirrors the graph's clusters"
empty = config.ensure_layout(Path(tempfile.mkdtemp(prefix="ratchet-test-concepts-empty-")))
assert concepts.concept_graph(empty) == {"nodes": [], "edges": [], "clusters": []}, "no concepts → empty graph"

print("test_concepts: all assertions passed")
