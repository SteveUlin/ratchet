"""Blobstore round-trip: ingest dedup, header data, meta-as-truth versioning, crash-orphan repair.
Run: `python tests/test_storage.py` (uses a throwaway RATCHET_DATA_DIR)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-")

from ratchet import blobstore, config  # noqa: E402

root = config.ensure_layout()
text = "[user] no, use jj not git"
origin = {"path": "/x/sess1.jsonl", "project": "proj", "session_id": "sess1",
          "size_bytes": len(text.encode()), "mtime": "2026-06-26T00:00:00Z"}

# content-addressed ingest + idempotent dedup
h1, w1 = blobstore.ingest(text, source_kind="transcript", source_id="sess1",
                          origin_ref=origin, fetched_at="2026-06-26T00:00:00Z")
h2, w2 = blobstore.ingest(text, source_kind="transcript", source_id="sess1",
                          origin_ref=origin, fetched_at="2026-06-26T00:00:00Z")
assert w1 is True and w2 is False and h1 == h2, "ingest dedup"
assert blobstore.get(h1) == text and blobstore.has(h1)

# header data: the meta sidecar is the source of truth
m = blobstore.get_meta(h1)
assert m["source_id"] == "sess1" and m["source_kind"] == "transcript" and m["prev"] is None
assert m["origin_ref"]["path"].endswith("sess1.jsonl")

# versioning derived from meta sidecars (no separate ledger), prev auto-linked
h3, w3 = blobstore.ingest(text + ". also prefer fish", source_kind="transcript", source_id="sess1",
                          origin_ref=origin, fetched_at="2026-06-26T01:00:00Z")
assert w3 is True and blobstore.get_meta(h3)["prev"] == h1, "prev derived from latest meta"
assert blobstore.latest_version("sess1") == h3, "latest version"

# crash-safety: a blob with content but NO meta is NOT committed and gets re-created
orphan = "[user] crash orphan"
oh = blobstore.blob_hash(orphan)
content_path, _ = blobstore._paths(oh, root)
content_path.parent.mkdir(parents=True, exist_ok=True)
content_path.write_text(orphan, encoding="utf-8")  # simulate crash after content, before meta
assert not blobstore.has(oh), "content-only blob is NOT committed (has() keys on meta)"
h4, w4 = blobstore.ingest(orphan, source_kind="transcript", source_id="sess2",
                          origin_ref=origin, fetched_at="2026-06-26T02:00:00Z")
assert w4 is True and blobstore.has(oh) and h4 == oh, "orphan repaired — meta now written"
assert blobstore.get_meta(oh)["source_id"] == "sess2"

# raw blobs carry the kind discriminator so a consumer can tell ground truth from cache
assert blobstore.get_meta(h1)["kind"] == "raw", "raw blob tagged kind=raw"

# derived blobs: content-addressed like raw, but lineage-tagged + TTL-eligible (ADR-0003)
rendered = "[user] no, use jj not git\n\n[assistant] ok"
dh, dw = blobstore.put_derived(rendered, source_kind="transcript", derived_from=h1,
                               produced_by="weave", render_version="weave/1",
                               fmt="weave.render/1", tags={"project": "proj"},
                               expires_at="2026-12-31T00:00:00Z")
dh2, dw2 = blobstore.put_derived(rendered, source_kind="transcript", derived_from=h1,
                                 produced_by="weave", render_version="weave/1",
                                 fmt="weave.render/1")
assert dw is True and dw2 is False and dh == dh2, "derived dedup is content-addressed"
assert blobstore.get(dh) == rendered

dm = blobstore.get_meta(dh)
assert dm["kind"] == "derived" and dm["derived_from"] == h1 and dm["produced_by"] == "weave"
assert dm["render_version"] == "weave/1" and dm["format"] == "weave.render/1"
assert dm["expires_at"] == "2026-12-31T00:00:00Z" and dm["tags"]["project"] == "proj"

# lineage is traversable: derived_for(raw) finds the render; format filter narrows it
lineage = list(blobstore.derived_for(h1))
assert [m["content_hash"] for m in lineage] == [dh], "derived_for yields the render"
assert list(blobstore.derived_for(h1, fmt="nope")) == [], "format filter excludes non-matches"

# raw_meta_of: THE cleaned → derived_from → raw lineage hop, single-sourced — every lineage read
# (session_of / project_of / glean's stamp fill / subject's repo) is a field off the dict it returns.
rm = blobstore.raw_meta_of(dh)
assert rm is not None and rm["content_hash"] == h1 and rm["source_id"] == "sess1", \
    "raw_meta_of resolves a derived blob to its RAW meta"
assert blobstore.session_of(dh) == "sess1", "session_of is the raw meta's source_id field"
assert blobstore.project_of(dh) == "proj", "project_of is the raw meta's origin_ref.project field"
assert blobstore.raw_meta_of(h1) is None, "a raw blob has no derived_from → None (degrade, not raise)"
assert blobstore.raw_meta_of("no-such-hash") is None, "absent meta degrades to None, never fatal"
lc = {}
assert blobstore.raw_meta_of(dh, cache=lc)["content_hash"] == h1 and dh in lc, "the cache fills on first call"
lc[dh] = {"content_hash": "sentinel"}
assert blobstore.raw_meta_of(dh, cache=lc)["content_hash"] == "sentinel", "a cache hit skips the meta reads"
lc2 = {}
blobstore.raw_meta_of("no-such-hash", cache=lc2)
assert lc2 == {"no-such-hash": None}, "a miss is cached too (an unresolvable blob pays the hop once)"

# derived blobs stay OUT of the raw version index (no source_id) — they don't fork the TimeMap
assert blobstore.latest_version("sess1") == h3, "derived blob does not become a raw version"

# byte-faithful store: content with \r must round-trip, or content-addressing breaks (a cleaned
# blob holds real \r from rendered tool output; read_text would rewrite \r\n→\n). (adversarial finding)
for crlf in ("a\rb", "h\r\nv", "x\r\r\ny"):
    ch, _ = blobstore.ingest(crlf, source_kind="transcript", source_id="cr-" + str(len(crlf)),
                             origin_ref={})
    assert blobstore.get(ch) == crlf, f"\\r round-trips ({crlf!r} → {blobstore.get(ch)!r})"
    assert blobstore.blob_hash(blobstore.get(ch)) == ch, "blob_hash(get(h)) == h"

# raw and derived share one hash→meta namespace; identical bytes across kinds must fail loud, not
# silently no-op and drop lineage (adversarial finding) — in BOTH directions.
collide = "x-collide-x"
blobstore.ingest(collide, source_kind="transcript", source_id="c1", origin_ref={})
try:
    blobstore.put_derived(collide, source_kind="transcript", derived_from=h1, produced_by="weave",
                          render_version="weave/1", fmt="weave.render/1")
    assert False, "derived bytes colliding with a raw blob must raise"
except ValueError:
    pass
derived_only = "y-derived-only-y"
blobstore.put_derived(derived_only, source_kind="transcript", derived_from=h1, produced_by="weave",
                      render_version="weave/1", fmt="weave.render/1")
try:
    blobstore.ingest(derived_only, source_kind="transcript", source_id="c2", origin_ref={})
    assert False, "raw bytes colliding with a derived blob must raise (symmetric guard)"
except ValueError:
    pass

# ── ADR-0007: every artifact is a blob ──────────────────────────────────────────────────────
# Events/takeaways/concepts/decisions are RAW blobs keyed by content_hash, VERSIONED by source_id.
# The deterministic source_id (span-derived event_id, membership-derived cluster_signature, …)
# splits identity from content: re-extraction with changed output is a NEW VERSION of a stable
# source, prev auto-linked, latest wins — the property the derived views build on.
import json as _json  # noqa: E402


def _cjson(obj):  # the load-bearing canonical serializer producers share (stable bytes => stable hash)
    return _json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# (a) a versioned non-deterministic source: same source_id, different content => a new version.
# An event_id keys identity; two re-extractions (different summary) are two versions, latest wins.
ev_id = "ev0abc123def"
ev_v1 = {"cleaned_hash": h1, "evidence": [{"byte_start": 0, "byte_end": 5}],
         "summary": "use jj not git", "markers": ["preference"], "confidence": 0.8}
ev_v2 = {"cleaned_hash": h1, "evidence": [{"byte_start": 0, "byte_end": 5}],
         "summary": "prefer jj over git (refined)", "markers": ["preference"], "confidence": 0.9}
eo = {"stage": "glean", "model": "m1", "prompt_version": "g/1", "run_id": "r1", "cleaned_hash": h1}
eh1, ew1 = blobstore.ingest(_cjson(ev_v1), source_kind="event", source_id=ev_id,
                            origin_ref=eo, fetched_at="2026-06-26T00:00:00Z")
eh1b, ew1b = blobstore.ingest(_cjson(ev_v1), source_kind="event", source_id=ev_id,
                              origin_ref=eo, fetched_at="2026-06-26T00:00:00Z")
assert ew1 is True and ew1b is False and eh1 == eh1b, "byte-identical re-extraction is a no-op"
eh2, ew2 = blobstore.ingest(_cjson(ev_v2), source_kind="event", source_id=ev_id,
                            origin_ref=eo, fetched_at="2026-06-26T03:00:00Z")
assert ew2 is True and eh2 != eh1, "changed content => a new version (distinct content_hash)"
assert blobstore.get_meta(eh2)["prev"] == eh1, "new version prev-links to prior latest"
assert blobstore.get_meta(eh2)["source_kind"] == "event"
assert blobstore.latest_version(ev_id) == eh2, "latest wins across versions of one source"
# the version content is the model output + provenance pointers; producer lives in meta.origin_ref
assert blobstore.get_meta(eh2)["origin_ref"]["stage"] == "glean"

# latest_by_kind folds each source to its newest version, restricted to ONE kind.
ev_id2 = "ev9zzz000111"
eh3, _ = blobstore.ingest(_cjson({"cleaned_hash": h1, "evidence": [{"byte_start": 6, "byte_end": 9}],
                                  "summary": "x", "markers": [], "confidence": 0.5}),
                          source_kind="event", source_id=ev_id2, origin_ref=eo,
                          fetched_at="2026-06-26T00:30:00Z")
by_kind = blobstore.latest_by_kind("event")
assert by_kind == {ev_id: eh2, ev_id2: eh3}, "latest_by_kind => {source_id: newest hash} for the kind"

# (b) latest_by_kind isolates one kind and ignores others sharing the store.
tk_sig = "cl0sig111aaaa222"
tk = {"title": "prefer jj", "why": "mutable history", "member_events": [ev_id],
      "evidence": [{"event_id": ev_id, "cleaned_hash": h1, "byte_start": 0, "byte_end": 5}],
      "supersedes": [], "cluster_signature": tk_sig}
th1, _ = blobstore.ingest(_cjson(tk), source_kind="takeaway", source_id=tk_sig,
                          origin_ref={"stage": "dream", "model": "m1", "run_id": "r2"},
                          fetched_at="2026-06-26T04:00:00Z")
assert blobstore.latest_by_kind("event") == {ev_id: eh2, ev_id2: eh3}, "event scan ignores takeaways"
assert blobstore.latest_by_kind("takeaway") == {tk_sig: th1}, "takeaway scan ignores events"
# transcript blobs (source_kind=transcript) never leak into a span-derived kind's view
assert "sess1" not in blobstore.latest_by_kind("event")
# derived blobs (the weave render) have no raw source_kind and never appear
assert blobstore.latest_by_kind("transcript") .get("sess1") == h3, "transcript view still the TimeMap"

# (c) decisions: a kind='raw', source_kind='decision' blob; source_id == its own content_hash,
# never re-versioned (prev=None). The body is unique per logical fact (target+verb+run_id+at) so
# two distinct decisions can never content-hash-collide into one blob.
def _mk_decision(body):
    s = _cjson(body)
    h = blobstore.blob_hash(s)
    return blobstore.ingest(s, source_kind="decision", source_id=h, prev=None,
                            origin_ref={"stage": body["producer"]["stage"],
                                        "run_id": body["producer"]["run_id"]},
                            fetched_at=body["producer"]["at"])


# a processed marker (producer 'done') referencing a chunkset input
cs_hash = "cs0chunkset0000"
d_proc = {"verb": "processed", "target": cs_hash,
          "producer": {"stage": "glean", "model": "m1", "run_id": "r1", "at": "2026-06-26T00:10:00Z"},
          "stage": "glean", "prompt_version": "g/1", "model": "m1", "run_id": "r1",
          "at": "2026-06-26T00:10:00Z", "n_events": 2}
dph, dpw = _mk_decision(d_proc)
assert dpw is True and blobstore.get_meta(dph)["source_kind"] == "decision"
assert blobstore.get_meta(dph)["prev"] is None, "decisions are never re-versioned (prev=None)"
assert blobstore.get_meta(dph)["source_id"] == dph, "decision source_id == its own content_hash"

# review verbs referencing a takeaway target: a reject then a later accept
d_reject = {"verb": "reject", "target": th1, "reason": "too vague",
            "producer": {"stage": "review", "run_id": "rv1", "at": "2026-06-26T05:00:00Z"}}
d_accept = {"verb": "accept", "target": th1, "concept_id": "cpt000111222",
            "producer": {"stage": "review", "run_id": "rv2", "at": "2026-06-26T06:00:00Z"}}
drh, _ = _mk_decision(d_reject)
dah, _ = _mk_decision(d_accept)

# decisions_for finds decisions by target; bodies are augmented with content_hash + fetched_at.
proc = list(blobstore.decisions_for(cs_hash))
assert len(proc) == 1 and proc[0]["verb"] == "processed" and proc[0]["content_hash"] == dph
assert proc[0]["fetched_at"] == "2026-06-26T00:10:00Z", "body augmented with meta.fetched_at"
on_tk = sorted(blobstore.decisions_for(th1), key=lambda b: b["fetched_at"])
assert [b["verb"] for b in on_tk] == ["reject", "accept"], "both decisions on the takeaway found"

# verb filter narrows; stage filter (body.producer.stage) keys the processed-marker query.
assert [b["content_hash"] for b in blobstore.decisions_for(th1, verb="accept")] == [dah]
glean_done = list(blobstore.decisions_for(cs_hash, verb="processed", stage="glean"))
assert len(glean_done) == 1, "processed marker matched on (target, verb, stage)"
assert list(blobstore.decisions_for(cs_hash, verb="processed", stage="dream")) == [], "stage filters"
# a target with no decisions yields nothing
assert list(blobstore.decisions_for("no-such-target")) == []

# latest_decision recency-folds to the single in-force decision (accept supersedes the reject).
assert blobstore.latest_decision(th1)["verb"] == "accept", "latest decision wins (by fetched_at)"
assert blobstore.latest_decision(cs_hash)["content_hash"] == dph
assert blobstore.latest_decision("no-such-target") is None

# (d) two byte-DISTINCT decision bodies for the same target are TWO blobs — the uniqueness rule
# that stops blob_hash from conflating two logically-distinct decisions. The reject + accept above
# already share target th1 yet are distinct blobs; assert it explicitly across the same verb too.
d_proc_b = dict(d_proc, run_id="r2", at="2026-06-26T00:20:00Z",
                producer={"stage": "glean", "model": "m1", "run_id": "r2", "at": "2026-06-26T00:20:00Z"})
dph2, dpw2 = _mk_decision(d_proc_b)
assert dpw2 is True and dph2 != dph, "distinct run_id/at => distinct bytes => a second blob"
assert drh != dah != dph, "distinct decisions on shared/other targets never share a blob"
# now two processed markers exist on cs_hash; the to-do fold sees both (run-keyed, not collapsed)
assert len(list(blobstore.decisions_for(cs_hash, verb="processed"))) == 2

# the existing raw/derived collision + kind guards still hold for the new source_kinds: a decision
# blob is kind='raw', so re-ingesting its bytes as derived must still fail loud.
try:
    blobstore.put_derived(_cjson(d_proc), source_kind="decision", derived_from=h1,
                          produced_by="x", render_version="x/1", fmt="x/1")
    assert False, "derived bytes colliding with a raw decision blob must raise"
except ValueError:
    pass

# ensure_layout makes ONLY what the blob model uses: blobs/ + tmp/ (concepts are blobs, not a dir;
# state/ is tap's on-demand cursor home) — a dir nothing reads or writes is a lie in the layout.
assert not (root / "concepts").exists(), "no fossil dirs — every artifact kind lives in blobs/"

# ── config.reviewer(): decision provenance names the OPERATOR, never a baked-in name ──────────
# The env var is the explicit override; the login name the honest default. A hardcoded default
# would forge every other user's audit trail, so resolution runs at CALL time. Assert the override
# beats the fallback; the fallback mirrors config's OWN or-guard (login, else last-ditch "reviewer")
# so the check is deterministic under any CI user without depending on getpass succeeding.
import getpass as _getpass  # noqa: E402
try:
    _login = _getpass.getuser()
except Exception:
    _login = "reviewer"
_saved_reviewer = os.environ.pop("RATCHET_REVIEWER", None)
assert config.reviewer() == _login, "no env → the honest login default (never a baked-in name)"
os.environ["RATCHET_REVIEWER"] = "ci-operator"
assert config.reviewer() == "ci-operator", "RATCHET_REVIEWER overrides the login default"
os.environ["RATCHET_REVIEWER"] = ""
assert config.reviewer() == _login, "an EMPTY override falls through to login (the or-guard)"
if _saved_reviewer is None:
    os.environ.pop("RATCHET_REVIEWER", None)
else:
    os.environ["RATCHET_REVIEWER"] = _saved_reviewer

print("OK — ingest dedup, header data, meta-as-truth versioning, crash-orphan repair,")
print("     derived primitive (content-addressed, lineage-tagged, TTL, out of TimeMap),")
print("     raw_meta_of single lineage hop (cached, degrades to None; session_of/project_of read off it),")
print("     byte-faithful \\r round-trip, symmetric raw/derived collision guard,")
print("     ADR-0007 blobs: event/takeaway versioning by source_id, latest_by_kind isolation,")
print("     decisions_for/latest_decision by target+verb+stage, decision uniqueness,")
print("     config.reviewer(): RATCHET_REVIEWER override beats the login-name default")
print("data dir:", root)
