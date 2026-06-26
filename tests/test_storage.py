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

print("OK — ingest dedup, header data, meta-as-truth versioning, crash-orphan repair,")
print("     derived primitive (content-addressed, lineage-tagged, TTL, out of TimeMap),")
print("     byte-faithful \\r round-trip, symmetric raw/derived collision guard")
print("data dir:", root)
