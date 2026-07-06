"""glean tests: the LLM stage is exercised offline with a FAKE Completer (no network, no API key),
so the suite is deterministic. The load-bearing checks are the trust anchor — the model POINTS at
numbered lines and the system copies their bytes (ADR-0026), so the stored span resolves to real
transcript bytes containing the learning, and an un-resolvable line selection is rejected — plus the
filter, the event schema/id, the append-only store + lineage, and idempotent re-runs.

glean is now a PER-CHUNK block (ADR-0009): the unit of work, persistence, and idempotency is the
single CHUNK (one LLM call), not the chunkset. The chunkset is just the container `items()`
enumerates chunks from. So the marker checks, idempotency, error isolation, and resume are all at
CHUNK granularity — a kill after some chunks keeps them and the re-run does only the rest. A live
CLI smoke test is gated behind RATCHET_LIVE_TEST=1. Run: `python tests/test_glean.py`."""
import glob as _glob
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-glean-")

from ratchet import blobstore, block, chunk, completer, config, glean, sig, weave  # noqa: E402
from ratchet.completer import Completion  # the LLM seam now lives in `completer`  # noqa: E402

config.ensure_layout()


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}

# A transcript whose turn 3 carries a memorable, unique durable line; multibyte filler in every turn
# forces byte≠char offsets, so the byte-based span math is actually under test.
REAL_QUOTE = "always commit with jj, never git"          # a real substring of turn 3 (its owning chunk finds it)
FAKE_QUOTE = "this phrase never appears in the transcript at all"  # no line carries it → dropped (no event)

records = [rec("u0", None, "user", message={"role": "user", "content": "kick off the work please"})]
parent = "u0"
for i in range(6):
    u = f"a{i}"
    body = f"step {i}: " + ("λ wörk ✓ " * 30)             # multibyte → byte offsets ≠ char offsets
    if i == 3:
        body = f"step 3: {REAL_QUOTE} — " + ("λ wörk ✓ " * 30)
    records.append(rec(u, parent, "assistant", message=amsg(f"M{i}", body)))
    parent = u
blob = "\n".join(json.dumps(r) for r in records) + "\n"

raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id="glean-syn",
                            origin_ref={"project": "p", "session_id": "glean-syn"})
cs_hash, _, chunks = chunk.materialize(raw_h, budget=600)
cleaned_hash = chunks[0].cleaned_hash
cleaned = blobstore.get(cleaned_hash)
assert len(chunks) > 1, "small budget yields several chunks (cross-chunk verification matters)"
assert sum(REAL_QUOTE in chunk.resolve(c) for c in chunks) == 1, "the durable line lives in one chunk"

# how many of this chunkset's chunks survive the pre-filter (== one LLM call each, per ADR-0009)
SIGNAL_CHUNKS = sum(glean.has_signal_potential(chunk.resolve(c)) for c in chunks)
ALL_CHUNKS = len(chunks)
assert SIGNAL_CHUNKS >= 2, "several chunks pass the filter (per-chunk verification is exercised)"


class FakeCompleter:
    """Records every call and returns canned candidates — the network seam, replaced. In the line era
    (ADR-0026) the model points at NUMBERED lines, so a canned candidate naming a `quote` is translated
    into a {"from":N,"to":N} line selection by FINDING that text in THIS chunk's numbered prompt: it is
    'found' (and yields an event) ONLY by the chunk that actually contains it — the per-chunk trust
    property, preserved. A `quote` absent from this chunk is dropped (the analogue of the old
    hallucination reject — there is no line to point at). A candidate that already carries `lines` (or
    neither field) passes through verbatim — used to exercise the un-resolvable reject path."""
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        line_of = {}
        for line in user.splitlines():
            num, sep, body = line.partition("| ")
            if sep and num.strip().isdigit():
                line_of[int(num)] = body
        out = []
        for cand in self.candidates:
            cand = dict(cand)
            if "quote" not in cand:
                out.append(cand)                                   # pass-through (already `lines`, or un-resolvable)
                continue
            q = cand.pop("quote")
            hit = next((n for n, body in line_of.items() if q in body), None)
            if hit is not None:
                cand["lines"] = {"from": hit, "to": hit}
                out.append(cand)                                   # found in THIS chunk → a real line selection
            # else: quote not on any line of this chunk → omit (nothing to point at)
        return Completion(text=json.dumps({"events": out}), model="fake", cost_usd=0.002)


class Probe:
    """A Progress-like streaming sink — it satisfies the driver's Progress PROTOCOL (start/tick/stop),
    so the driver drives it exactly as the real bar. One record per item AS IT LANDS, so a test reads
    the live per-chunk feed (the ADR-0009 fix for 'no visible progress'). A SKIPPED item lands no line
    (a skip is a bare counter, not a landed item) — so `len(probe.lines)` counts only the
    processed/errored/dry-run items, matching how piped Progress emits one line per such item."""
    def __init__(self):
        self.lines = []

    def start(self, *, total, todo, already, backlog=0):
        pass

    def tick(self, key, outcome, *, outputs=0, cost=0.0):
        if outcome == "skipped":
            return                                    # a skip is a counter, not a landed per-item line
        self.lines.append({"key": key, "n_out": outputs, "cost": cost,
                           "dry_run": outcome == "dry_run", "errored": outcome == "errored"})

    def stop(self):
        pass


def glean_markers(target=None):
    """Every glean `processed` decision (optionally for one target chunk key) — the per-chunk done set."""
    return list(blobstore.decisions_for(target, verb="processed", stage="glean"))


# --- 1. filter: skip excerpts that cannot carry a durable learning --------------------------

assert not glean.has_signal_potential("ok", min_chars=80), "too small → skip"
assert not glean.has_signal_potential("→ Bash: ls\n  ⤷ a.txt b.txt" * 4), "no human/assistant turn → skip"
assert glean.has_signal_potential(chunk.resolve([c for c in chunks if REAL_QUOTE in chunk.resolve(c)][0]))

# free, no-LLM marker priors: a rendered error and a corrective user turn each raise a surprise cue
assert glean.structural_cues("[assistant]\n→ Bash: pytest\n  ⤷ [error] 1 failed") , "tool error → cue"
assert glean.structural_cues("[user]\nno, don't use git here\n[assistant]\nok"), "corrective user turn → cue"
assert not glean.structural_cues("[assistant]\nhere is a tidy summary of the work"), "no cue when nothing fired"

# --- 2. parse: tolerate the ```json fence the CLI wraps results in --------------------------

fenced = '```json\n{"events": [{"lines": {"from": 1, "to": 1}, "summary": "y", "markers": {"insight": 1}, "confidence": 1}]}\n```'
assert glean.parse_candidates(fenced) == [{"lines": {"from": 1, "to": 1}, "summary": "y", "markers": {"insight": 1}, "confidence": 1}]
assert glean.parse_candidates("not json at all") == [], "malformed output → no candidates, no crash"
assert glean.parse_candidates('{"events": []}') == [], "explicit empty → no candidates"

# --- 3. the trust anchor (ADR-0026): the model POINTS at numbered lines; the system copies their bytes,
#     so evidence is verbatim by construction and cannot be fabricated. Unit of work = ONE CHUNK (ADR-0009).

