"""weave tests: active-path reconstruction over a hand-built transcript that exercises every hard
structure (turn-split records, parallel-call result fan-in, a rewind, a working compact stitch, a
BROKEN compact stitch bridged by file order, a sidechain, noise), the surrogate poison-pill and
duplicate-tool-id hardenings, and a reconstruct+materialize pass over a real resumed+compacted+
branched transcript. Run: `python tests/test_weave.py` (throwaway dirs)."""
import glob
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-weave-")

from ratchet import blobstore, block, config, weave  # noqa: E402

config.ensure_layout()


# --- synthetic transcript: one record per content block, like Claude Code writes -----------

def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": kw.pop("sc", False)}
    r.update(kw)
    return r

def umsg(content):
    return {"role": "user", "content": content}

def amsg(mid, *blocks):
    return {"role": "assistant", "id": mid, "content": list(blocks)}

def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def tool_result(tid, text):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}

def jsonl(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"

# Append order is meaningful: the tip is the last user/assistant; a rewind's survivor is written
# after the branch it abandons; a broken compact bridges to the highest-ordered earlier turn.
R = [
    rec("u1", None, "user", message=umsg("use jj not git")),                       # 0 root
    rec("a1a", "u1", "assistant", message=amsg("M1", {"type": "thinking", "thinking": "hmm"})),  # 1
    rec("a1b", "a1a", "assistant", message=amsg("M1", {"type": "text", "text": "I'll use jj."})),# 2
    rec("a1c", "a1b", "assistant", message=amsg("M1", tool_use("TUx", "Bash", command="jj st"))),# 3
    rec("sc1", "a1c", "assistant", sc=True, message=amsg("S1", {"type": "text", "text": "SIDECHAIN-NOISE"})),  # 4 dropped
    rec("trx", "a1c", "user", message=tool_result("TUx", "RESULT-X")),             # 5 folded into a1c
    rec("cav", "trx", "user", isMeta=True, message=umsg("CAVEAT-NOISE")),           # 6 isMeta dropped
    rec("a2a", "cav", "assistant", message=amsg("M2", tool_use("TUp1", "Bash", command="ls"))),   # 7 parallel
    rec("a2b", "a2a", "assistant", message=amsg("M2", tool_use("TUp2", "Bash", command="pwd"))),  # 8 parallel
    rec("trp1", "a2a", "user", message=tool_result("TUp1", "RESULT-PARALLEL-1")),  # 9 OFF-spine, must fold by id
    rec("trp2", "a2b", "user", message=tool_result("TUp2", "RESULT-PARALLEL-2")),  # 10 on-spine, folded
    rec("a3", "trp2", "assistant", message=amsg("M3", {"type": "text", "text": "both ran"})),      # 11
    rec("ubad", "a3", "user", message=umsg("actually do X")),                       # 12 abandoned branch
    rec("abad", "ubad", "assistant", message=amsg("MB", {"type": "text", "text": "doing X"})),     # 13 abandoned
    rec("ugood", "a3", "user", message=umsg("no, do Y instead")),                   # 14 rewind survivor
    rec("sysd", "ugood", "system", subtype="turn_duration", message=umsg("NOISE-SYSTEM")),         # 15 on-spine, render-skipped
    rec("agood", "sysd", "assistant", message=amsg("M4", {"type": "text", "text": "doing Y"})),    # 16
    rec("cb1", None, "system", subtype="compact_boundary", logicalParentUuid="agood",
        compactMetadata={"trigger": "manual", "preTokens": 1234}),                  # 17 compact, stitch works
    rec("upost", "cb1", "user", message=umsg("summary of earlier work")),           # 18
    rec("apost", "upost", "assistant", message=amsg("M5", {"type": "text", "text": "continuing"})),# 19
    rec("cb2", None, "system", subtype="compact_boundary", logicalParentUuid="GONE-NEVER-WRITTEN",
        compactMetadata={"trigger": "auto", "preTokens": 5678}),                    # 20 compact, stitch BROKEN
    rec("upost2", "cb2", "user", message=umsg("second continuation")),              # 21
    rec("apost2", "upost2", "assistant", message=amsg("M6", {"type": "text", "text": "final answer"})),  # 22 tip
]
blob = jsonl(R)

# --- 1. active path: exact membership ------------------------------------------------------

spine = weave.active_path(weave.parse(blob))
on = [r["uuid"] for r in spine]
expected = ["u1", "a1a", "a1b", "a1c", "trx", "cav", "a2a", "a2b", "trp2", "a3",
            "ugood", "sysd", "agood", "cb1", "upost", "apost", "cb2", "upost2", "apost2"]
assert on == expected, f"active path wrong:\n got {on}\n exp {expected}"
for dropped in ("sc1", "trp1", "ubad", "abad"):
    assert dropped not in on, f"{dropped} should be off the active path"

# --- 2. render: folding, noise drop, segments, span integrity, determinism -----------------

doc = weave.render(blob)
t = doc.text
assert "RESULT-PARALLEL-1" in t, "off-spine parallel-call result recovered by tool_use_id fold"
assert "RESULT-X" in t and "RESULT-PARALLEL-2" in t, "on-spine results folded"
assert "no, do Y instead" in t and "doing Y" in t, "rewind survivor kept"
for noise in ("doing X", "SIDECHAIN-NOISE", "CAVEAT-NOISE", "NOISE-SYSTEM"):
    assert noise not in t, f"{noise!r} must not be rendered"
assert t.count("[compact]") == 2, "both compact boundaries marked"
assert {turn.segment for turn in doc.turns} == {0, 1, 2}, "three segments across two compacts"
assert "\n\n".join(t[turn.start:turn.end] for turn in doc.turns) == t, "turns tile the cleaned doc"
assert doc.cleaned_hash == blobstore.blob_hash(t), "cleaned_hash pins the rendered bytes"
assert weave.render(blob).cleaned_hash == doc.cleaned_hash, "render is deterministic"

# 2b. surrogate poison-pill: a lone surrogate (Claude Code truncating a pair) must not crash
surr = jsonl([rec("s0", None, "user", message=umsg("go")),
              rec("s1", "s0", "assistant", message=amsg("MS", {"type": "text", "text": "out \udc80 put"}))])
sdoc = weave.render(surr)                                    # would raise UnicodeEncodeError unguarded
assert "out" in sdoc.text and "put" in sdoc.text and "\udc80" not in sdoc.text, "surrogate sanitized"
assert sdoc.cleaned_hash == blobstore.blob_hash(sdoc.text), "hash works on sanitized text"

# 2c. duplicate tool_use_id (a rewound attempt reuses an id): the SURVIVOR's result is folded
dup = jsonl([rec("d0", None, "user", message=umsg("go")),
             rec("d1", "d0", "assistant", message=amsg("MA", tool_use("T", "Bash", command="old"))),
             rec("dro", "d1", "user", message=tool_result("T", "RESULT-OLD")),
             rec("d2", "d0", "assistant", message=amsg("MB", tool_use("T", "Bash", command="new"))),
             rec("drn", "d2", "user", message=tool_result("T", "RESULT-NEW"))])
ddoc = weave.render(dup)
assert "RESULT-NEW" in ddoc.text and "RESULT-OLD" not in ddoc.text, "survivor's result wins on reused id"

# --- 3. materialize the CLEANED blob: content-addressed, lineage-tagged, span-verifiable ----

raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id="synthetic",
                            origin_ref={"project": "p", "session_id": "synthetic",
                                        "cwd": "/p", "git_branch": "main"})
