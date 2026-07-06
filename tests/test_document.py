"""document source tests (ADR-0031): tap --file ingests a verbatim `document` blob keyed on its
PATH, the cursor no-ops a re-tap and versions a change; weave's document render is a
line-preserving passthrough MINUS any ratchet:generated region (the SELF-LOOP GUARD — spans in the
cleaned blob can never point inside it, because the region is structurally absent); chunk/glean
run the document shape end-to-end (document prompt variant, its own idempotency key, ADR-0026
pointing discipline unchanged); and the SESSION-IDENTITY epistemology holds: one path = one
session, so a re-tapped identical rule corroborates deterministically at ZERO added maturity, an
edited rule seeds fresh, and ONE lived transcript corroboration matures the claim to net ≈ 2.
Run: `python tests/test_document.py` (throwaway dirs)."""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-doc-")

from ratchet import blobstore, block, chunk, concepts, config, glean, resolve, review, subject, tap, temporal, weave  # noqa: E402
from ratchet.completer import Completion  # noqa: E402

config.ensure_layout()
root = config.data_root()

docdir = Path(tempfile.mkdtemp(prefix="ratchet-test-docdir-"))
doc = docdir / "CLAUDE.md"
sid = str(doc)                       # the document's source id AND session id — path-as-session

RULE = "- Always commit with jj, never git; the working copy is always a commit."
EDITED = "- Never push directly to main; land changes through a reviewed PR bookmark."
SUMMARY = "Always commit with jj, never git; the working copy is always a commit."
EDITED_SUMMARY = "Never push directly to main; land changes through a reviewed PR bookmark."
GENERATED_RULE = "- GENERATED-ONLY-RULE: prefer tabs over spaces in every file."

# the CANONICAL markers generate.py writes, verbatim (pinned here rather than imported — weave must
# not import generate, and this fixture is the drift tripwire for the convention coupling).
GEN_START = ("<!-- ratchet:generated START — managed by `ratchet generate --apply`; "
             "edits here are overwritten -->")
GEN_END = "<!-- ratchet:generated END -->"
assert GEN_START.startswith(weave.GENERATED_START_PREFIX), "guard prefix must match generate's START"
assert GEN_END == weave.GENERATED_END, "guard END must match generate's END"

REGION = f"{GEN_START}\n<!-- kinds: behavioral -->\n{GENERATED_RULE}\n{GEN_END}"
V1 = f"# rules\n\n{RULE}\n\n## style\n\nCompress. Active sentences, direct verbs.\n\n{REGION}\n"
doc.write_text(V1, encoding="utf-8")


def tap_files(*paths, **kw):
    """A fresh TapBlock per call mirrors a fresh process (the cursor reloads from disk in items())."""
    return block.run(tap.TapBlock(files=tuple(paths)), **kw)


# --- 1. tap --file: verbatim document ingest, path-as-session, cursor + versioning ------------

plain = docdir / "notes.md"          # a second file, region-free — multi---file + plain render
plain.write_text("A plain notes file.\nWith two lines.\n", encoding="utf-8")

rep = tap_files(doc, plain)
assert rep.processed == 2 and rep.outputs == 2, f"both files ingested: {rep}"
raw1 = blobstore.latest_version(sid)
assert raw1 is not None, "the document's source id is its path"
m1 = blobstore.get_meta(raw1)
assert m1["source_kind"] == "document" and m1["source_id"] == sid
o = m1["origin_ref"]
assert o["path"] == sid and o["session_id"] == sid, "path-as-session (ADR-0031)"
assert o.get("mtime"), "origin_ref carries the file's mtime — the document's valid-time"
assert "project" not in o and "cwd" not in o, "no repo-facet fields — a document stays subject-empty"
assert blobstore.get(raw1) == V1, "document ingest is VERBATIM — the raw blob is the true file bytes"

# cursor: an unchanged file is filtered inside items() — not even examined.
rep2 = tap_files(doc, plain)
assert rep2.examined == 0, f"cursored files never examined on re-tap: {rep2}"