cleaned_bytes = cleaned.encode("utf-8")
owner = [c for c in chunks if REAL_QUOTE in chunk.resolve(c)][0]

# number_lines: a 1-based numbered presentation + a parallel line→cleaned-blob byte-span map.
numbered, line_spans = glean.number_lines(cleaned_bytes, owner)
disp = numbered.split("\n")
assert disp[0].startswith("1| "), "lines are 1-based, prefixed `N| `"
assert len(line_spans) == len(disp), "one span per displayed line"
for (s, e), shown in zip(line_spans, disp):
    assert cleaned_bytes[s:e].decode("utf-8", "replace") == shown.split("| ", 1)[1], \
        "each line's span copies back to exactly its displayed bytes"

# the line carrying the learning resolves to bytes that CONTAIN it (line-granular: ⊇ the phrase, ADR-0026).
qline = next(i + 1 for i, (s, e) in enumerate(line_spans)
             if REAL_QUOTE in cleaned_bytes[s:e].decode("utf-8", "replace"))
span = glean.resolve_lines({"lines": {"from": qline, "to": qline}}, line_spans, cleaned_bytes)
assert span is not None and REAL_QUOTE in cleaned_bytes[span[0]:span[1]].decode(), "pointed line's bytes carry the learning"
assert span[0] != cleaned.find(REAL_QUOTE), "offsets are BYTES, not chars (multibyte content present)"

# tolerance (recall-first): bare int / [i,j] accepted; a reversed range swaps; out-of-range CLAMPS into the chunk.
assert glean.resolve_lines({"lines": qline}, line_spans, cleaned_bytes) == span, "a bare int == that single line"
assert glean.resolve_lines({"lines": [1, qline]}, line_spans, cleaned_bytes) == (line_spans[0][0], line_spans[qline - 1][1])
assert glean.resolve_lines({"lines": {"from": qline, "to": 1}}, line_spans, cleaned_bytes) \
    == (line_spans[0][0], line_spans[qline - 1][1]), "a reversed range is swapped, not dropped"
assert glean.resolve_lines({"lines": {"from": 1, "to": 10**9}}, line_spans, cleaned_bytes) \
    == (line_spans[0][0], line_spans[-1][1]), "an over-range end clamps to the last line (a near-miss still yields evidence)"

# only a TRULY unreadable selection is rejected — no fabricated evidence, but a slip is never punished.
assert glean.resolve_lines({"lines": "nope"}, line_spans, cleaned_bytes) is None, "non-numeric selection → None"
assert glean.resolve_lines({}, line_spans, cleaned_bytes) is None, "no line selection → None"
assert glean.resolve_lines({"lines": {"from": 1, "to": 1}}, [], cleaned_bytes) is None, "empty chunk → None"
assert glean.resolve_lines({"lines": {"from": 1, "to": 1}}, [(2, 2)], b"abcd") is None, "empty span → None"
assert glean.resolve_lines({"lines": {"from": 1, "to": 1}}, [(0, 3)], b"   x") is None, "whitespace-only selection → None"


# the REAL quote is found ONLY in its owning chunk (→ one event); the second candidate carries an
# un-resolvable `lines` selection in EVERY chunk (→ a deterministic reject), exercising both paths.
fake = FakeCompleter([
    {"quote": REAL_QUOTE, "summary": "Commit with jj, never git.",
     "markers": {"insight": 0.8, "surprise": 0.1}, "confidence": 0.9},
    {"lines": "not-a-line-ref", "summary": "ungrounded",
     "markers": {"insight": 0.5}, "confidence": 0.5},
])
probe = Probe()
blk = glean.GleanBlock(fake, model="fake", targets=[cs_hash])
report = block.run(blk, progress=probe)

# per-chunk granularity: one call per SURVIVING chunk; only the owning chunk finds the needle → one event.
assert fake.calls == SIGNAL_CHUNKS, "one LLM call per signal-bearing chunk (the chunk is the unit now)"
assert report.outputs == 1 == blk.events, "only the chunk whose lines carry the learning becomes an event"
assert report.processed == ALL_CHUNKS, "EVERY chunk processed (filter-skips included, as 0-output items)"
assert blk.rejected == SIGNAL_CHUNKS, "the un-resolvable candidate in every signal chunk is rejected, deterministically"

# streaming progress landed ONE line per processed chunk; only the owning chunk's line reports an event.
assert len(probe.lines) == ALL_CHUNKS and all(not l["errored"] for l in probe.lines), "a live line per chunk"
assert sum(l["n_out"] for l in probe.lines) == 1, "exactly the owning chunk's line reports an event"

ev = glean.load_events()[0]
sp = ev["evidence"][0]
resolved = cleaned_bytes[sp["byte_start"]:sp["byte_end"]].decode()
assert REAL_QUOTE in resolved, "the stored span resolves to real bytes that contain the learning (line-granular)"
assert sp["byte_start"] != cleaned.find(REAL_QUOTE), "offsets are bytes, not chars (multibyte content present)"
assert ev["id"] == glean.event_id(cleaned_hash, sp["byte_start"], sp["byte_end"]), "id = sha256(cleaned+span)[:12]"
assert ev["cleaned_hash"] == cleaned_hash and "quote" not in ev, "event points into the cleaned blob; never copies its text"
assert set(ev["markers"]) == set(glean.MARKER_KINDS) and ev["markers"]["insight"] == 0.8, "markers scored per kind"
assert "status" not in ev, "state is a decision now (ADR-0007) — no in-record status field"
# the RELEVANCE marker (4b/ADR-0019): the fake gave no verdict → coerced to `novel` (recall-safe) and stored
assert ev["relevance"] == "novel", "an event with no model relevance defaults to novel (recall-safe, 4b)"
assert glean.event_content(ev)["relevance"] == "novel", "relevance is part of the stored content projection"

# the dream-v3 §2.1 stamps (S1): every NEW event carries subject_key + stmt_sig, both deterministic
# (no extra LLM call) — resolve reads identity features off the blob instead of recomputing.
sk = ev["subject_key"]
assert set(sk) == {"repo", "files"} and isinstance(sk["files"], list), "subject_key = {repo, files}"
ss = ev["stmt_sig"]
assert set(ss) == {"simhash", "shingles", "entropy"}, "stmt_sig = {simhash, shingles, entropy}"
assert ss == sig.stmt_sig(ev["summary"]), "stmt_sig signs the STORED summary (recomputable on read)"
assert ss["shingles"] == sorted(ss["shingles"]), "shingles persist sorted (stable canonical-json)"
proj = glean.event_content(ev)
assert proj["subject_key"] == sk and proj["stmt_sig"] == ss, "the stamps ride the stored projection when present"
# an OLD-shape event (pre-stamp blob) still folds fine and projects BYTE-IDENTICALLY — no spurious version
old = {k: v for k, v in ev.items() if k not in ("subject_key", "stmt_sig")}
old_proj = glean.event_content(old)
assert "subject_key" not in old_proj and "stmt_sig" not in old_proj, "absent stamps stay absent (compute-on-read)"
assert blobstore.canonical_json(old_proj) == blobstore.canonical_json(old), "old blobs project unchanged (byte-compatible)"