ch, written, _ = weave.materialize(raw_h)
assert written and ch == doc.cleaned_hash, "materialized cleaned hash matches the pure render"
again_h, again_written, _ = weave.materialize(raw_h)
assert again_h == ch and not again_written, "materialize is idempotent (content-addressed)"

m = blobstore.get_meta(ch)
assert m["kind"] == "derived" and m["derived_from"] == raw_h and m["produced_by"] == "weave"
assert m["render_version"] == weave.RENDER_VERSION and m["tags"]["project"] == "p"
assert [x["content_hash"] for x in blobstore.derived_for(raw_h)] == [ch], "lineage links cleaned→raw"
cleaned_text = blobstore.get(ch)
assert "\n\n".join(cleaned_text[turn.start:turn.end] for turn in doc.turns) == cleaned_text, "turns tile the cleaned blob"

# --- 3b. the WeaveBlock path: --all/--source-id, idempotency, the processed marker --------

# A streaming-progress probe so we assert the driver streams per item (not batched to the end).
class _Probe:
    def __init__(self): self.lines = []
    def __call__(self, report, item, key, n_out, cost, *, dry_run=False, errored=False):
        self.lines.append((key, n_out, cost, report.processed, report.skipped, dry_run, errored))

wb = weave.WeaveBlock()
assert wb.name == "weave" and wb.commits_per_item is True
assert wb.params == (("render_version", weave.RENDER_VERSION),), "weave keys on render_version only"
assert isinstance(wb, block.Block), "WeaveBlock satisfies the structural Block protocol"
assert wb.key(raw_h) == raw_h, "the item key IS the raw blob hash"

