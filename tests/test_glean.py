"""glean tests: the LLM stage is exercised offline with a FAKE Completer (no network, no API key),
so the suite is deterministic. The load-bearing checks are the trust anchor — a fabricated quote is
rejected, a real quote yields a byte span that resolves back to exactly the quote — plus the
filter, the event schema/id, the append-only store + lineage, and idempotent re-runs. A live
CLI smoke test is gated behind RATCHET_LIVE_TEST=1. Run: `python tests/test_glean.py`."""
import glob as _glob
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-glean-")

from ratchet import blobstore, chunk, completer, config, glean, weave  # noqa: E402
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

fake = FakeCompleter([
    {"quote": REAL_QUOTE, "summary": "Commit with jj, never git.",
     "markers": {"insight": 0.8, "surprise": 0.1}, "confidence": 0.9},
    {"quote": FAKE_QUOTE, "summary": "A hallucinated claim.", "markers": {"insight": 0.9}, "confidence": 0.9},
])
report = glean.run([cs_hash], fake, model="fake")

assert report.events == 1, "only the real quote, in its one owning chunk, becomes an event"
assert fake.calls == sum(glean.has_signal_potential(chunk.resolve(c)) for c in chunks), "one call per surviving chunk"
# every non-owning chunk rejects REAL+FAKE (2); the owning chunk rejects only FAKE (1)
assert report.rejected == 2 * (fake.calls - 1) + 1, "every unverifiable quote is rejected, deterministically"

ev = report.results[0].events[0]
span = ev["evidence"][0]
resolved = blobstore.get(cleaned_hash).encode("utf-8")[span["byte_start"]:span["byte_end"]].decode()
assert resolved == REAL_QUOTE, "the stored span resolves to EXACTLY the quote (the trust check)"
assert span["byte_start"] != cleaned.find(REAL_QUOTE), "offsets are bytes, not chars (multibyte content present)"
assert ev["id"] == glean.event_id(cleaned_hash, span["byte_start"], span["byte_end"]), "id = sha256(cleaned+span)[:12]"
assert ev["cleaned_hash"] == cleaned_hash and "quote" not in ev, "event points into the cleaned blob; never copies its text"
assert set(ev["markers"]) == set(glean.MARKER_KINDS) and ev["markers"]["insight"] == 0.8, "markers scored per kind"
assert ev["status"] == "extracted"
assert ev["producer"]["stage"] == "glean" and ev["producer"]["model"] == "fake"
assert ev["producer"]["prompt_version"] == glean.PROMPT_VERSION and ev["producer"]["cost_usd"] > 0

# the trust check (verify) returns a span; record assembly (build_event) is a separate step
owner = [c for c in chunks if REAL_QUOTE in chunk.resolve(c)][0]
ok_span = glean.verify({"quote": REAL_QUOTE}, owner, cleaned.encode("utf-8"))
assert ok_span is not None and isinstance(ok_span, tuple), "verify yields a span, not an event"

# untrusted-field hygiene (build_event): marker scores clamp to [0,1], unknown keys drop, missing → 0
dirty = glean.build_event({"quote": REAL_QUOTE, "summary": "x", "markers": {"insight": 9, "bogus": 1},
                           "confidence": 9}, owner, ok_span, model="fake", run_id="r")
assert dirty["markers"] == {"surprise": 0.0, "insight": 1.0, "research": 0.0}, "markers coerced/clamped, unknowns dropped"
assert dirty["confidence"] == 1.0, "confidence clamped to [0,1]"

# a quote that IS a real substring but too short to be useful evidence is rejected (no span)
assert glean.verify({"quote": SHORT_QUOTE}, owner, cleaned.encode("utf-8")) is None, "too-short quote rejected"

print("OK — trust anchor: real quote → exact byte span that resolves back; fabricated/short quotes")
print("     rejected deterministically; event is a pointer (no copied text); fields coerced/clamped.")

# --- 4. the append-only event store + lineage -----------------------------------------------

loaded = glean.load_events()
assert len(loaded) == 1 and loaded[0]["id"] == ev["id"], "events committed to events/glean-*.jsonl and reloaded"
shards = _glob.glob(os.path.join(os.environ["RATCHET_DATA_DIR"], "events", "glean-*.jsonl"))
assert len(shards) == 1 and not shards[0].endswith(".partial"), ".partial renamed to final on clean exit"
# lineage is content-addressed without making events blobs: event → cleaned → raw → datastore
assert blobstore.get_meta(cleaned_hash)["derived_from"] == raw_h, "event.cleaned_hash → derived_from → raw"

# a leftover .partial (a crashed run's output) is invisible to readers
part = Path(os.environ["RATCHET_DATA_DIR"]) / "events" / "glean-crashed.jsonl.partial"
part.write_text(json.dumps(ev) + "\n", encoding="utf-8")
assert len(glean.load_events()) == 1, "a .partial shard is ignored (crashed run reprocessed, not read)"
part.unlink()

# --- 5. idempotency: a re-run for the same (chunkset, prompt_version, model) does no work -----

before = fake.calls
again = glean.run([cs_hash], fake, model="fake")
assert fake.calls == before, "the processed ledger skips a done chunkset — zero LLM calls"
assert again.skipped == 1 and not again.results, "re-run skips, produces nothing new"
assert len(glean.load_events()) == 1, "no duplicate events written on re-run"
# a different model is a different ledger key → it re-extracts over the same frozen chunks
fake2 = FakeCompleter([{"quote": REAL_QUOTE, "summary": "Commit with jj.", "signal": "preference", "confidence": 0.9}])
rerun = glean.run([cs_hash], fake2, model="fake-v2")
assert fake2.calls > 0 and rerun.events == 1, "bumping the model re-extracts (idempotency is per prompt+model)"