# untrusted-field hygiene (build_event): marker scores clamp to [0,1], unknown keys drop, missing → 0
dirty = glean.build_event({"summary": "x", "markers": {"insight": 9, "bogus": 1}, "confidence": 9},
                          owner, span, model="fake", run_id="r")
assert dirty["markers"] == {"surprise": 0.0, "insight": 1.0, "research": 0.0}, "markers coerced/clamped, unknowns dropped"
assert dirty["confidence"] == 1.0, "confidence clamped to [0,1]"
assert dirty["producer"]["stage"] == "glean" and dirty["producer"]["prompt_version"] == glean.PROMPT_VERSION

# the RELEVANCE marker coercion (4b/ADR-0019): a known verdict is kept; an unknown/missing one coerces to `novel`
assert glean.clean_relevance("contradicts") == "contradicts" and glean.clean_relevance("known") == "known"
assert glean.clean_relevance("wat") == "novel" and glean.clean_relevance(None) == "novel", "unknown/missing → novel"
assert dirty["relevance"] == "novel", "a candidate with no relevance field defaults to novel (recall-safe)"
assert glean.build_event({"relevance": "contradicts"}, owner, span,
                         model="fake", run_id="r")["relevance"] == "contradicts", "a valid verdict is kept verbatim"

print("OK — trust anchor (lines): the model points at numbered lines, the system copies their bytes; "
      "spans resolve to real evidence, un-resolvable selections rejected")
print("     rejected deterministically; one LLM call PER CHUNK; event is a pointer (no copied text).")

# --- 4. a processed marker PER CHUNK (signal-bearing AND filter-skipped 0-output) ------------

# (ADR-0009) the done-set is exact ONLY if every examined chunk gets a marker, including the ones the
# pre-filter skipped (0 events, 0 cost) — otherwise a skipped chunk would be re-examined forever.
markers = glean_markers()
assert len(markers) == ALL_CHUNKS, "one processed decision PER CHUNK (not per chunkset)"
chunk_keys = {glean.chunk_key(c) for c in chunks}
assert {m["target"] for m in markers} == chunk_keys, "each marker targets a CHUNK key, not the chunkset"
# the chunk key is provably disjoint from event source-ids (the :chunk suffix) — no collision
assert ev["id"] not in chunk_keys, "chunk-marker target space is disjoint from event source-ids"
# the owning chunk's marker reports 1 output; every other chunk's marker reports 0 (filter or no-quote)
owner_key = glean.chunk_key(owner)
owner_marker = [m for m in markers if m["target"] == owner_key][0]
assert owner_marker["n_outputs"] == 1, "the owning chunk's marker records its 1 event"
assert sum(m["n_outputs"] for m in markers) == 1, "exactly one chunk produced an event; the rest are 0-output"
# every marker carries the (prompt_version, model) key (top-level audit + the ordered params list)
for m in markers:
    assert m["prompt_version"] == glean.PROMPT_VERSION and m["model"] == "fake", "marker carries the key"
    assert m["params"] == [["prompt_version", glean.PROMPT_VERSION], ["model", "fake"]], "ordered done-key params"
# per-chunk audit fields (n_rejected/n_calls/cleaned_hash) live on the marker for forensics
assert owner_marker["n_calls"] == 1 and owner_marker["cleaned_hash"] == cleaned_hash, "per-chunk audit recorded"

# the marker is a real decision blob (source_id == its own content_hash, prev=None, never re-versioned)
d_meta = blobstore.get_meta(owner_marker["content_hash"])
assert d_meta["source_kind"] == "decision" and d_meta["source_id"] == owner_marker["content_hash"]
assert d_meta["prev"] is None, "decisions are never re-versioned (prev=None)"

# THE FILTER-SKIP 0-OUTPUT MARKER (ADR-0009): a chunk the pre-filter skips STILL gets a marker, with
# 0 calls / 0 events, so the done-set is exact (else it would be re-examined forever). A dedicated tiny
# transcript (a 'go' user turn under a small budget) forces a sub-80-char chunk the filter skips.
recs_fs = [rec("u0", None, "user", message={"role": "user", "content": "go"}),
           rec("a0", "u0", "assistant",
               message=amsg("M0", "always commit with jj, never git " + ("λ " * 40)))]
blob_fs = "\n".join(json.dumps(r) for r in recs_fs) + "\n"
raw_fs, _ = blobstore.ingest(blob_fs, source_kind="transcript", source_id="glean-fs",
                             origin_ref={"session_id": "glean-fs"})
cs_fs, _, fschunks = chunk.materialize(raw_fs, budget=100)
skipped_chunk = [c for c in fschunks if not glean.has_signal_potential(chunk.resolve(c))]
assert skipped_chunk, "the tiny 'go' turn is a filter-skipped chunk (a 0-call, 0-output marker case)"
# an empty-events completer keeps this section's event store unchanged (it tests MARKERS, not events) —
# so §5's "one current event" count stays exact; the signal chunk here just yields 0 events.
fs_fake = FakeCompleter([])
fs_rep = block.run(glean.GleanBlock(fs_fake, model="fs", targets=[cs_fs]), progress=None)
assert fs_rep.processed == len(fschunks), "EVERY chunk processed, including the filter-skipped one"
fs_markers = {m["target"]: m for m in glean_markers() if m["target"] in {glean.chunk_key(c) for c in fschunks}}
assert len(fs_markers) == len(fschunks), "a marker for every chunk (filter-skip included) → exact done-set"
skip_marker = fs_markers[glean.chunk_key(skipped_chunk[0])]
assert skip_marker["n_calls"] == 0 and skip_marker["n_outputs"] == 0 and skip_marker["n_rejected"] == 0, \
    "the filter-skipped chunk's marker is empty (no LLM call, no events, no rejections)"
# re-running skips the filter-skipped chunk with NO LLM call (the 0-output marker did its job)
before_fs = fs_fake.calls
fs_again = block.run(glean.GleanBlock(fs_fake, model="fs", targets=[cs_fs]), progress=None)
assert fs_again.skipped == len(fschunks) and fs_fake.calls == before_fs, \
    "the re-run skips the filter-skipped chunk too (the 0-output marker keeps it out of the to-do)"

# events ARE blobs (versioned by event_id), lineage holds (unchanged by ADR-0009 — marker granularity
# changed, not the event blob model)
by_kind = blobstore.latest_by_kind("event")
assert ev["id"] in by_kind, "the event's source_id (event_id) is in latest_by_kind('event')"
ev_meta = blobstore.get_meta(by_kind[ev["id"]])
assert ev_meta["source_kind"] == "event" and ev_meta["kind"] == "raw", "meta.source_kind == event, kind == raw"
assert ev_meta["source_id"] == ev["id"] and ev_meta["prev"] is None, "first version: source_id == event_id, prev None"
assert ev_meta["origin_ref"]["stage"] == "glean" and ev_meta["origin_ref"]["chunkset_hash"] == cs_hash, \
    "provenance (incl. the parent chunkset for lineage) mirrored into meta.origin_ref"
assert blobstore.get(by_kind[ev["id"]]) == blobstore.canonical_json(ev), "content is canonical-json of the record"
assert blobstore.get_meta(cleaned_hash)["derived_from"] == raw_h, "event.cleaned_hash → derived_from → raw"