# first run over just this session: the cleaned blob is already materialized (§3 above), so the
# put_derived is a content-addressed no-op → outputs==0, BUT the item still PROCESSES and gets a
# marker (the done-set must record it so the re-run skips). Use a FRESH raw blob to also assert the
# outputs==1-on-first-write case cleanly.
fresh_blob = jsonl([rec("f0", None, "user", message=umsg("a brand new session")),
                    rec("f1", "f0", "assistant", message=amsg("MF", {"type": "text", "text": "hi there"}))])
fresh_raw, _ = blobstore.ingest(fresh_blob, source_kind="transcript", source_id="weave-block-fresh",
                                origin_ref={"project": "p", "session_id": "weave-block-fresh"})
assert next(blobstore.derived_for(fresh_raw), None) is None, "the fresh raw has no cleaned blob yet"

probe = _Probe()
r1 = block.run(wb, source_id="weave-block-fresh", progress=probe)
assert (r1.examined, r1.processed, r1.skipped, r1.errored) == (1, 1, 0, 0), "first run processes one"
assert r1.outputs == 1, "a never-materialized raw yields one cleaned blob"
assert r1.cost_usd == 0.0 and not r1.stopped_on_budget, "weave is deterministic, cost 0"
assert probe.lines == [(fresh_raw, 1, 0.0, 1, 0, False, False)], "progress streamed exactly one landed item"

# the cleaned blob is real and content-addressed (the block path produces the SAME bytes as render)
fresh_cleaned = weave.render(fresh_blob).cleaned_hash
assert blobstore.has(fresh_cleaned) and blobstore.get_meta(fresh_cleaned)["derived_from"] == fresh_raw
assert blobstore.blob_hash(blobstore.get(fresh_cleaned)) == fresh_cleaned, "byte-exact, content-addressed"

# the processed marker exists and keys on (raw_hash, render_version) — the done-set entry
done = block.done_index("weave", config.ensure_layout())
assert (fresh_raw, weave.RENDER_VERSION) in done, "marker keys on (raw_hash, render_version)"
markers = [d for d in blobstore.decisions_for(fresh_raw, verb="processed", stage="weave")]
assert len(markers) == 1 and markers[0]["render_version"] == weave.RENDER_VERSION, "one weave marker for the raw"
assert markers[0]["n_outputs"] == 1 and markers[0]["target"] == fresh_raw

# re-run: the marker skips it — no re-render, no new marker (idempotent)
probe2 = _Probe()
r2 = block.run(wb, source_id="weave-block-fresh", progress=probe2)
assert (r2.examined, r2.processed, r2.skipped, r2.outputs) == (1, 0, 1, 0), "re-run skips the done item"
assert probe2.lines == [], "a skipped item lands no progress line"
assert len(list(blobstore.decisions_for(fresh_raw, verb="processed", stage="weave"))) == 1, "no duplicate marker"

