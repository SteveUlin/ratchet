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
    return _paths(h, root or config.data_root())[0].read_text(encoding="utf-8")


def get_meta(h: str, root: Path | None = None) -> dict:
    return json.loads(_paths(h, root or config.data_root())[1].read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: str, root: Path) -> None:
    tmpdir = root / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmpdir, suffix=".partial")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic within the filesystem (tmp + blobs share `root`)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


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
    """Freeze `text` as an immutable blob (content first, meta last = commit). Returns
    (hash, written); an already-committed hash is a no-op. Internal — callers use `ingest`."""
    root = root or config.data_root()
    h = h or blob_hash(text)
    content, meta = _paths(h, root)
    if meta.exists():  # already committed
        return h, False
    record = {
        "content_hash": h,
        "source_kind": source_kind,
        "source_id": source_id,
        "origin_ref": origin_ref,
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "prev": prev,
        "bytes": len(text.encode("utf-8")),
    }
    content.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(content, text, root)                                            # 1. content
    _atomic_write(meta, json.dumps(record, ensure_ascii=False, indent=2), root)   # 2. commit
    return h, True


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
        return h, False
    if prev is _DERIVE:
        prev = latest_version(source_id, root)
    return _put(text, source_kind=source_kind, source_id=source_id, origin_ref=origin_ref,
                prev=prev, fetched_at=fetched_at, h=h, root=root)