# the driver's done_index agrees with the on-disk markers (the idempotency key the next run consults)
done = block.done_index("glean", config.data_root())
assert (owner_key, glean.PROMPT_VERSION, "fake") in done, "done_index derives the (chunk, prompt, model) key"
assert all((glean.chunk_key(c), glean.PROMPT_VERSION, "fake") in done for c in chunks), "every chunk is done"

print("OK — a processed decision PER CHUNK (filter-skips get 0-output markers so the done-set is exact);")
print("     events versioned by event_id, content-addressed lineage to raw, marker keys on the chunk.")

# --- 5. idempotency + resume: re-run skips at CHUNK granularity; a kill keeps committed chunks --

main_keys = {glean.chunk_key(c) for c in chunks}
main_markers_before = len([m for m in glean_markers() if m["target"] in main_keys])
before = fake.calls
probe2 = Probe()
blk2 = glean.GleanBlock(fake, model="fake", targets=[cs_hash])
again = block.run(blk2, progress=probe2)
assert fake.calls == before, "every chunk has a marker for the key → all skipped, zero LLM calls"
assert again.skipped == ALL_CHUNKS and again.processed == 0, "re-run skips at chunk granularity, does nothing new"
assert probe2.lines == [], "a skipped item lands NO progress line (a skip is not a landed item)"
assert len(glean.load_events()) == 1, "no duplicate event source on re-run (latest_by_kind folds by event_id)"
assert len([m for m in glean_markers() if m["target"] in main_keys]) == main_markers_before, \
    "a skipped re-run writes no new markers for the source (count unchanged)"

# RESUME: a completer that raises on the Nth signal chunk (a 'kill' mid-run) commits the chunks before
# it and writes NO marker for the failed one; the re-run does ONLY the rest. Use a fresh source so
# nothing is pre-done.
raw_r, _ = blobstore.ingest(blob.replace("kick off", "kick off resume"), source_kind="transcript",
                            source_id="glean-resume", origin_ref={"session_id": "glean-resume"})
cs_r, _, rchunks = chunk.materialize(raw_r, budget=600)
r_signal = sum(glean.has_signal_potential(chunk.resolve(c)) for c in rchunks)

class DieOnNth:
    """Succeeds on the first (n-1) signal chunks, then raises — simulating a kill/outage mid-backfill."""
    def __init__(self, n, candidates):
        self.n, self.candidates, self.calls = n, candidates, 0
    def __call__(self, system, user):
        self.calls += 1
        if self.calls >= self.n:
            raise RuntimeError("simulated kill mid-run")
        return Completion(text=json.dumps({"events": self.candidates}), model="fake", cost_usd=0.001)

die = DieOnNth(r_signal, [{"quote": REAL_QUOTE, "summary": "ok", "markers": {"insight": 0.5}, "confidence": 0.8}])
partial = block.run(glean.GleanBlock(die, model="resume", targets=[cs_r]), progress=None)
assert partial.errored == 1, "exactly the in-flight chunk errored (isolated; the run did not abort)"
assert partial.processed == ALL_CHUNKS - 1, "every other chunk (signal + filter-skip) committed"
markers_r = len(glean_markers())  # whole-store count; the failed chunk has NO marker
# count markers for THIS source's chunks only
r_keys = {glean.chunk_key(c) for c in rchunks}
r_markers = [m for m in glean_markers() if m["target"] in r_keys]
assert len(r_markers) == ALL_CHUNKS - 1, "the killed chunk has NO marker — it is retried next run"

# the re-run does ONLY the one missing chunk; the committed ones are skipped (the resume guarantee)
recover = FakeCompleter([{"quote": REAL_QUOTE, "summary": "recovered", "markers": {"insight": 0.6}, "confidence": 0.8}])
blk_rec = glean.GleanBlock(recover, model="resume", targets=[cs_r])
resumed = block.run(blk_rec, progress=None)
assert resumed.processed == 1 and recover.calls == 1, "the re-run does ONLY the previously-killed chunk"
assert resumed.skipped == ALL_CHUNKS - 1, "the already-committed chunks are skipped on resume"
assert len([m for m in glean_markers() if m["target"] in r_keys]) == ALL_CHUNKS, "now every chunk has a marker"

# even forced re-extraction (a fresh model key) of a byte-IDENTICAL event no-ops the event VERSION:
# the span-derived event_id is the source_id and the canonical-json bytes are unchanged => same blob.
ev_versions_before = sum(1 for m in blobstore.iter_meta()
                         if m.get("source_kind") == "event" and m.get("source_id") == ev["id"])
fake_same = FakeCompleter([{"quote": REAL_QUOTE, "summary": "Commit with jj, never git.",
                            "markers": {"insight": 0.8, "surprise": 0.1}, "confidence": 0.9},
                           {"quote": FAKE_QUOTE, "summary": "A hallucinated claim.",
                            "markers": {"insight": 0.9}, "confidence": 0.9}])
block.run(glean.GleanBlock(fake_same, model="fake-same", targets=[cs_hash]), progress=None)
ev_versions_after = sum(1 for m in blobstore.iter_meta()
                        if m.get("source_kind") == "event" and m.get("source_id") == ev["id"])
assert ev_versions_after == ev_versions_before, "a byte-identical re-extraction is a no-op (no spurious version)"

# a CHANGED extraction (different summary) for the SAME event_id is a NEW VERSION, prev-linked, latest wins
fake_changed = FakeCompleter([{"quote": REAL_QUOTE, "summary": "Refined: commit with jj, never git.",
                               "markers": {"insight": 0.8, "surprise": 0.1}, "confidence": 0.95}])
block.run(glean.GleanBlock(fake_changed, model="fake-changed", targets=[cs_hash]), progress=None)
latest_h = blobstore.latest_by_kind("event")[ev["id"]]
latest_ev = json.loads(blobstore.get(latest_h))
assert latest_ev["summary"].startswith("Refined:"), "the latest version is the newer (changed) extraction"
assert blobstore.get_meta(latest_h)["prev"] is not None, "the new version prev-links to a prior version"
assert sum(1 for e in glean.load_events() if e["id"] == ev["id"]) == 1, "load_events returns ONE current event per id"

# a different model is a different chunk-marker key → it re-extracts over the same frozen chunks
fake2 = FakeCompleter([{"quote": REAL_QUOTE, "summary": "Commit with jj.", "signal": "preference", "confidence": 0.9}])
blk_v2 = glean.GleanBlock(fake2, model="fake-v2", targets=[cs_hash])
rerun = block.run(blk_v2, progress=None)
assert fake2.calls == SIGNAL_CHUNKS and blk_v2.events == 1, "bumping the model re-extracts every chunk (per prompt+model)"
assert rerun.skipped == 0, "the new model key has no markers yet → nothing skipped"

print("OK — per-chunk idempotency + RESUME: a kill keeps committed chunks (no marker for the in-flight one);")
print("     the re-run does ONLY the rest. Re-extraction = a new version, latest wins; identical = no-op.")

# --- 6. budget stop is a clean exit (now BETWEEN chunks) -------------------------------------

# a fresh source so nothing is pre-done; max_usd below the first call's cost → stop before any call
raw2, _ = blobstore.ingest(blob.replace("kick off", "kick off again"), source_kind="transcript",
                           source_id="glean-syn2", origin_ref={"session_id": "glean-syn2"})
