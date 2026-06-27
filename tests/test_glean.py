"""glean tests: the LLM stage is exercised offline with a FAKE Completer (no network, no API key),
so the suite is deterministic. The load-bearing checks are the trust anchor — a fabricated quote is
rejected, a real quote yields a byte span that resolves back to exactly the quote — plus the
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

from ratchet import blobstore, block, chunk, completer, config, glean, weave  # noqa: E402
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
REAL_QUOTE = "always commit with jj, never git"          # a real substring of turn 3
FAKE_QUOTE = "this phrase never appears in the transcript at all"  # hallucination → must be rejected
SHORT_QUOTE = "jj"                                        # a real substring, but < MIN_QUOTE_BYTES

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
    """Records every call and returns canned candidates — the network seam, replaced. Returning the
    same REAL+FAKE pair for every chunk proves verification is per-chunk: REAL is accepted only by
    the chunk that actually contains it; FAKE never."""
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return Completion(text=json.dumps({"events": self.candidates}), model="fake", cost_usd=0.002)


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

fenced = '```json\n{"events": [{"quote": "x", "summary": "y", "markers": {"insight": 1}, "confidence": 1}]}\n```'
assert glean.parse_candidates(fenced) == [{"quote": "x", "summary": "y", "markers": {"insight": 1}, "confidence": 1}]
assert glean.parse_candidates("not json at all") == [], "malformed output → no candidates, no crash"
assert glean.parse_candidates('{"events": []}') == [], "explicit empty → no candidates"

# --- 3. the trust anchor: real quote accepted with an exact span, fabricated quote rejected --
#     ... and the unit of work is now ONE CHUNK = ONE LLM call (ADR-0009).

fake = FakeCompleter([
    {"quote": REAL_QUOTE, "summary": "Commit with jj, never git.",
     "markers": {"insight": 0.8, "surprise": 0.1}, "confidence": 0.9},
    {"quote": FAKE_QUOTE, "summary": "A hallucinated claim.", "markers": {"insight": 0.9}, "confidence": 0.9},
])
probe = Probe()
blk = glean.GleanBlock(fake, model="fake", targets=[cs_hash])
report = block.run(blk, progress=probe)

# per-chunk granularity: one call per SURVIVING chunk; events accrue on the block instance + Report.outputs
assert fake.calls == SIGNAL_CHUNKS, "one LLM call per signal-bearing chunk (the chunk is the unit now)"
assert report.outputs == 1 == blk.events, "only the real quote, in its one owning chunk, becomes an event"
assert report.processed == ALL_CHUNKS, "EVERY chunk processed (filter-skips included, as 0-output items)"
# every non-owning signal chunk rejects REAL+FAKE (2); the owning chunk rejects only FAKE (1)
assert blk.rejected == 2 * (SIGNAL_CHUNKS - 1) + 1, "every unverifiable quote is rejected, deterministically"

# streaming progress landed ONE line per processed chunk (no skips this fresh run); the owning chunk's
# line carries n_out==1, the rest n_out==0 (filter-skips + signal chunks with no surviving quote)
assert len(probe.lines) == ALL_CHUNKS and all(not l["errored"] for l in probe.lines), "a live line per chunk"
assert sum(l["n_out"] for l in probe.lines) == 1, "exactly the owning chunk's line reports an event"

ev = glean.load_events()[0]
span = ev["evidence"][0]
resolved = blobstore.get(cleaned_hash).encode("utf-8")[span["byte_start"]:span["byte_end"]].decode()
assert resolved == REAL_QUOTE, "the stored span resolves to EXACTLY the quote (the trust check)"
assert span["byte_start"] != cleaned.find(REAL_QUOTE), "offsets are bytes, not chars (multibyte content present)"
assert ev["id"] == glean.event_id(cleaned_hash, span["byte_start"], span["byte_end"]), "id = sha256(cleaned+span)[:12]"
assert ev["cleaned_hash"] == cleaned_hash and "quote" not in ev, "event points into the cleaned blob; never copies its text"
assert set(ev["markers"]) == set(glean.MARKER_KINDS) and ev["markers"]["insight"] == 0.8, "markers scored per kind"
assert "status" not in ev, "state is a decision now (ADR-0007) — no in-record status field"

# the trust check (verify) returns a span; record assembly (build_event) is a separate step
owner = [c for c in chunks if REAL_QUOTE in chunk.resolve(c)][0]
ok_span = glean.verify({"quote": REAL_QUOTE}, owner, cleaned.encode("utf-8"))
assert ok_span is not None and isinstance(ok_span, tuple), "verify yields a span, not an event"

# untrusted-field hygiene (build_event): marker scores clamp to [0,1], unknown keys drop, missing → 0
dirty = glean.build_event({"quote": REAL_QUOTE, "summary": "x", "markers": {"insight": 9, "bogus": 1},
                           "confidence": 9}, owner, ok_span, model="fake", run_id="r")
assert dirty["markers"] == {"surprise": 0.0, "insight": 1.0, "research": 0.0}, "markers coerced/clamped, unknowns dropped"
assert dirty["confidence"] == 1.0, "confidence clamped to [0,1]"
assert dirty["producer"]["stage"] == "glean" and dirty["producer"]["prompt_version"] == glean.PROMPT_VERSION

# a quote that IS a real substring but too short to be useful evidence is rejected (no span)
assert glean.verify({"quote": SHORT_QUOTE}, owner, cleaned.encode("utf-8")) is None, "too-short quote rejected"

print("OK — trust anchor: real quote → exact byte span that resolves back; fabricated/short quotes")
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
nanev = glean.build_event({"quote": REAL_QUOTE, "summary": "x", "markers": {"surprise": float("nan")},
                           "confidence": float("inf")}, owner, ok_span, model="fake", run_id="r")
assert nanev["markers"]["surprise"] == 0.0 and nanev["confidence"] == 0.5, "NaN/inf scrubbed from the event"

# a long but all-whitespace quote is real text yet zero signal → rejected (no span)
assert glean.verify({"quote": " " * 20}, owner, cleaned.encode("utf-8")) is None, "whitespace quote rejected"

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