# a content-identical touch is re-read AT MOST once, ingests nothing, then cheap-skips again.
reads = {"n": 0}
_orig_read_document = tap.read_document
def _counting(p):
    reads["n"] += 1
    return _orig_read_document(p)
tap.read_document = _counting
os.utime(doc, None)
r_touch = tap_files(doc)
r_touch2 = tap_files(doc)
tap.read_document = _orig_read_document
assert reads["n"] == 1 and r_touch.outputs == 0, "touched file re-read once, no new blob"
assert r_touch2.examined == 0, "cursor current again after the touch re-read"

# a CHANGED file mints a new VERSION of the SAME source, prev-linked (the transcript fold, reused).
V2 = V1 + "\nA new hand-written note.\n"
doc.write_text(V2, encoding="utf-8")
r_v2 = tap_files(doc)
assert r_v2.outputs == 1, f"a changed file ingests a new snapshot: {r_v2}"
raw2 = blobstore.latest_version(sid)
assert raw2 != raw1 and blobstore.get_meta(raw2)["prev"] == raw1, "new VERSION, prev-linked"

# --file refuses the datastore-sweep selectors (they'd be silently inert — a hidden rule otherwise).
try:
    with contextlib.redirect_stderr(io.StringIO()):    # argparse prints the refusal before exiting
        tap.main(["--file", str(doc), "--last", "5", "--quiet"])
    raise AssertionError("--file + --last must be refused")
except SystemExit:
    pass

print("OK — tap --file: verbatim document blob, path-as-session origin, cursor no-op + touched-once,")
print("     changed file = new prev-linked version, sweep selectors refused alongside --file")


# --- 2. weave document mode: line-preserving passthrough MINUS the generated region -----------

# strip_generated unit behavior: whole regions out, unterminated strips to EOF, multiple regions.
assert weave.strip_generated(f"a\n{GEN_START}\nzz\n{GEN_END}\nb") == "a\n\nb"
assert weave.strip_generated("keep\n<!-- ratchet:generated START x -->\nnever terminated") == "keep\n", \
    "an unterminated region strips to EOF (when in doubt, exclude)"
assert weave.strip_generated(f"a\n{GEN_START} r1 {GEN_END}\nb\n{GEN_START} r2 {GEN_END}\nc") == "a\n\nb\n\nc", \
    "every region is stripped, not just the first"
assert weave.strip_generated("no region here\n") == "no region here\n", "region-free text is untouched"

ch2, written2, rdoc2 = weave.materialize(raw2)
cleaned2 = blobstore.get(ch2)
assert cleaned2.startswith(f"[document] {sid}"), "the header turn names the file (the speaker-tag analogue)"
assert RULE in cleaned2 and "Compress. Active sentences" in cleaned2, "human content is all there"
# THE SELF-LOOP GUARD: the generated region is STRUCTURALLY absent from the cleaned blob, so no
# downstream span (glean evidence, chunk window) can ever point inside it — there is nothing to point at.
assert "GENERATED-ONLY-RULE" not in cleaned2 and "ratchet:generated" not in cleaned2
# line-preserving passthrough: header + the stripped body, byte-exact.
assert cleaned2 == f"[document] {sid}\n\n" + weave.strip_generated(V2)
# turns tile the cleaned blob (the invariant chunk packs on), all one document segment.
assert "\n\n".join(cleaned2[t.start:t.end] for t in rdoc2.turns) == cleaned2, "turns tile the doc"
assert all(t.kind == "document" and t.segment == 0 for t in rdoc2.turns)
cm = blobstore.get_meta(ch2)
assert cm["source_kind"] == "document" and cm["format"] == weave.DOC_RENDER_FORMAT
assert cm["derived_from"] == raw2 and cm["tags"]["session_id"] == sid
h_again, w_again, _ = weave.materialize(raw2)
assert h_again == ch2 and not w_again, "document materialize is idempotent (content-addressed)"

# a plain, region-free file renders as header + its exact bytes.
raw_p = blobstore.latest_version(str(plain))
ch_p, _, _ = weave.materialize(raw_p)
assert blobstore.get(ch_p) == f"[document] {plain}\n\n" + plain.read_text(encoding="utf-8"), \
    "a region-free document is a pure passthrough behind the header"