print("OK — append-only event store (glob+merge), content-addressed lineage to raw, .partial ignored,")
print("     idempotent re-runs (skip done; re-extract on a new model/prompt key).")

# --- 6. budget stop is a clean exit ----------------------------------------------------------

# a fresh source so nothing is pre-done; max_usd below the first call's cost → stop before any call
raw2, _ = blobstore.ingest(blob.replace("kick off", "kick off again"), source_kind="transcript",
                           source_id="glean-syn2", origin_ref={"session_id": "glean-syn2"})
cs2, _, _ = chunk.materialize(raw2, budget=600)
fake3 = FakeCompleter([{"quote": REAL_QUOTE, "summary": "x", "markers": {"insight": 1}, "confidence": 1}])
stopped = glean.run([cs2], fake3, model="fake3", max_usd=0.0)
assert stopped.stopped_on_budget and fake3.calls == 0, "max_usd stops the run cleanly before spending"

print("OK — budget ceiling stops a run cleanly before overspend.")


# --- 7. adversarial hardening: flaky completer isolated, fully-errored chunkset retried -------

# non-finite / out-of-range scores never reach the store (NaN clamps to 1.0 only by argument-order luck)
assert glean._clean_score(float("nan")) == 0.0 and glean._clean_score(float("inf")) == 0.0
assert glean._clean_score("NaN") == 0.0 and glean._clean_score(1e400) == 0.0
assert glean._clean_score(float("nan"), 0.5) == 0.5, "non-finite falls back to the default, not 1.0"
nanev = glean.build_event({"quote": REAL_QUOTE, "summary": "x", "markers": {"surprise": float("nan")},
                           "confidence": float("inf")}, owner, ok_span, model="fake", run_id="r")
assert nanev["markers"]["surprise"] == 0.0 and nanev["confidence"] == 0.5, "NaN/inf scrubbed from the event"

# a long but all-whitespace quote is real text yet zero signal → rejected (no span)
assert glean.verify({"quote": " " * 20}, owner, cleaned.encode("utf-8")) is None, "whitespace quote rejected"

# a completer raising a NON-GleanError is isolated per-chunk; the run completes (does not abort), and a
# chunkset where every call failed is NOT marked done — it is retried next run (no silent loss)
class Boom:
    def __init__(self): self.calls = 0
    def __call__(self, system, user):
        self.calls += 1
        raise ValueError("simulated binding failure")

raw_b, _ = blobstore.ingest(blob.replace("kick off", "kick off boom"), source_kind="transcript",
                            source_id="glean-boom", origin_ref={"session_id": "glean-boom"})
cs_b, _, _ = chunk.materialize(raw_b, budget=600)
boom = Boom()
rep_b = glean.run([cs_b], boom, model="boom")
assert boom.calls > 0 and rep_b.errored == boom.calls, "every failing call is isolated and counted"
assert rep_b.events == 0 and (cs_b, glean.PROMPT_VERSION, "boom") not in glean.processed_index(), \
    "a fully-errored chunkset is NOT marked done"
recovered = glean.run([cs_b], FakeCompleter([{"quote": REAL_QUOTE, "summary": "ok",
    "markers": {"insight": 0.5}, "confidence": 0.8}]), model="boom")
assert recovered.events == 1, "the retried chunkset recovers once the completer works"

# an absent (TTL-reclaimed) cleaned blob → the chunkset is errored, not a crashed run, and not done
raw_m, _ = blobstore.ingest(blob.replace("kick off", "kick off gone"), source_kind="transcript",
                            source_id="glean-gone", origin_ref={"session_id": "glean-gone"})
cs_m, _, mchunks = chunk.materialize(raw_m, budget=600)
blobstore._paths(mchunks[0].cleaned_hash, config.data_root())[0].unlink()   # reclaim the cleaned blob
rep_m = glean.run([cs_m], FakeCompleter([]), model="gone")
assert rep_m.errored == 1 and (cs_m, glean.PROMPT_VERSION, "gone") not in glean.processed_index(), \
    "absent cleaned blob → errored chunkset, retried (no crash, no false done)"

print("OK — adversarial hardening: flaky completer isolated + run survives, fully-errored chunkset")
print("     retried (not falsely done), absent cleaned blob handled, non-finite/whitespace scrubbed.")


# --- 8. live smoke (opt-in): the real claude CLI over one real chunkset ----------------------

if os.environ.get("RATCHET_LIVE_TEST") == "1":
    real = sorted(_glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getsize)
    if real:
        raw_live, _ = blobstore.ingest(Path(real[-1]).read_text(encoding="utf-8", errors="replace"),
                                       source_kind="transcript", source_id="live-" + Path(real[-1]).stem,
                                       origin_ref={"session_id": Path(real[-1]).stem})
        cs_live, _, _ = chunk.materialize(raw_live, budget=12000)
        rep = glean.run([cs_live], completer.make_cli_completer("haiku"), model="haiku", max_usd=0.50)
        cb = {h: blobstore.get(h).encode("utf-8") for h in {e["cleaned_hash"] for r in rep.results for e in r.events}}
        for r in rep.results:
            for e in r.events:
                s = e["evidence"][0]
                assert cb[e["cleaned_hash"]][s["byte_start"]:s["byte_end"]], "every live event resolves"
        print(f"OK — live: {rep.events} events, {rep.rejected} rejected, ${rep.cost_usd:.4f} "
              f"(every event's span resolves against its cleaned blob)")
    else:
        print("SKIP live smoke — no transcript found")
else:
    print("SKIP live smoke — set RATCHET_LIVE_TEST=1 to run the real claude CLI")