cs2, _, c2chunks = chunk.materialize(raw2, budget=600)
fake3 = FakeCompleter([{"quote": REAL_QUOTE, "summary": "x", "markers": {"insight": 1}, "confidence": 1}])
stopped = block.run(glean.GleanBlock(fake3, model="fake3", targets=[cs2]), max_usd=0.0)
assert stopped.stopped_on_budget and fake3.calls == 0, "max_usd stops the run cleanly before spending"
assert not [m for m in glean_markers() if m["target"] in {glean.chunk_key(c) for c in c2chunks}], \
    "a budget stop before any chunk writes no markers (the source is retried next run)"

print("OK — budget ceiling stops a run cleanly between chunks, before overspend.")


# --- 7. adversarial hardening: flaky completer isolated PER CHUNK, absent blob retried -------

# non-finite / out-of-range scores never reach the store (NaN clamps via argument-order in _clean_score)
assert glean._clean_score(float("nan")) == 0.0 and glean._clean_score(float("inf")) == 0.0
assert glean._clean_score("NaN") == 0.0 and glean._clean_score(1e400) == 0.0
assert glean._clean_score(float("nan"), 0.5) == 0.5, "non-finite falls back to the default, not 1.0"
nanev = glean.build_event({"summary": "x", "markers": {"surprise": float("nan")},
                           "confidence": float("inf")}, owner, span, model="fake", run_id="r")
assert nanev["markers"]["surprise"] == 0.0 and nanev["confidence"] == 0.5, "NaN/inf scrubbed from the event"

# (whitespace-only / empty / un-resolvable line selections → rejected: covered by §3's resolve_lines checks)

# a completer that raises on EVERY call: each signal chunk errors INDEPENDENTLY; the run completes and
# NO marker is written for any failed chunk — every signal chunk is retried next run (no silent loss).
class Boom:
    def __init__(self): self.calls = 0
    def __call__(self, system, user):
        self.calls += 1
        raise ValueError("simulated binding failure")

raw_b, _ = blobstore.ingest(blob.replace("kick off", "kick off boom"), source_kind="transcript",
                            source_id="glean-boom", origin_ref={"session_id": "glean-boom"})
cs_b, _, bchunks = chunk.materialize(raw_b, budget=600)
b_signal = sum(glean.has_signal_potential(chunk.resolve(c)) for c in bchunks)
b_keys = {glean.chunk_key(c) for c in bchunks}
boom = Boom()
rep_b = block.run(glean.GleanBlock(boom, model="boom", targets=[cs_b]), progress=None)
assert boom.calls == b_signal and rep_b.errored == b_signal, "every signal chunk's call fails and is isolated"
assert rep_b.outputs == 0, "no events from a fully-failing completer"
# the FILTER-SKIPPED chunks still got their 0-output markers (process didn't raise for them); the
# signal chunks (which raised) did NOT — so they retry, the skipped ones do not.
b_markers = [m for m in glean_markers() if m["target"] in b_keys]
assert len(b_markers) == len(bchunks) - b_signal, "only the filter-skipped chunks are marked done; signal chunks retry"
assert all((c_key, glean.PROMPT_VERSION, "boom") not in block.done_index("glean", config.data_root())
           for c_key in {glean.chunk_key(c) for c in bchunks if glean.has_signal_potential(chunk.resolve(c))}), \
    "no signal chunk is falsely marked done"

# recovery: the retried signal chunks succeed once the completer works (the filter-skips stay skipped)
recovered = block.run(glean.GleanBlock(
    FakeCompleter([{"quote": REAL_QUOTE, "summary": "ok", "markers": {"insight": 0.5}, "confidence": 0.8}]),
    model="boom", targets=[cs_b]), progress=None)
assert recovered.processed == b_signal and recovered.skipped == len(bchunks) - b_signal, \
    "the retried signal chunks recover; the already-marked filter-skips are skipped"
assert recovered.outputs == 1, "the recovered run extracts the durable line"

# an absent (TTL-reclaimed) cleaned blob → items() still surfaces the chunk pointers, but process()
# raises FileNotFoundError on the slice → the chunk is errored, retried, never a crashed run, never done.
raw_m, _ = blobstore.ingest(blob.replace("kick off", "kick off gone"), source_kind="transcript",
                            source_id="glean-gone", origin_ref={"session_id": "glean-gone"})
cs_m, _, mchunks = chunk.materialize(raw_m, budget=600)
m_signal = sum(glean.has_signal_potential(chunk.resolve(c)) for c in mchunks)
m_keys = {glean.chunk_key(c) for c in mchunks}
blobstore._paths(mchunks[0].cleaned_hash, config.data_root())[0].unlink()   # reclaim the cleaned blob
rep_m = block.run(glean.GleanBlock(FakeCompleter([]), model="gone", targets=[cs_m]), progress=None)
# EVERY chunk errors now: the slice of the absent cleaned blob raises before the filter even runs
assert rep_m.errored == len(mchunks), "absent cleaned blob → every chunk errored (slice raises pre-filter)"
assert not [m for m in glean_markers() if m["target"] in m_keys], "no chunk of the gone source is marked done"

print("OK — adversarial: flaky completer isolated PER CHUNK + run survives (signal chunks retried, not")
print("     falsely done), absent cleaned blob → all chunks errored + retried, non-finite/whitespace scrubbed.")


# --- 8. the glean.run shim: dream/review setup keeps working over the per-chunk driver -------

# dream/review call glean.run([cs], fake, model='fake') purely to populate the event store; the shim
# builds a GleanBlock, runs the driver, and returns an object exposing .events/.skipped that mirror
# the SAME block.Report the driver populated (no parallel construction → no desync).
raw_s, _ = blobstore.ingest(blob.replace("kick off", "kick off shim"), source_kind="transcript",
                            source_id="glean-shim", origin_ref={"session_id": "glean-shim"})
cs_s, _, schunks = chunk.materialize(raw_s, budget=600)
s_signal = sum(glean.has_signal_potential(chunk.resolve(c)) for c in schunks)
fake_s = FakeCompleter([{"quote": REAL_QUOTE, "summary": "shim", "markers": {"insight": 0.7}, "confidence": 0.9}])
shim = glean.run([cs_s], fake_s, model="fake")
assert shim.events == 1 and fake_s.calls == s_signal, "the shim extracts via the per-chunk driver"
assert shim.skipped == 0, "fresh source → nothing skipped"
shim2 = glean.run([cs_s], fake_s, model="fake")    # idempotent re-run through the shim
assert shim2.events == 0 and shim2.skipped == len(schunks), "the shim re-run skips at chunk granularity"
assert fake_s.calls == s_signal, "no new LLM calls on the shim's idempotent re-run"

print("OK — the glean.run shim drives the per-chunk block (dream/review setup untouched), idempotent.")


# --- 8b. priority(): the amortization queue orders by likely yield from the chunk POINTER alone ----
# A --limit/--max-usd-capped tick must glean the RICHEST chunks first so the backlog drains best-first.
# priority() reads ONLY the chunk's free structural cues (kinds + turn span) — never the content — so
# prioritizing adds no per-tick O(bytes) scan (the overhead amortization is meant to avoid).
def _ci(kinds, turns):
    return glean.ChunkItem(chunk=chunk.Chunk(cleaned_hash="h", byte_start=0, byte_end=10,
                                             turn_start=0, turn_end=turns, segment=0, kinds=kinds),
                           chunkset_hash="cs")