# WeaveBlock --all enumerates documents beside transcripts.
assert raw2 in set(weave.WeaveBlock().items(root)), "--all sweeps document raws too"

print("OK — weave document mode: generated region structurally absent (self-loop guard), passthrough")
print("     is line-preserving + byte-exact, turns tile, DOC_RENDER_FORMAT sidecar, idempotent")


# --- 3. chunk: the document shape flows through pointer windows unchanged ---------------------

cs2, _, chunks2 = chunk.materialize(raw2)
assert blobstore.get_meta(cs2)["format"] == chunk.DOC_CHUNKSET_FORMAT
assert all(c.kinds == ["document"] for c in chunks2), "document turns → document chunk kinds"
assert chunk.chunkset_for(ch2) == cs2 and glean.chunkset_for_source(sid) == cs2
assert ch2 in set(chunk.ChunkBlock().items(root)), "ChunkBlock --all enumerates document cleaned blobs"
assert "".join(chunk.resolve(c) for c in chunks2).replace("\n\n", "") \
       .startswith("[document]"), "chunks resolve against the immutable cleaned blob"

# --- 4. glean document mode: rule extraction, valid spans, its OWN idempotency key ------------


class FakeCompleter:
    """The test_glean pattern: a canned candidate naming a `quote` is translated to a line selection
    by FINDING the text in THIS chunk's numbered prompt — emitted only by the chunk that contains it
    (the per-chunk trust property). Records every system prompt so the mode dispatch is assertable."""
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0
        self.systems: list[str] = []

    def __call__(self, system, user):
        self.calls += 1
        self.systems.append(system)
        line_of = {}
        for line in user.splitlines():
            num, sep, body = line.partition("| ")
            if sep and num.strip().isdigit():
                line_of[int(num)] = body
        out = []
        for cand in self.candidates:
            cand = dict(cand)
            q = cand.pop("quote")
            hit = next((n for n, body in line_of.items() if q in body), None)
            if hit is not None:
                cand["lines"] = {"from": hit, "to": hit}
                out.append(cand)
        return Completion(text=json.dumps({"events": out}), model="fake", cost_usd=0.001)


CANDS = [
    {"quote": "Always commit with jj", "summary": SUMMARY,
     "markers": {"insight": 0.2}, "confidence": 0.9},
    {"quote": "Never push directly to main", "summary": EDITED_SUMMARY,
     "markers": {"insight": 0.2}, "confidence": 0.9},
]

# the pre-filter: document mode needs no speaker tags; transcript mode still does.
plain_text = "Rules line one about workflow.\nRules line two about style." + " x" * 20
assert glean.has_signal_potential(plain_text, mode="document"), "doc mode: size floor only"
assert not glean.has_signal_potential(plain_text), "transcript mode still requires a speaker turn"
assert not glean.has_signal_potential("tiny", mode="document"), "the size floor still applies"

fake = FakeCompleter(CANDS)
blk = glean.GleanBlock(fake, model="fake", targets=[cs2])
items2 = list(blk.items(root))
assert items2 and all(it.mode == "document" for it in items2), "chunkset sidecar sets document mode"
# the marker key differs from transcript mode: it carries DOC_PROMPT_VERSION.
k0 = blk.key(items2[0])
assert k0 == f"{glean.chunk_key(items2[0].chunk)}:{glean.DOC_PROMPT_VERSION}" \
       and k0 != glean.chunk_key(items2[0].chunk), "document done-keys carry the doc version knob"

rep_g = block.run(blk, progress=None)
assert fake.calls >= 1 and set(fake.systems) == {glean.DOC_SYSTEM_PROMPT}, \
    "document chunks get the DOCUMENT system prompt, never the transcript one"
assert rep_g.outputs == 1, f"exactly the rule line becomes an event: {rep_g}"
assert (k0, glean.PROMPT_VERSION, "fake") in block.done_index("glean", root), \
    "the done-key rides the doc-suffixed target"

