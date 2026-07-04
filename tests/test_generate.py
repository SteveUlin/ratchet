"""generate tests (Section 5, ADR-0020): fabricate VALID concepts with known statements/ids/tags and
provenance, then assert `generate.project` renders the expected TAG-LED, provenance-marked region
(deterministic — theme `## <tag>` headings, a de-redundant repo-only rule line, an untagged concept in the
trailing `## general` bucket), `--apply` refreshes the region IN PLACE while preserving human content above
AND below the markers, a target with the markers TWICE is REFUSED (ambiguous region — never clobber), a
RETIRED concept vanishes on re-project (retraction-for-free), re-apply with unchanged concepts is
byte-identical (idempotent), the empty store yields a clear empty projection, and the KIND filter
(ADR-0029) keeps `reference` concepts out of the default projection — stated in the region's kinds note
— while `--kinds behavioral,reference` widens.

generate is the mechanical projection that CLOSES THE LOOP (concept → CLAUDE.md); no LLM, so this whole suite
runs offline.

Run: `python tests/test_generate.py` (throwaway dir)."""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-generate-")

from ratchet import blobstore, config, garden, generate, review, weave  # noqa: E402

R = config.ensure_layout()


# --- synthetic transcripts (mirrors test_concept_digest's fixture) --------------------------------

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


# Two alpha sessions (s1, s2) + one beta session (s3), each an Edit. The FILE/cluster axis no longer drives
# grouping (tag does); the sessions still set ENTRENCHMENT — c-a is cited across s1+s2 (2 distinct sessions),
# so it outranks the single-session c-b within their shared `workflow` group.
T1, T2, T3 = "2026-06-01T10:00:00+00:00", "2026-06-02T10:00:00+00:00", "2026-07-01T10:00:00+00:00"
ch1 = add_session(edit_session("s1", "/repo/foo.py"), project="alpha", sid="s1", mtime=T1)
ch2 = add_session(edit_session("s2", "/repo/foo.py"), project="alpha", sid="s2", mtime=T2)
ch3 = add_session(edit_session("s3", "/other/baz.py"), project="beta", sid="s3", mtime=T3)

mint_concept("c-a", "foo span", "Run the formatter before every commit.", [ch1, ch2])  # alpha, 2 sessions
mint_concept("c-b", "foo one", "Keep functions under fifty lines.", [ch1])             # alpha, 1 session
mint_concept("c-d", "beta baz", "Prefer jj over git for every operation.", [ch3])      # beta, 1 session
mint_concept("c-e", "no prov", "Write tests first.", [])                               # no evidence → no repo
# a REFERENCE concept (ADR-0029): true, kept, but lookup material — the default projection excludes it.
# The kind lives on the reviewer's set_kind DECISION, never the blob (a garden re-version can't drop it).
mint_concept("c-f", "an effort fact", "ultracode is an effort level, not a model tier.", [])
review.set_kind("c-f", "reference", R, reason="a fact you'd look up, not conduct")

# Managed tags (3b/ADR-0014) — the PRIMARY tag is now the grouping axis. c-a + c-b share `workflow` (a 2-member
# group), c-d is alone under `version-control`, c-e is untagged → the trailing `general` bucket.
garden.assign_tags("c-a", ["workflow"], "fp", R, run_id="t", model="test")
garden.assign_tags("c-b", ["workflow"], "fp", R, run_id="t", model="test")
garden.assign_tags("c-d", ["version-control"], "fp", R, run_id="t", model="test")


# === 1. project: TAG-LED groups, de-redundant rules, provenance markers; deterministic ==============

p = generate.project(R)
assert p.startswith(generate.START) and p.endswith(generate.END), f"the region is marker-delimited:\n{p}"

# every concept's statement appears VERBATIM, each tagged with its concept-id provenance marker.
assert "Run the formatter before every commit. <!-- c-a -->" in p, p
assert "Keep functions under fifty lines. <!-- c-b -->" in p, p
assert "Prefer jj over git for every operation. <!-- c-d -->" in p, p
assert "Write tests first. <!-- c-e -->" in p, p

