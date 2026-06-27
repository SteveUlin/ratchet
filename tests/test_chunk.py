"""chunk tests: the chunkset is a MATERIALIZED, auditable pointer-set into a cleaned blob — never
split a turn, never cross a compact segment, contiguous tiling, byte offsets that resolve to the
exact text by slicing the immutable cleaned blob (no on-the-fly render), lineage cleaned→chunkset,
idempotent. Plus a real-corpus chunkset whose every pointer resolves. Run: `python tests/test_chunk.py`."""
import glob
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-chunk-")

from ratchet import blobstore, block, chunk, config, weave  # noqa: E402

config.ensure_layout()


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}

# A transcript with several sizeable turns, a compact in the middle, and a unicode turn (to prove
# byte offsets, not char offsets, anchor the pointers).
records = [rec("u0", None, "user", message={"role": "user", "content": "kick off the work"})]
parent = "u0"
for i in range(6):
    u = f"a{i}"
    body = f"step {i}: " + ("λ wörk ✓ " * 40)        # multibyte chars → byte≠char offsets
    records.append(rec(u, parent, "assistant", message=amsg(f"M{i}", body)))
    parent = u
records.append(rec("cb", None, "system", subtype="compact_boundary", logicalParentUuid="a5",
                   compactMetadata={"trigger": "manual", "preTokens": 99}))
records.append(rec("a6", "cb", "assistant", message=amsg("M6", "after the compact, continue")))
blob = "\n".join(json.dumps(r) for r in records) + "\n"

raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id="chunk-syn",
                            origin_ref={"project": "p", "session_id": "chunk-syn"})
doc = weave.render(blob)

# --- 1. build: never split a turn, never cross a segment, contiguous tiling, byte pointers ---

chunks = chunk.build(doc, budget=600)
assert len(chunks) > 1, "small budget yields multiple chunks"
starts = {turn.start for turn in doc.turns}
ends = {turn.end for turn in doc.turns}
cleaned_bytes = doc.text.encode("utf-8")
for c in chunks:
    # byte pointer resolves to exactly the turn-span text (byte≠char here: multibyte content)
    sliced = cleaned_bytes[c.byte_start:c.byte_end].decode("utf-8")
    assert sliced == doc.text[doc.turns[c.turn_start].start:doc.turns[c.turn_end].end], "pointer slice == turns"
    assert doc.turns[c.turn_start].start in starts and doc.turns[c.turn_end].end in ends, "boundaries on turn edges"
    assert len({doc.turns[i].segment for i in range(c.turn_start, c.turn_end + 1)}) == 1, "no chunk crosses a compact"
assert chunks[0].byte_start == 0 and chunks[-1].byte_end == len(cleaned_bytes), "tiling spans the whole doc"
assert all(chunks[i].turn_end + 1 == chunks[i + 1].turn_start for i in range(len(chunks) - 1)), "contiguous"
assert any(c.byte_end - c.byte_start != doc.turns[c.turn_end].end - doc.turns[c.turn_start].start
           for c in chunks), "byte offsets differ from char offsets (multibyte content present)"

# --- 2. materialize: a stored chunkset blob, pointers resolved by slicing the cleaned blob ---

cs_hash, written, chunks = chunk.materialize(raw_h, budget=600)
assert written, "first materialize writes the chunkset"
_, again, _ = chunk.materialize(raw_h, budget=600)
assert not again, "materialize is idempotent (content-addressed)"

# the cleaned blob exists and is the chunks' anchor; the chunkset points at it
cleaned_hash = chunks[0].cleaned_hash
assert blobstore.has(cleaned_hash) and blobstore.get_meta(cleaned_hash)["produced_by"] == "weave"
csm = blobstore.get_meta(cs_hash)
assert csm["kind"] == "derived" and csm["produced_by"] == "chunk" and csm["derived_from"] == cleaned_hash
assert csm["format"] == chunk.CHUNKSET_FORMAT and csm["tags"]["budget"] == 600

# lineage: raw → cleaned → chunkset, all queryable by one scan
assert chunk.chunkset_for(cleaned_hash) == cs_hash, "chunkset reachable from its cleaned blob"
assert blobstore.get_meta(cleaned_hash)["derived_from"] == raw_h, "cleaned reachable from raw"

# resolve reads ONLY stored bytes (no render); load round-trips the stored pointers
for c in chunk.load(cs_hash):
    text = chunk.resolve(c)
    assert text == doc.text[doc.turns[c.turn_start].start:doc.turns[c.turn_end].end], "resolved == source span"

# trust primitive: a quote is trusted iff it is a substring of the resolved chunk
quote = "after the compact"
owner = [c for c in chunks if quote in chunk.resolve(c)]
assert len(owner) == 1, "the quote resolves to exactly one chunk"

