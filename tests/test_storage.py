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

print("OK — ingest dedup, header data, meta-as-truth versioning, crash-orphan repair")
print("data dir:", root)