# TAG-LED headings (theme-shaped, like a human CLAUDE.md), NOT the old provenance-cluster headings/comments.
assert "## workflow" in p, f"the primary tag is the group heading:\n{p}"
assert "## version-control" in p, p
assert "## general" in p, f"the untagged concept lands in the trailing general bucket:\n{p}"
assert "<!-- cluster" not in p, "the cluster comments are gone (tag-led, not cluster-led)"
assert "**Shared" not in p, "the shared-basis cluster headings are gone"

# DE-REDUNDANT rule line: the heading carries the THEME (tag), so the trigger carries only the WHERE (repo) —
# no tag echoed in the trigger anymore. A concept with no repo (c-e) renders UNCONDITIONALLY (no trigger).
assert "- When working in `alpha`: Run the formatter before every commit. <!-- c-a -->" in p, f"repo-only trigger:\n{p}"
assert "- When working in `beta`: Prefer jj over git for every operation. <!-- c-d -->" in p, p
assert "- Write tests first. <!-- c-e -->" in p, f"no repo → unconditional rule (no trigger):\n{p}"
assert "with `workflow`" not in p and "with `version-control`" not in p, "the tag is NOT echoed in the trigger"

# PARTITION: every concept in EXACTLY one group (4 statements, 4 markers, 3 headings).
assert p.count("<!-- c-") == 4, f"four concepts, four provenance markers, one group each:\n{p}"
assert p.count("## ") == 3, f"three groups (workflow, version-control, general):\n{p}"

# GROUP order: `general` always LAST; the rest by descending size then name. Within `workflow`, the more-
# entrenched c-a (2 sessions) precedes c-b (1 session).
order = [p.index(h) for h in ("## workflow", "## version-control", "## general")]
assert order == sorted(order), f"groups: workflow (2) → version-control (1) → general (last):\n{p}"
assert p.index("<!-- c-a -->") < p.index("<!-- c-b -->"), f"within workflow, c-a (more sessions) leads c-b:\n{p}"

# DETERMINISTIC: same store → byte-identical projection.
assert generate.project(R) == p, "the projection is deterministic (order-stable)"


# === 2. apply: refresh-in-place PRESERVING human content above AND below the markers ===============

ABOVE = "# My CLAUDE.md\n\nHand-written rule: always greet the user.\n\n"
BELOW = "\n\n## Notes\n\nHuman-owned section below the region.\n"
STALE = f"{generate.START}\nstale content a human should never have to keep\n{generate.END}"

target = Path(tempfile.mkdtemp(prefix="ratchet-target-")) / "CLAUDE.md"
target.write_text(ABOVE + STALE + BELOW)

res = generate.apply(R, target=target)
assert res["action"] == "replaced" and res["changed"], res
out = target.read_text()
assert out.startswith(ABOVE), "human content ABOVE the region is byte-preserved"
assert out.endswith(BELOW), "human content BELOW the region is byte-preserved"
assert "stale content a human should never have to keep" not in out, "the stale region is overwritten"
assert "Run the formatter before every commit. <!-- c-a -->" in out, "the live projection landed in the region"


# === 3. multiplicity refusal: markers TWICE → REFUSE (ambiguous region), never clobber ==============
# A CLAUDE.md that DOCUMENTS the markers (an example block) above the real region carries START/END TWICE.
# find-the-first would splice the WRONG span, so `_region_span` raises — `apply` writes NOTHING, and the
# read-only `current_region` refuses too (the guard rides in `_region_span`). Human content is preserved.

DOC = f"# Docs\n\nExample region:\n{generate.START}\n(an illustration)\n{generate.END}\n\n## Real\n"
REAL = f"{generate.START}\n{generate.EMPTY_BODY}\n{generate.END}\n"
dup = Path(tempfile.mkdtemp(prefix="ratchet-target-dup-")) / "CLAUDE.md"
dup.write_text(DOC + REAL)
before = dup.read_text()

raised = False
try:
    generate.apply(R, target=dup)
except ValueError:
    raised = True
assert raised, "duplicated markers RAISE (ambiguous region — refuse rather than guess the span)"
assert dup.read_text() == before, "the file is NOT written on the raise path — human content preserved"

raised = False
try:
    generate.current_region(before)
except ValueError:
    raised = True
assert raised, "current_region refuses the ambiguous region too (the guard rides in _region_span)"


# === 4. idempotent: re-apply with unchanged concepts is byte-identical ==============================