# carriage returns from rendered tool output must survive the cleaned blob and resolve byte-exact
# (content-addressing would break if the store rewrote \r) — adversarial finding.
crlf_blob = "\n".join(json.dumps(r) for r in [
    rec("c0", None, "user", message={"role": "user", "content": "fetch it"}),
    rec("c1", "c0", "assistant", message={"role": "assistant", "id": "MC",
        "content": [{"type": "tool_use", "id": "T", "name": "Bash", "input": {"command": "curl"}}]}),
    rec("c2", "c1", "user", message={"role": "user", "content": [{"type": "tool_result",
        "tool_use_id": "T", "content": "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\nbody"}]}),
]) + "\n"
craw, _ = blobstore.ingest(crlf_blob, source_kind="transcript", source_id="crlf", origin_ref={})
cdoc = weave.render(crlf_blob)
assert "\r" in cdoc.text, "carriage returns reach the cleaned text"
_, _, cchunks = chunk.materialize(craw)
assert blobstore.blob_hash(blobstore.get(cdoc.cleaned_hash)) == cdoc.cleaned_hash, "\\r-bearing blob is content-addressed"
assert any("\r" in chunk.resolve(c) for c in cchunks), "\\r survives into the resolved chunk"
assert all(chunk.resolve(c) == cdoc.text[cdoc.turns[c.turn_start].start:cdoc.turns[c.turn_end].end]
           for c in cchunks), "resolution is byte-exact through \\r"

# a cleaned blob may hold several chunksets (one per budget); chunkset_for picks by budget
chunk.materialize(raw_h, budget=80)
assert chunk.chunkset_for(cleaned_hash, budget=600) != chunk.chunkset_for(cleaned_hash, budget=80), \
    "chunkset_for disambiguates by budget"

# --- 2b. the ChunkBlock path: item = a cleaned blob, idempotency, the processed marker -----

class _Probe:
    def __init__(self): self.lines = []
    def __call__(self, report, item, key, n_out, cost, *, dry_run=False, errored=False):
        self.lines.append((key, n_out, cost, report.processed, report.skipped, dry_run, errored))

# A FRESH session so the chunkset for budget 600 is not already materialized (§2 used budget 600
# on chunk-syn already), letting us assert the outputs==1-on-first-write case cleanly.
brecords = [rec("b0", None, "user", message={"role": "user", "content": "fresh chunk session"})]
bparent = "b0"
for i in range(4):
    u = f"b{i+1}"
    brecords.append(rec(u, bparent, "assistant", message=amsg(f"BM{i}", f"step {i}: " + ("data " * 50))))
    bparent = u
bblob = "\n".join(json.dumps(r) for r in brecords) + "\n"
braw, _ = blobstore.ingest(bblob, source_kind="transcript", source_id="chunk-block-fresh",
                           origin_ref={"project": "p", "session_id": "chunk-block-fresh"})

cb = chunk.ChunkBlock(budget=600)
assert cb.name == "chunk" and cb.commits_per_item is True
assert cb.params == (("render_version", weave.RENDER_VERSION), ("budget", "600")), \
    "chunk keys on (render_version, budget)"
assert isinstance(cb, block.Block), "ChunkBlock satisfies the structural Block protocol"

bdoc = weave.render(bblob)
bcleaned = bdoc.cleaned_hash  # the item identity per ADR-0009 (cleaned blob, not raw)

# the cleaned blob does not exist YET — chunk's items() over a source walks raw → derived(cleaned);
# but process() materializes the raw (which builds the cleaned blob first), so the first run must
# create both. To enumerate, the cleaned blob must be discoverable: weave it first (the real
# pipeline runs weave before chunk). This mirrors production (weave --all, then chunk --all).
weave.materialize(braw)
assert blobstore.has(bcleaned), "weave produced the cleaned blob chunk enumerates"
assert cb.key(bcleaned) == bcleaned, "the item key IS the cleaned blob hash"

probe = _Probe()
r1 = block.run(cb, source_id="chunk-block-fresh", progress=probe)
assert (r1.examined, r1.processed, r1.skipped, r1.errored) == (1, 1, 0, 0), "first run chunks one cleaned blob"
assert r1.outputs == 1, "a never-chunked cleaned blob yields one chunkset"
assert r1.cost_usd == 0.0 and not r1.stopped_on_budget, "chunk is deterministic, cost 0"
assert probe.lines == [(bcleaned, 1, 0.0, 1, 0, False, False)], "progress streamed exactly one landed item"