# bumping render_version flips the done-key → the item re-processes (the re-render-on-logic-change
# semantic weave lacked). Simulate by a block whose params carry a new version.
class _Bumped(weave.WeaveBlock):
    def __init__(self):
        super().__init__()
        self.params = (("render_version", "weave/TEST-BUMP"),)
r3 = block.run(_Bumped(), source_id="weave-block-fresh", progress=None)
assert (r3.processed, r3.skipped) == (1, 0), "a bumped render_version re-processes (new done-key)"
assert (fresh_raw, "weave/TEST-BUMP") in block.done_index("weave", config.ensure_layout())

# --all sweeps every transcript's LATEST raw; --dry-run lists without writing a marker
r4 = block.run(weave.WeaveBlock(), dry_run=True, progress=None)
assert r4.would_process >= 0 and r4.processed == 0, "dry-run processes nothing"
# error isolation: a raw whose render path is forced to raise is counted errored, run continues
class _Boom(weave.WeaveBlock):
    def process(self, raw_hash, *, root, run_id):
        if raw_hash == fresh_raw:
            raise RuntimeError("boom")
        return super().process(raw_hash, root=root, run_id=run_id)
# bump the param so fresh_raw is NOT already-done (else it'd skip before process)
boom = _Boom(); boom.params = (("render_version", "weave/BOOM"),)
r5 = block.run(boom, progress=None)
assert r5.errored >= 1, "the raising item is isolated as errored, run completes"
assert (fresh_raw, "weave/BOOM") not in block.done_index("weave", config.ensure_layout()), \
    "an errored item writes NO marker (retried next run)"

print("OK — WeaveBlock: --all/--source-id, idempotent re-run (marker skip), render_version bump re-does,")
print("     per-item marker keyed (raw_hash, render_version), streaming progress, dry-run, error isolation")


print("OK — active-path (turn-split, parallel fold, rewind, compact stitch + file-order bridge,")
print("     sidechain/noise drop), surrogate + reused-id hardening, render spans, cleaned-blob lineage")


# --- 4. real corpus: reconstruct a genuinely resumed+compacted+branched transcript ---------

def _features(path):
    by_uuid, children, compacts = {}, {}, 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
                    compacts += 1
                u = r.get("uuid")
                if u:
                    by_uuid[u] = r
                    pu = r.get("parentUuid")
                    if pu:
                        children[pu] = children.get(pu, 0) + 1
    except OSError:
        return (0, 0, 0)
    roots = sum(1 for u, r in by_uuid.items()
                if r.get("parentUuid") is None or r.get("parentUuid") not in by_uuid)
    return (roots, sum(1 for v in children.values() if v > 1), compacts)


hard = sorted((p for p in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
               if all(_features(p))), key=os.path.getsize)
if not hard:
    print("SKIP real-corpus check — no resumed+compacted+branched transcript found")
else:
    path = hard[0]
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    h, _ = blobstore.ingest(raw, source_kind="transcript", source_id="real-" + Path(path).stem,
                            origin_ref={"project": Path(path).parent.name, "session_id": Path(path).stem})
    recs = weave.parse(raw)
    spine = weave.active_path(recs)
    doc = weave.render(raw)
    assert len({r["uuid"] for r in spine}) == len(spine), "no record appears twice on the path"

    # KEY property: every tool_use on the path has its result folded (parallel-call safe)
    results = weave._index_tool_results(recs)
    tool_uses = [b["id"] for r in spine if isinstance(r.get("message"), dict)
                 and isinstance(r["message"].get("content"), list)
                 for b in r["message"]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    assert not [tid for tid in tool_uses if tid not in results], "tool results lost on a real path"

    rh, _, _ = weave.materialize(h)
    cleaned = blobstore.get(rh)
    assert "\n\n".join(cleaned[turn.start:turn.end] for turn in doc.turns) == cleaned, "turns tile the cleaned blob"
    multi = len({turn.segment for turn in doc.turns}) > 1
    print(f"OK — real transcript {Path(path).name[:12]} "
          f"({len(recs)} recs → {len(spine)} on path, {len(doc.turns)} turns, "
          f"{len(tool_uses)} tool calls all paired, multi-segment={multi})")