pb = glean.GleanBlock(FakeCompleter([]), model="prio")
rich = _ci(["user", "assistant", "tool"], 3)    # a real exchange WITH the human → highest yield
useronly = _ci(["user"], 1)                      # the human present, even tersely → still high
work = _ci(["assistant", "tool"], 8)             # assistant + tools, no human steer
dump = _ci(["tool"], 40)                          # a long tool-output monologue → bytes-heavy, low yield
assert pb.priority(rich) > pb.priority(useronly) > pb.priority(work) > pb.priority(dump), \
    f"user-bearing + diverse ranks first: {[pb.priority(c) for c in (rich, useronly, work, dump)]}"
assert pb.priority(useronly) > pb.priority(dump), "a terse human turn outranks a long tool dump (NOT length)"
assert pb.priority(work) == pb.priority(_ci(["assistant", "tool"], 8)), "priority is pure in the pointer"
print("OK — priority: capped tick drains best-first by free pointer cues (user-presence > diversity > turns, not bytes).")


# --- 8c. the relevance marker flows completer → store, coerced (4b/ADR-0019) ----------------
# glean asks the model to judge each event's novelty vs the concept digest ("what we already know",
# injected into the prompt — empty here, so the fake just echoes a verdict) and STORES it on the event.
# The trust anchor is unchanged (only the REAL_QUOTE candidate verifies): we assert a VALID verdict is
# stored verbatim and an UNKNOWN one coerces to `novel` end-to-end (the recall-safe default).
def _store_relevance(tag, verdict):
    raw_x, _ = blobstore.ingest(blob.replace("kick off", f"kick off {tag}"), source_kind="transcript",
                                source_id=f"glean-{tag}", origin_ref={"session_id": f"glean-{tag}"})
    cs_x, _, xchunks = chunk.materialize(raw_x, budget=600)
    fx = FakeCompleter([{"quote": REAL_QUOTE, "summary": "x", "relevance": verdict,
                         "markers": {"surprise": 0.5}, "confidence": 0.9}])
    block.run(glean.GleanBlock(fx, model=tag, targets=[cs_x]), progress=None)
    evs = [e for e in glean.load_events() if e["cleaned_hash"] == xchunks[0].cleaned_hash]
    assert len(evs) == 1, f"the durable line is exactly one event in {tag}'s source"
    return evs[0]["relevance"]

assert _store_relevance("contra", "contradicts") == "contradicts", "a valid verdict is stored verbatim"
assert _store_relevance("junkrel", "not-a-verdict") == "novel", "an unknown verdict coerces to novel in the store"
print("OK — relevance (4b): a valid verdict stores verbatim; an unknown one coerces to novel (recall-safe),")
print("     end-to-end through completer → verify → build_event → ingest → load_events.")


# --- 8d. age(): aging reads the RAW transcript's fetched_at through the cleaned lineage (ADR-0021) ---
# weave-derived cleaned blobs carry `created_at`, never `fetched_at` — so reading the stamp off the
# CLEANED meta finds None on every chunk and flattens all ages to 0.0 ("fresh"), leaving `--priority
# aging`'s anti-starvation term inert. age() must hop cleaned → derived_from → raw and use the
# transcript's own arrival stamp (recompute-on-read, ADR-0013: the render's age is its source's age).
from datetime import datetime, timedelta, timezone  # noqa: E402

OLD_STAMP = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
raw_old, _ = blobstore.ingest(blob.replace("kick off", "kick off aged"), source_kind="transcript",
                              source_id="glean-aged", origin_ref={"session_id": "glean-aged"},
                              fetched_at=OLD_STAMP)
cs_old, _, ochunks = chunk.materialize(raw_old, budget=600)
# the regression premise, pinned: the weave-derived cleaned blob carries NO fetched_at — a direct
# cleaned-meta read could only ever see None, so every chunk degraded to age 0.0.
assert "fetched_at" not in blobstore.get_meta(ochunks[0].cleaned_hash), \
    "cleaned (derived) meta has no fetched_at — derived blobs stamp created_at only"
ablk = glean.GleanBlock(FakeCompleter([]), model="aged", targets=[cs_old])
aitems = list(ablk.items(config.data_root()))          # items() captures _root, as the driver does
age_old = ablk.age(aitems[0])
assert 9.99 < age_old < 10.01, f"a 10-day-old raw transcript reports ~10.0d via the lineage hop: {age_old}"
assert ablk._age_cache[ochunks[0].cleaned_hash] == OLD_STAMP, "the hop's stamp is cached per cleaned blob"
assert all(abs(ablk.age(i) - age_old) < 0.01 for i in aitems), "chunks sharing the cleaned blob share the age"

# the documented degradation holds THROUGH the hop: no stamp anywhere → 0.0, never a raise.
# (a) a raw meta missing its stamp (legacy/hand-written snapshot): strip fetched_at in this temp store.
raw_ns, _ = blobstore.ingest(blob.replace("kick off", "kick off nostamp"), source_kind="transcript",
                             source_id="glean-nostamp", origin_ref={"session_id": "glean-nostamp"})
cs_ns, _, nschunks = chunk.materialize(raw_ns, budget=600)
_mp = blobstore._paths(raw_ns, config.data_root())[1]
_mns = json.loads(_mp.read_text(encoding="utf-8"))
_mns.pop("fetched_at")
_mp.write_text(json.dumps(_mns), encoding="utf-8")
nblk = glean.GleanBlock(FakeCompleter([]), model="nostamp", targets=[cs_ns])
nitems = list(nblk.items(config.data_root()))
assert nblk.age(nitems[0]) == 0.0, "raw meta without fetched_at → age 0.0 (fresh), the documented degrade"
# (b) broken lineage: a chunk whose cleaned blob is unknown to the store → 0.0, never raises.
ghost = glean.ChunkItem(chunk=chunk.Chunk(cleaned_hash="0" * 64, byte_start=0, byte_end=1,
                                          turn_start=0, turn_end=1, segment=0, kinds=["user"]),
                        chunkset_hash="cs")
assert nblk.age(ghost) == 0.0, "absent cleaned meta → age 0.0, never raises"
print("OK — age (ADR-0021): the lineage hop reads the RAW transcript's fetched_at (~10.0d pinned),")
print("     cached per cleaned blob; missing stamp or broken lineage degrades to 0.0, never raises.")


# --- 8e. priority(): the VALID-TIME recency bonus — mine the owner's recent life first (§8d's twin) ---
# The structural score is date-blind: under a backfill every transcript ARRIVES the same week (the
# fetched_at clock age() reads is one flat cohort), but the sessions' `origin_ref.mtime` dates spread
# over months. priority() must add W_RECENT · 0.5^(mtime_age / RECENT_HALF_LIFE_DAYS) read through the
# SAME cleaned→derived_from→raw hop — and a chunk with NO readable date must earn NO bonus (an unknown
# date never outranks known-recent material; contrast age(), where missing → "fresh" is the safe side).
# Four same-SHAPE sources (the 6-char tags keep byte lengths — hence chunk boundaries and structural
# scores — identical) differ ONLY in their origin mtime, so every score delta below is pure recency.
_now_dt = datetime.now(timezone.utc)