evs = glean.load_events(root)
assert len(evs) == 1
ev = evs[0]
cb2 = cleaned2.encode("utf-8")
sp = ev["evidence"][0]
quote = cb2[sp["byte_start"]:sp["byte_end"]].decode("utf-8")
assert "Always commit with jj" in quote and "GENERATED" not in quote, \
    "the span resolves to real document bytes — and can never touch the (absent) generated region"
assert ev["summary"] == SUMMARY
ev_meta = blobstore.get_meta(blobstore.latest_by_kind("event", root)[ev["id"]], root)
assert ev_meta["origin_ref"]["prompt_version"] == glean.DOC_PROMPT_VERSION, \
    "provenance names the DOCUMENT prompt version (producer rides meta.origin_ref, ADR-0007)"
assert subject.is_empty(ev["subject_key"]), "a document event has an EMPTY subject (seed-only, no repo)"

# idempotent re-run: zero new calls, all chunks skipped.
before = fake.calls
rep_again = block.run(glean.GleanBlock(fake, model="fake", targets=[cs2]), progress=None)
assert fake.calls == before and rep_again.skipped == len(chunks2), "doc-mode re-run skips on its key"

# --source FOCUS reaches documents through the path fallback (project_of) — the seeding flow's filter.
assert blobstore.project_of(ch2, root) == sid, "a document's FOCUS handle is its path"
focused = list(glean.GleanBlock(fake, model="fake", targets=[cs2], source_filter="claude.md").items(root))
assert len(focused) == len(chunks2), "--source CLAUDE.md selects the document's chunks"
assert not list(glean.GleanBlock(fake, model="fake", targets=[cs2], source_filter="no-such").items(root))

print("OK — glean document mode: doc system prompt + relaxed pre-filter, spans verbatim by")
print("     construction, DOC_PROMPT_VERSION in producer + done-key, idempotent, --source via path")


# --- 5. SESSION IDENTITY end-to-end: one path = one session ------------------------------------


class NoneCompleter:
    """A resolve residue adjudicator that always abstains — and counts calls, so the tests can prove
    the document paths below never even ASK the LLM (det dup / $0 non-match / same-session gate)."""
    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return Completion(text='{"verdict": "none"}', model="fake", cost_usd=0.0)


def claim_by_title(title):
    got = [c for c in resolve.claim_pool(root) if c["title"] == title]
    assert len(got) == 1, f"exactly one claim titled {title!r}: {[c['title'] for c in resolve.claim_pool(root)]}"
    return got[0]


rc = NoneCompleter()

# 5a. the document claim mints with session = the PATH, and sits INCUBATING at net ≈ 1.
r5a = resolve.run(rc, root=root)
assert r5a.n_minted >= 1, f"the document rule seeds a claim: {r5a}"
claim = claim_by_title(SUMMARY)
assert claim["sessions_seen"] == [sid], "the claim's ONE session is the document's path"
assert claim["support"] == {"events": 1, "sessions": 1}
assert claim["scope_repo"] == concepts.SCOPE_GLOBAL, "empty subject derives scope=global (never a path-repo)"
vt = temporal.session_valid_times(root)
assert vt.get(sid), "the document session is dated by its file mtime (it joins the valid-time map)"
net1 = temporal.net_entrenchment(claim, valid_times=vt)
assert abs(net1 - 1.0) < 0.05 and net1 < temporal.MATURITY_WEIGHT, f"one session ⇒ incubating: {net1}"
assert any(r["takeaway_id"] == claim["id"] for r in review.incubating(root)), \
    "the fresh document claim sits in review --incubating (the seeding flow's queue)"