bytes_after_first = target.read_text()
res2 = generate.apply(R, target=target)
assert not res2["changed"] and res2["action"] == "replaced", f"re-apply is a no-op: {res2}"
assert target.read_text() == bytes_after_first, "re-apply produces a byte-identical file (idempotent)"


# === 5. retraction-for-free: a retired concept vanishes from the region on re-project ===============

review.retire("c-b", R, reason="superseded by a sharper rule")
p_after = generate.project(R)
assert "<!-- c-b -->" not in p_after and "Keep functions under fifty lines." not in p_after, \
    "a retired concept (absent from valid_concepts) drops from the projection"
assert "<!-- c-a -->" in p_after and "<!-- c-d -->" in p_after, "the surviving concepts remain"

# re-apply propagates the retraction into the file, still preserving the human content.
generate.apply(R, target=target)
out2 = target.read_text()
assert out2.startswith(ABOVE) and out2.endswith(BELOW), "human content still preserved after retraction"
assert "Keep functions under fifty lines." not in out2, "the retracted rule is gone from the file too"


# === 6. apply into a file with NO region creates it at the end; empty store → empty projection ======

fresh = Path(tempfile.mkdtemp(prefix="ratchet-target2-")) / "CLAUDE.md"
fresh.write_text("# Existing\n\nA human rule.\n")
res3 = generate.apply(R, target=fresh)
assert res3["action"] == "appended" and res3["changed"], res3
ftext = fresh.read_text()
assert ftext.startswith("# Existing\n\nA human rule.\n"), "existing content is preserved when the region is created"
assert generate.START in ftext and generate.END in ftext, "the region is created at the end"

empty = config.ensure_layout(Path(tempfile.mkdtemp(prefix="ratchet-test-generate-empty-")))
pe = generate.project(empty)
assert pe == f"{generate.START}\n<!-- kinds: behavioral -->\n{generate.EMPTY_BODY}\n{generate.END}", \
    f"the empty store yields a clear empty projection (kinds note + empty-body sentinel):\n{pe}"
assert "<!--" in pe and "## " not in pe, "no rules/headings, but a well-formed (idempotent) region"


# === 7. the KIND filter (ADR-0029): reference excluded by default, stated in the note; --kinds widens =

p7 = generate.project(R)
assert "<!-- c-f -->" not in p7 and "ultracode is an effort level" not in p7, \
    "a reference concept is EXCLUDED from the default projection — lookup material, not conduct"
assert "kinds: behavioral — 1 reference concept(s) excluded" in p7, \
    f"the region's header note states the filter, so a CLAUDE.md reader knows:\n{p7}"

p7w = generate.project(R, kinds=("behavioral", "reference"))
assert "ultracode is an effort level, not a model tier. <!-- c-f -->" in p7w, \
    "--kinds behavioral,reference widens: the reference rule renders (untagged → general)"
assert "kinds: behavioral, reference" in p7w and "excluded" not in p7w, "nothing excluded → nothing claimed"
assert generate.project(R, kinds=("reference", "behavioral")) == p7w, \
    "the kind selection is canonicalized — flag order can't break --apply idempotency"

try:
    generate.project(R, kinds=("behavioral", "mechanism"))
    assert False, "an unknown kind must raise — a typo silently projecting nothing is a hidden rule"
except ValueError:
    pass
raised = False
try:
    generate.main(["--dry-run", "--kinds", "bogus"])
except SystemExit:
    raised = True
assert raised, "the CLI surfaces the bad --kinds cleanly (SystemExit, not a traceback)"

# projected_concepts (the faithfulness context) tracks the SAME filter as the region.
ids_default = {c["id"] for c in generate.projected_concepts(R)}
assert "c-f" not in ids_default and "c-a" in ids_default
wide = {c["id"]: c for c in generate.projected_concepts(R, kinds=("behavioral", "reference"))}
assert wide["c-f"]["kind"] == "reference" and wide["c-a"]["kind"] == "behavioral", \
    "each faithfulness row carries its derived kind (kind-less legacy blobs read behavioral)"

buf = io.StringIO()
with redirect_stdout(buf):
    generate.main(["--dry-run", "--kinds", "behavioral,reference"])
assert "<!-- c-f -->" in buf.getvalue(), "the CLI escape hatch reaches the projection"

print("test_generate: all assertions passed")