def _vt_source(tag, mtime):
    origin = {"session_id": f"glean-{tag}"}
    if mtime is not None:
        origin["mtime"] = mtime
    raw_v, _ = blobstore.ingest(blob.replace("kick off", f"kick {tag}"), source_kind="transcript",
                                source_id=f"glean-{tag}", origin_ref=origin)
    cs_v, _, vchunks = chunk.materialize(raw_v, budget=600)
    return cs_v, vchunks

cs_vn, ch_vn = _vt_source("vt-now", _now_dt.isoformat())                        # a session from today
cs_v6, ch_v6 = _vt_source("vt-60d", (_now_dt - timedelta(days=60)).isoformat())  # one half-life ago
cs_v2, ch_v2 = _vt_source("vt-240", (_now_dt - timedelta(days=240)).isoformat()) # four half-lives ago
cs_vx, ch_vx = _vt_source("vt-non", None)                                        # undated (no origin mtime)

# STRICT clock ("valid") isolates the valid-time decay curve: the undated fixture is the
# pure-structural baseline here, which the default fallback clock would (correctly) date by arrival.
# The RECENT_CLOCK policies themselves are pinned right below the curve.
vblk = glean.GleanBlock(FakeCompleter([]), model="vt", targets=[cs_vn, cs_v6, cs_v2, cs_vx],
                        recent_clock="valid")
vlist = list(vblk.items(config.data_root()))            # items() captures _root, as the driver does
it_now, it_60, it_240, it_non = (next(it for it in vlist if it.chunk == chs[0])
                                 for chs in (ch_vn, ch_v6, ch_v2, ch_vx))
p_now, p_60, p_240, p_non = (vblk.priority(it) for it in (it_now, it_60, it_240, it_non))

# the decay curve, pinned against the undated baseline (pure-structural — proven exactly below)
assert abs((p_now - p_non) - glean.W_RECENT) < 0.02, f"a today session earns ≈ the full bonus: {p_now - p_non}"
assert abs((p_60 - p_non) - glean.W_RECENT / 2) < 0.02, f"one half-life old → half the bonus: {p_60 - p_non}"
assert abs((p_240 - p_non) - glean.W_RECENT / 16) < 0.02, f"four half-lives → a sixteenth: {p_240 - p_non}"

# ONE hop fills BOTH clocks: priority()'s valid-time read also populated age()'s fetched_at cache,
# and the valid-time itself is cached per cleaned blob (the §8d idiom), None for the undated source.
assert vblk._vt_cache[ch_vn[0].cleaned_hash] == _now_dt.isoformat(), "the hop's valid-time cached per cleaned blob"
assert vblk._vt_cache[ch_vx[0].cleaned_hash] is None, "no origin mtime → cached as undated (None)"
assert ch_vn[0].cleaned_hash in vblk._age_cache, "one read pair fills BOTH stamp caches (fetched_at rides along)"

# the escape hatch doubles as the exactness pin: W_RECENT=0 restores the pure-structural score, and
# the undated chunk must ALREADY sit there (missing date → +0.0 exactly, never the age-0 maximum);
# a broken-lineage chunk (§8d's ghost) likewise earns nothing — its score is identical term-on/off.
ghost_v = glean.ChunkItem(chunk=chunk.Chunk(cleaned_hash="f" * 64, byte_start=0, byte_end=1,
                                            turn_start=0, turn_end=1, segment=0, kinds=["user"]),
                          chunkset_hash="cs")
gp_on = vblk.priority(ghost_v)
_w = glean.W_RECENT
try:
    glean.W_RECENT = 0.0
    assert vblk.priority(it_now) == p_non == vblk.priority(it_non), \
        "W_RECENT=0 restores pure-structural; the undated chunk was already there (+0.0 exactly)"
    assert vblk.priority(ghost_v) == gp_on, "broken lineage → no bonus (score identical with the term off)"
finally:
    glean.W_RECENT = _w

# the payoff, end-to-end: equal structural scores → Greedy drains the RECENT session first (the
# backfill order becomes newest-life-first; aging's wait-clock still guarantees the old tail later).
ordered_v = block.priority_strategy("greedy").order([it_240, it_non, it_now, it_60],
                                                    vblk.priority, vblk.age)
assert ordered_v == [it_now, it_60, it_240, it_non], \
    "given equal structural scores, valid-time recency alone sets the drain order (newest first)"

# --- the RECENT_CLOCK policies (sulin, 2026-07-03): valid-then-arrival default, per-mode pins -------
# Default ("valid-then-arrival"): an undated source is dated by ARRIVAL — these fixtures were just
# ingested (fetched_at ≈ now), so the undated chunk earns ≈ the full bonus instead of +0.0. The
# fallback exists for conversation-less sources (fetched PDFs/pages: arrival IS their honest date).
fb_blk = glean.GleanBlock(FakeCompleter([]), model="vt", targets=[cs_vn, cs_v6, cs_v2, cs_vx])
assert fb_blk.recent_clock == glean.RECENT_CLOCK == "valid-then-arrival", "the fallback is the default"
list(fb_blk.items(config.data_root()))
fb_non = fb_blk.priority(it_non)
assert abs((fb_non - p_non) - glean.W_RECENT) < 0.02, \
    f"default clock dates the undated fixture by its (fresh) arrival: {fb_non - p_non}"
assert abs(fb_blk.priority(it_60) - p_60) < 1e-9, "a dated source ignores the fallback (valid wins)"
# "arrival": the tap date ONLY — the 240d-old session was fetched today, so it reads fresh.
ar_blk = glean.GleanBlock(FakeCompleter([]), model="vt", targets=[cs_vn, cs_v6, cs_v2, cs_vx],
                          recent_clock="arrival")
list(ar_blk.items(config.data_root()))
assert abs((ar_blk.priority(it_240) - p_non) - glean.W_RECENT) < 0.02, \
    "arrival clock: an old conversation fetched today reads fresh (a fetched-corpus policy)"
# a broken lineage has NO stamp under ANY policy → still +0.0
assert fb_blk.priority(ghost_v) == gp_on, "no stamp under any clock → no bonus"
# constructor refuses an unknown policy (a typo must not silently become a behavior)
try:
    glean.GleanBlock(FakeCompleter([]), model="vt", targets=[], recent_clock="mtime")
    raise AssertionError("unknown recent_clock must raise")
except ValueError:
    pass

print("OK — recency (valid-time): +W_RECENT today, half at one half-life, 1/16 at four; strict clock")
print("     → undated/broken lineage +0.0 exactly; default clock dates undated sources by ARRIVAL")
print("     (PDF/webpage policy), 'arrival' uses the tap date only; unknown clock refused.")


# --- 8f. warm-base FORK mode (ADR-0035, --warm-base): the digest rides a shared base, forks the chunk --
# Opt-in cost mode: the concept digest is seated in a warm base ONCE per tick and each chunk FORKS it,
# instead of re-sending the digest in every chunk's oneshot prompt. The load-bearing properties:
#   (a) the warm base carries the digest; each fork carries ONLY its chunk (no per-chunk digest re-send);
#   (b) the oneshot path is UNCHANGED — its per-chunk turn still carries the digest, as before R2;
#   (c) warm done-keys are DISTINCT from oneshot's (glean/5-fork vs glean/4), so the two never confuse;
#   (d) the completer's cache fields flow to the per-chunk marker + the run tally.