# 5b. RE-TAPPED IDENTICAL RULE → the exact-dup fast path corroborates DETERMINISTICALLY ($0,
#     by:"det") into the same claim — and adds ZERO maturity: same session, no new distinct-session
#     support. A document can never self-mature by being saved again.
V3 = V2 + "\nAnother unrelated hand-written note.\n"
doc.write_text(V3, encoding="utf-8")
tap_files(doc)
raw3 = blobstore.latest_version(sid)
assert raw3 not in (raw1, raw2), "v3 is a third version of the same source"
cs3, _, chunks3 = chunk.materialize(raw3)
block.run(glean.GleanBlock(fake, model="fake", targets=[cs3]), progress=None)
r5b = resolve.run(rc, root=root)
assert r5b.n_corroborated == 1 and r5b.n_minted == 0, f"the identical rule corroborates, never twins: {r5b}"
assert rc.calls == 0, "settled deterministically — the LLM was never asked"
claim = claim_by_title(SUMMARY)
assert claim["support"]["events"] == 2 and claim["support"]["sessions"] == 1, \
    "a re-tap adds an event but NO session (path-as-session)"
det_edges = [e for e in resolve.live_edges(root, resolve.reject_merge_facts(root))[claim["id"]]
             if (e.get("match") or {}).get("by") == "det"]
assert len(det_edges) == 1 and det_edges[0]["session_id"] == sid, "the dup edge is by:'det', same session"
net2 = temporal.net_entrenchment(claim, valid_times=temporal.session_valid_times(root))
assert abs(net2 - net1) < 0.05 and net2 < temporal.MATURITY_WEIGHT, \
    f"ZERO maturity gain from the re-tap ({net1} → {net2}) — a document cannot self-mature"

# 5c. an EDITED rule seeds a FRESH claim — and again without an LLM call: it is either a $0
#     non-match or blocked by the same-session gate (a document may adjudicate only against LIVED
#     claims, which carry different sessions).
V4 = V3.replace(RULE, EDITED)
doc.write_text(V4, encoding="utf-8")
tap_files(doc)
cs4, _, _ = chunk.materialize(blobstore.latest_version(sid))
block.run(glean.GleanBlock(fake, model="fake", targets=[cs4]), progress=None)
r5c = resolve.run(rc, root=root)
assert r5c.n_minted == 1 and rc.calls == 0, f"an edited rule seeds fresh, LLM-free: {r5c}"
claim_edited = claim_by_title(EDITED_SUMMARY)
assert claim_edited["sessions_seen"] == [sid]

# 5d. a LIVED transcript event (different session) corroborating the document claim → maturity 2:
#     the owner living the rule is what matures it.
def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

LIVED_LINE = ("Understood. " + SUMMARY + " I will use jj for every change in this session going "
              "forward, as you asked.")
trans = "\n".join(json.dumps(r) for r in [
    rec("u0", None, "user", message={"role": "user", "content": "set up the repo workflow please"}),
    rec("a0", "u0", "assistant",
        message={"role": "assistant", "id": "M0",
                 "content": [{"type": "text", "text": LIVED_LINE}]}),
]) + "\n"
raw_t, _ = blobstore.ingest(trans, source_kind="transcript", source_id="lived-sess-1",
                            origin_ref={"session_id": "lived-sess-1",
                                        "mtime": config.now()})
cs_t, _, _ = chunk.materialize(raw_t)
fake_t = FakeCompleter(CANDS)
block.run(glean.GleanBlock(fake_t, model="fake", targets=[cs_t]), progress=None)
assert set(fake_t.systems) == {glean.SYSTEM_PROMPT}, "a transcript chunkset still gets the transcript prompt"
r5d = resolve.run(rc, root=root)
assert r5d.n_corroborated == 1 and rc.calls == 0, f"identical statement → det dup, cross-session: {r5d}"
claim = claim_by_title(SUMMARY)
assert set(claim["sessions_seen"]) == {sid, "lived-sess-1"} and claim["support"]["sessions"] == 2, \
    "the lived session is a SECOND distinct session"
net3 = temporal.net_entrenchment(claim, valid_times=temporal.session_valid_times(root))
assert abs(net3 - 2.0) < 0.05 and net3 >= temporal.MATURITY_WEIGHT, \
    f"document + one lived session ⇒ mature (net {net3} ≥ bar {temporal.MATURITY_WEIGHT})"

print("OK — session identity: re-tapped identical rule = det corroboration at ZERO maturity gain,")
print("     edited rule seeds fresh (LLM never asked), ONE lived session matures the claim to net 2")

print("\nall document-source tests passed.")
