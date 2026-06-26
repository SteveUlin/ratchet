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

from ratchet import blobstore, chunk, config, weave  # noqa: E402

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