class WarmFake(FakeCompleter):
    """FakeCompleter + the session-chain seam (ADR-0035). `warm` records the base user turn and mints a
    session; `fork` records the per-chunk turn and reuses __call__'s quote→lines logic, stamping cache
    tokens on the returned Completion so §(d) can prove they flow through to the marker."""
    def __init__(self, candidates):
        super().__init__(candidates)
        self.warm_calls, self.fork_calls = [], []
    def warm(self, system, base_user):
        self.warm_calls.append({"system": system, "base_user": base_user})
        return f"sess-{len(self.warm_calls)}"                 # a fresh id per warm
    def fork(self, system, user, session_id):
        self.fork_calls.append({"system": system, "user": user, "session_id": session_id})
        base = self(system, user)                             # __call__: the quote→lines candidate logic
        return Completion(text=base.text, model=base.model, cost_usd=base.cost_usd,
                          cache_read_tokens=4096, cache_creation_tokens=0)   # a warm read landed

raw_w, _ = blobstore.ingest(blob.replace("kick off", "kick off warm"), source_kind="transcript",
                            source_id="glean-warm", origin_ref={"session_id": "glean-warm"})
cs_w, _, wchunks = chunk.materialize(raw_w, budget=600)
w_signal = sum(glean.has_signal_potential(chunk.resolve(c)) for c in wchunks)
w_keys = {glean.chunk_key(c) for c in wchunks}

warm_fake = WarmFake([{"quote": REAL_QUOTE, "summary": "warm", "markers": {"insight": 0.7}, "confidence": 0.9}])
wblk = glean.GleanBlock(warm_fake, model="warm", targets=[cs_w], warm_base=True)
wreport = block.run(wblk, progress=None)

# (a) exactly ONE warm base for the tick; every signal chunk forked it; the base carries the digest,
#     the forks do NOT re-send it.
assert len(warm_fake.warm_calls) == 1, "the digest is warmed ONCE per tick (one shared base for the run)"
assert len(warm_fake.fork_calls) == w_signal, "each signal chunk forks the shared base (one fork per call)"
assert "WHAT WE ALREADY KNOW" in warm_fake.warm_calls[0]["base_user"], "the warm base carries the concept digest"
assert all("WHAT WE ALREADY KNOW" not in f["user"] for f in warm_fake.fork_calls), \
    "a fork's turn is the excerpt ALONE — the digest is NOT re-sent per chunk (the whole point)"
assert all(f["session_id"] == "sess-1" for f in warm_fake.fork_calls), "every fork resumes the one base"
assert wreport.outputs == 1, "warm mode still extracts the durable line (the fork sees base + its chunk)"

# (b) the ONESHOT path is unchanged: its per-chunk turn still carries the digest (pre-R2 behavior). A
#     capturing completer over the SAME chunks (fresh model → not skipped) proves the digest rides the turn.
seen_oneshot = []
class Capture(FakeCompleter):
    def __call__(self, system, user):
        seen_oneshot.append(user)
        return super().__call__(system, user)
cap = Capture([{"quote": REAL_QUOTE, "summary": "cap", "markers": {"insight": 0.7}, "confidence": 0.9}])
block.run(glean.GleanBlock(cap, model="cap", targets=[cs_w]), progress=None)
assert seen_oneshot and all("WHAT WE ALREADY KNOW" in u for u in seen_oneshot), \
    "(b) oneshot: the digest rides EACH chunk's turn, exactly as before R2 (byte-identical prompt build)"

# (c) warm done-keys key on glean/5-fork; the SAME chunks+model under oneshot key on glean/4 → distinct sets.
assert glean.FORK_PROMPT_VERSION != glean.PROMPT_VERSION, "the fork path has its own prompt version"
wdone = block.done_index("glean", config.data_root())
assert all((k, glean.FORK_PROMPT_VERSION, "warm") in wdone for k in w_keys), "warm markers key on glean/5-fork"
one_fake = FakeCompleter([{"quote": REAL_QUOTE, "summary": "one", "markers": {"insight": 0.7}, "confidence": 0.9}])
one_rep = block.run(glean.GleanBlock(one_fake, model="warm", targets=[cs_w]), progress=None)
assert one_fake.calls == w_signal and one_rep.skipped == 0, \
    "(c) oneshot over the SAME chunks+model is NOT skipped — its glean/4 done-key is distinct from glean/5-fork"

# (d) the fork's cache fields flow to the per-chunk marker AND the run tally. Select the WARM path's
#     marker precisely (its glean/5-fork + "warm" key) — the same chunks also carry cap/oneshot markers.
owner_w = next(m for m in glean_markers()
               if m["target"] in w_keys and m.get("n_outputs") == 1
               and m.get("prompt_version") == glean.FORK_PROMPT_VERSION and m.get("model") == "warm")
assert owner_w["cache_read"] == 4096, "the fork's cache_read landed on its per-chunk marker"
assert owner_w["cache_creation"] == 0 and "input_tokens" in owner_w, "the token/cache audit fields ride the marker"
assert wblk.cache_read == 4096 * w_signal, "the run tallies cache_read across every fork"
assert wblk.cache_creation == 0, "no cache-creation on the fork calls in this fake"

# the refusal (ADR-0027): warm_base against a completer with no .warm/.fork raises, loudly, at construction.
try:
    glean.GleanBlock(FakeCompleter([]), model="x", targets=[], warm_base=True)
    raise AssertionError("warm_base without a session-capable completer must raise")
except ValueError:
    pass

print("OK — warm-base (ADR-0035): the digest warms ONE shared base per tick; each fork carries its chunk")
print("     alone; oneshot unchanged (digest per turn); done-keys distinct (glean/5-fork); cache fields flow.")


# --- 9. live smoke (opt-in): the real claude CLI over one real chunkset ----------------------

if os.environ.get("RATCHET_LIVE_TEST") == "1":
    real = sorted(_glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getsize)
    if real:
        raw_live, _ = blobstore.ingest(Path(real[-1]).read_text(encoding="utf-8", errors="replace"),
                                       source_kind="transcript", source_id="live-" + Path(real[-1]).stem,
                                       origin_ref={"session_id": Path(real[-1]).stem})
        cs_live, _, _ = chunk.materialize(raw_live, budget=12000)
        rep = glean.run([cs_live], completer.make_cli_completer("haiku"), model="haiku", max_usd=0.50)
        for e in glean.load_events():
            cb = blobstore.get(e["cleaned_hash"]).encode("utf-8")
            s = e["evidence"][0]
            assert cb[s["byte_start"]:s["byte_end"]], "every live event resolves against its cleaned blob"
        print(f"OK — live: {rep.events} events, ${rep.cost_usd:.4f} "
              f"(every event's span resolves against its cleaned blob)")
    else:
        print("SKIP live smoke — no transcript found")
else:
    print("SKIP live smoke — set RATCHET_LIVE_TEST=1 to run the real claude CLI")

print("\nall glean tests passed.")
