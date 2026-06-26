"""Immutable, content-addressed, versioned blobstore.

A blob is a frozen snapshot of one fetched artifact. Blobs are immutable; the logical source
behind them evolves, so each blob's META sidecar records a stable `source_id` plus
`fetched_at` and a `prev` link to the previous snapshot. The META sidecars ARE the source of
truth — the per-source version history (a Memento TimeMap) is derived by scanning them. There
is no separate ledger to desync (ADR-0002).

Crash-safety: a blob is written content-first, then the meta sidecar last as the COMMIT
marker, each via temp + atomic rename. `has()` keys on the meta, so a crash after the content
write but before the meta leaves only a harmless orphan content file (invisible to every
reader) that the next run overwrites and commits. Reconciling source *updates* across versions
is deferred (ADR-0001/0002).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import config

_DERIVE = object()  # sentinel: ingest derives `prev` itself unless the caller supplies it


def blob_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _paths(h: str, root: Path) -> tuple[Path, Path]:
    d = root / "blobs" / h[:2]
    return d / h, d / f"{h}.meta.json"


def has(h: str, root: Path | None = None) -> bool:
    """True iff the blob is fully committed — its meta sidecar (written last) exists."""
    return _paths(h, root or config.data_root())[1].exists()


def get(h: str, root: Path | None = None) -> str:
    # read_bytes + decode, NOT read_text: read_text opens in universal-newline mode and would
    # rewrite \r\n→\n and lone \r→\n on read, corrupting content and breaking content-addressing
    # (`blob_hash(get(h)) != h`). A cleaned blob holds real \r decoded from rendered tool output
    # (HTTP headers, terminal control), so this is load-bearing, not theoretical.
    return _paths(h, root or config.data_root())[0].read_bytes().decode("utf-8")


def get_meta(h: str, root: Path | None = None) -> dict:
    return json.loads(_paths(h, root or config.data_root())[1].read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: str, root: Path) -> None:
    tmpdir = root / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmpdir, suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as f:  # bytes, not text: no newline translation (see `get`)
            f.write(data.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic within the filesystem (tmp + blobs share `root`)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit(h: str, text: str, record: dict, root: Path) -> tuple[str, bool]:
    """Write content first, then the meta sidecar last as the COMMIT marker, each via temp +
    atomic rename. Returns (hash, written); an already-committed hash (meta present) is a no-op.
    Shared by the raw (`_put`) and derived (`put_derived`) paths."""
    content, meta = _paths(h, root)
    if meta.exists():  # already committed
        existing = json.loads(meta.read_text(encoding="utf-8"))
        if existing.get("kind", "raw") != record.get("kind", "raw"):
            # raw and derived share one hash→meta namespace; identical bytes across kinds would
            # otherwise silently no-op and drop the new lineage. Fail loud instead (near-impossible:
            # a rendered doc byte-identical to a raw .jsonl), never mis-tag a committed blob.
            raise ValueError(
                f"blob {h[:12]} already committed as kind={existing.get('kind', 'raw')!r}; "
                f"refusing to re-commit as kind={record.get('kind')!r}")
        return h, False
    content.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(content, text, root)                                            # 1. content
    _atomic_write(meta, json.dumps(record, ensure_ascii=False, indent=2), root)   # 2. commit
    return h, True


def _put(
    text: str,
    *,
    source_kind: str,
    source_id: str,
    origin_ref: dict,
    prev: str | None = None,
    fetched_at: str | None = None,
    h: str | None = None,
    root: Path | None = None,
) -> tuple[str, bool]:
    """Freeze `text` as an immutable RAW blob (ground truth, kept forever). Internal — callers
    use `ingest`."""
    root = root or config.data_root()
    h = h or blob_hash(text)
    record = {
        "content_hash": h,
        "kind": "raw",
        "source_kind": source_kind,
        "source_id": source_id,
        "origin_ref": origin_ref,
        "fetched_at": fetched_at or _now(),
        "prev": prev,
        "bytes": len(text.encode("utf-8")),
    }
    return _commit(h, text, record, root)


def put_derived(
    text: str,
    *,
    source_kind: str,
    derived_from: str,
    produced_by: str,
    render_version: str,
    fmt: str,
    tags: dict | None = None,
    expires_at: str | None = None,
    created_at: str | None = None,
    h: str | None = None,
    root: Path | None = None,
) -> tuple[str, bool]:
    """Freeze a DERIVED artifact — a deterministic function of an immutable blob — as its own
    content-addressed blob. Returns (hash, written); an existing hash is a no-op.

    Unlike a raw blob (ground truth, kept forever) a derived blob is rebuildable from
    `derived_from` + `render_version`, so it is TTL-eligible (`expires_at`) — yet still immutable
    in place, which keeps provenance spans sound. The sidecar is self-describing (`kind`,
    `source_kind`, `format`, `render_version`, `produced_by`, `derived_from`, `tags`) so a consumer
    can skip/use without reading the content (ADR-0003)."""
    root = root or config.data_root()
    h = h or blob_hash(text)
    record = {
        "content_hash": h,
        "kind": "derived",
        "source_kind": source_kind,
        "derived_from": derived_from,
        "produced_by": produced_by,
        "render_version": render_version,
        "format": fmt,
        "created_at": created_at or _now(),
        "expires_at": expires_at,
        "tags": tags or {},
        "bytes": len(text.encode("utf-8")),
    }
    return _commit(h, text, record, root)


def iter_meta(root: Path | None = None) -> Iterator[dict]:
    """Yield every committed blob's meta sidecar — the source of truth for versioning."""
    root = root or config.data_root()
    blobs = root / "blobs"
    if not blobs.exists():
        return
    for shard in sorted(blobs.iterdir()):
        if not shard.is_dir():
            continue
        for meta in sorted(shard.glob("*.meta.json")):
            try:
                yield json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue


def latest_index(root: Path | None = None) -> dict[str, tuple[str, str]]:
    """source_id -> (fetched_at, content_hash) of its newest blob, in ONE scan over the meta
    sidecars. Ties on `fetched_at` break deterministically by hash, so the answer is
    well-defined regardless of scan order."""
    idx: dict[str, tuple[str, str]] = {}
    for m in iter_meta(root):
        sid = m.get("source_id")
        if not sid:
            continue
        key = (m.get("fetched_at", ""), m.get("content_hash", ""))
        if sid not in idx or key > idx[sid]:
            idx[sid] = key
    return idx


def latest_version(source_id: str, root: Path | None = None) -> str | None:
    """Most recent committed blob hash for a logical source, or None."""
    key = latest_index(root).get(source_id)
    return key[1] if key else None


def derived_for(derived_from: str, root: Path | None = None, *,
                fmt: str | None = None) -> Iterator[dict]:
    """Yield the meta of every derived blob produced from `derived_from` (optionally filtered by
    `format`) — the downstream lineage of a raw blob, by one scan over the sidecars. No index
    (ADR-0002): add one only when a scan actually hurts."""
    for m in iter_meta(root):
        if m.get("kind") == "derived" and m.get("derived_from") == derived_from:
            if fmt is None or m.get("format") == fmt:
                yield m


def ingest(
    text: str,
    *,
    source_kind: str,
    source_id: str,
    origin_ref: dict,
    fetched_at: str | None = None,
    prev=_DERIVE,
    h: str | None = None,
    root: Path | None = None,
) -> tuple[str, bool]:
    """Copy `text` as a versioned snapshot of `source_id` iff new. Returns (hash, written).

    Identical content already committed => no-op. A changed source becomes a new immutable
    snapshot whose meta links `prev` to the latest known version. `h`/`prev` may be supplied
    by the caller to avoid recomputation (tap does this); otherwise `prev` is derived.
    """
    root = root or config.data_root()
    h = h or blob_hash(text)
    if has(h, root):
        if get_meta(h, root).get("kind", "raw") != "raw":   # symmetric with _commit's guard:
            raise ValueError(f"blob {h[:12]} already committed as derived; "  # never silently drop
                             f"refusing to ingest as raw")                    # a raw snapshot
        return h, False
    if prev is _DERIVE:
        prev = latest_version(source_id, root)
    return _put(text, source_kind=source_kind, source_id=source_id, origin_ref=origin_ref,
                prev=prev, fetched_at=fetched_at, h=h, root=root)