# the chunkset is real, lineage cleaned→chunkset holds, and its pointers resolve byte-exact through
# the block path (the block must preserve pointer correctness, not just write a marker)
bcs = chunk.chunkset_for(bcleaned, budget=600)
assert bcs is not None and blobstore.get_meta(bcs)["derived_from"] == bcleaned, "lineage cleaned→chunkset"
for c in chunk.load(bcs):
    assert chunk.resolve(c) == bdoc.text[bdoc.turns[c.turn_start].start:bdoc.turns[c.turn_end].end], \
        "block-produced chunkset resolves byte-exact to its turn span (trust anchor intact)"

# the processed marker keys on (cleaned_hash, render_version, budget)
done = block.done_index("chunk", config.ensure_layout())
assert (bcleaned, weave.RENDER_VERSION, "600") in done, "marker keys on (cleaned, render_version, budget)"
markers = list(blobstore.decisions_for(bcleaned, verb="processed", stage="chunk"))
assert len(markers) == 1 and markers[0]["budget"] == "600" and markers[0]["target"] == bcleaned
assert markers[0]["n_outputs"] == 1

# re-run skips (idempotent); no re-chunk, no duplicate marker
probe2 = _Probe()
r2 = block.run(cb, source_id="chunk-block-fresh", progress=probe2)
assert (r2.examined, r2.processed, r2.skipped, r2.outputs) == (1, 0, 1, 0), "re-run skips the done item"
assert probe2.lines == [], "a skipped item lands no progress line"
assert len(list(blobstore.decisions_for(bcleaned, verb="processed", stage="chunk"))) == 1, "no duplicate marker"

# a DIFFERENT budget is a distinct done-key → re-chunks the same cleaned blob (new chunkset + marker)
cb80 = chunk.ChunkBlock(budget=80)
r3 = block.run(cb80, source_id="chunk-block-fresh", progress=None)
assert (r3.processed, r3.skipped) == (1, 0), "a different budget re-processes (distinct done-key)"
assert (bcleaned, weave.RENDER_VERSION, "80") in block.done_index("chunk", config.ensure_layout())
assert chunk.chunkset_for(bcleaned, budget=80) != bcs, "budget 80 is a distinct chunkset"

# --all enumerates every cleaned blob; error isolation: a forced raise is counted, run continues
class _Boom(chunk.ChunkBlock):
    def process(self, cleaned_hash, *, root, run_id):
        if cleaned_hash == bcleaned:
            raise RuntimeError("boom")
        return super().process(cleaned_hash, root=root, run_id=run_id)
boom = _Boom(budget=600); boom.params = (("render_version", weave.RENDER_VERSION), ("budget", "BOOM"))
r4 = block.run(boom, progress=None)
assert r4.errored >= 1, "the raising item is isolated as errored, run completes"
assert (bcleaned, weave.RENDER_VERSION, "BOOM") not in block.done_index("chunk", config.ensure_layout()), \
    "an errored item writes NO marker (retried next run)"

print("OK — ChunkBlock: item=cleaned blob, --all/--source-id, idempotent re-run (marker skip),")
print("     budget bump re-chunks, marker keyed (cleaned, render_version, budget), byte-exact pointers,")
print("     streaming progress, error isolation")


print("OK — chunkset materialized as byte-offset pointers, never-split/segment-safe tiling,")
print("     resolve-by-slice (no render), cleaned→chunkset lineage, idempotent, trust primitive,")
print("     \\r byte-exact through the store, chunkset_for(budget)")


# --- 3. real corpus: every pointer in a real chunkset resolves against its cleaned blob ------

def _interesting(path):
    try:
        return os.path.getsize(path) > 200_000 and b"compact_boundary" in Path(path).read_bytes()[:5_000_000]
    except OSError:
        return False


real = sorted((p for p in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
               if _interesting(p)), key=os.path.getsize)
if not real:
    print("SKIP real-corpus check — no large compacted transcript found")
else:
    path = real[0]
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    h, _ = blobstore.ingest(raw, source_kind="transcript", source_id="real-" + Path(path).stem,
                            origin_ref={"project": Path(path).parent.name, "session_id": Path(path).stem})
    cs_hash, _, chunks = chunk.materialize(h, budget=12000)
    loaded = chunk.load(cs_hash)
    assert [(c.byte_start, c.byte_end) for c in loaded] == \
           [(c.byte_start, c.byte_end) for c in chunks], "load round-trips the stored pointers"
    empty = [c for c in loaded if not chunk.resolve(c)]
    assert not empty, "every stored pointer resolves to non-empty text"
    print(f"OK — real transcript {Path(path).name[:12]}: {len(chunks)} chunks, "
          f"all pointers resolve, chunkset {cs_hash[:12]}")
