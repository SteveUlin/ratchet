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


def canonical_json(obj) -> str:
    """The one serializer the producer stages (glean/dream) hash artifact records with. Stable bytes
    are load-bearing now that events/takeaways/decisions are stored by content_hash=blob_hash(record)
    (ADR-0007): a re-extraction whose logical output is unchanged must re-serialize to the SAME bytes
    so it no-ops as a version (not a spurious new one). sort_keys pins dict order; compact separators
    and ensure_ascii=False keep the bytes minimal and faithful."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


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


def latest_by_kind(source_kind: str, root: Path | None = None) -> dict[str, str]:
    """source_id -> latest content_hash, restricted to one raw `source_kind` (ADR-0007 §4).

    The kind-scoped sibling of `latest_index`: every derived view (current events, current
    takeaways, valid concepts) folds each source's version history to its newest snapshot, but
    only over the kind it cares about. Filter on `source_kind` BEFORE the fold (a thin loop over
    `iter_meta`, not over `latest_index`) so a view never loads kinds it does not need, and the
    cross-kind source_id spaces stay disjoint. Same (fetched_at, content_hash) tie-break as
    `latest_index` — max wins — so the answer is well-defined regardless of scan order and
    consistent with `latest_version`."""
    latest: dict[str, tuple[str, str]] = {}
    for m in iter_meta(root):
        if m.get("kind", "raw") != "raw" or m.get("source_kind") != source_kind:
            continue
        sid = m.get("source_id")
        if not sid:
            continue
        key = (m.get("fetched_at", ""), m.get("content_hash", ""))
        if sid not in latest or key > latest[sid]:
            latest[sid] = key
    return {sid: key[1] for sid, key in latest.items()}


def decisions_for(target: str | None, root: Path | None = None, *,
                  verb: str | None = None, stage: str | None = None) -> Iterator[dict]:
    """Yield the parsed body of every `decision` blob whose body.target == `target` (ADR-0007 §3/§4).

    The basis of every decision-fold: the current state of an artifact is the latest decision
    referencing it. target/verb/key live IN the body (meta carries only source_kind/source_id/
    fetched_at/prev), so this MUST read content, not just meta. Each yielded body is augmented
    with its `content_hash` + `fetched_at` from meta so callers can recency-fold and dedup.
    `target=None` matches ANY target — the producer-marker scan (`processed_index` folds every
    glean/dream `processed` decision to its (input, prompt_version, model) key, with no single
    target). `verb` filters body['verb']; `stage` filters body['producer']['stage'] (the
    processed-marker query keys on (stage, prompt_version, model)). O(total blobs) per ADR-0002 — an
    index is a later, deletable cache."""
    for m in iter_meta(root):
        if m.get("kind", "raw") != "raw" or m.get("source_kind") != "decision":
            continue
        ch = m.get("content_hash")
        if not ch:
            continue
        try:
            body = json.loads(get(ch, root))
        except (OSError, json.JSONDecodeError):
            continue
        if target is not None and body.get("target") != target:
            continue
        if verb is not None and body.get("verb") != verb:
            continue
        if stage is not None and (body.get("producer") or {}).get("stage") != stage:
            continue
        body = dict(body)
        body["content_hash"] = ch
        body["fetched_at"] = m.get("fetched_at", "")
        yield body


def latest_decision(target: str, root: Path | None = None) -> dict | None:
    """The single decision currently in force for `target` — the recency-fold of `decisions_for`
    to one body (max by (fetched_at, content_hash)), or None. "Current state = the latest decision
    referencing it" (ADR-0007 §3) is the most common call, so it gets a name."""
    best: dict | None = None
    best_key: tuple[str, str] | None = None
    for body in decisions_for(target, root):
        key = (body.get("fetched_at", ""), body.get("content_hash", ""))
        if best_key is None or key > best_key:
            best, best_key = body, key
    return best


def latest_decisions(root: Path | None = None) -> dict[str, dict]:
    """target -> the latest LIFECYCLE decision in force for it, folded over decisions in ONE scan — the
    batch sibling of `latest_decision` for derived views that filter MANY targets at once (the review
    queue over every takeaway, valid-concepts over every concept). Same (fetched_at, content_hash)
    recency tie-break.

    Producer `processed` markers are EXCLUDED. They are not lifecycle state, and their target space
    COLLIDES with review state: a takeaway's source_id is its `cluster_signature`, which is also what
    dream's per-cluster `processed` marker targets. Folding them in would let a later dream run's
    marker (a newer decision on the same target) shadow an `accept`/`reject`/`snooze` and resurrect a
    reviewed takeaway. Producer idempotency reads markers via the verb-scoped `decisions_for(...,
    verb='processed')`, never this fold."""
    best: dict[str, tuple[tuple[str, str], dict]] = {}
    for body in decisions_for(None, root):
        if body.get("verb") == "processed":           # producer bookkeeping, not lifecycle state
            continue
        target = body.get("target")
        if not target:
            continue
        key = (body.get("fetched_at", ""), body.get("content_hash", ""))
        if target not in best or key > best[target][0]:
            best[target] = (key, body)
    return {t: b for t, (k, b) in best.items()}


def validate_span(data: bytes, byte_start, byte_end) -> tuple[int, int] | None:
    """Re-anchor a recorded byte span at a READ boundary — the single source of the trust anchor's
    read-side check (glean writes the span; dream and review re-validate it here, never trust it).
    Accept it ONLY as in-bounds plain ints (0 <= start < end <= len) so a malformed/foreign span can
    never resolve to the whole blob or silently-clamped bytes. `bool` is an int subclass → rejected
    (a span is never a flag). Returns the (start, end) span or None."""
    if isinstance(byte_start, bool) or isinstance(byte_end, bool):
        return None
    if not (isinstance(byte_start, int) and isinstance(byte_end, int) and 0 <= byte_start < byte_end <= len(data)):
        return None
    return byte_start, byte_end


def raw_meta_of(cleaned_hash: str, root: Path | None = None, cache: dict | None = None) -> dict | None:
    """The RAW blob's meta behind a cleaned blob — THE content-addressed lineage hop (cleaned blob →
    `derived_from` → raw meta), single-sourced. Every lineage read is a field off this one dict: the
    session id (`source_id`, `session_of`), the source handle (`origin_ref.project`/`.path`,
    `project_of`), the two clock stamps (`fetched_at` + `origin_ref.mtime`, glean's stamp fill), the
    repo facet (`origin_ref` → `concepts.repo_label`, subject) — so the hop and its degrade policy
    live in exactly one spelling. An optional `cache` (keyed by cleaned_hash, holding the meta dict
    or None) memoizes across calls; sharing ONE cache across different field reads pays the hop once
    per cleaned blob. Absent/broken meta anywhere along the hop → None, never fatal — a lineage read
    degrades (the item just resolves to "unknown"), it must not crash the caller."""
    root = root or config.data_root()
    if cache is not None and cleaned_hash in cache:
        return cache[cleaned_hash]
    m = None
    try:
        raw = get_meta(cleaned_hash, root).get("derived_from")
        if raw:
            m = get_meta(raw, root)
    except (OSError, json.JSONDecodeError):
        m = None
    if cache is not None:
        cache[cleaned_hash] = m
    return m


def session_of(cleaned_hash: str, root: Path | None = None, cache: dict | None = None) -> str | None:
    """The originating session id for a cleaned blob: the raw's `source_id`, one field off
    `raw_meta_of` (the hop itself is the single-sourced thing — this is a field read; both dream and
    review resolve sessions here). `cache` is `raw_meta_of`'s (meta dicts, shareable with any other
    lineage read). Absent or broken meta → None, never fatal."""
    m = raw_meta_of(cleaned_hash, root, cache)
    return m.get("source_id") if m else None


def project_of(cleaned_hash: str, root: Path | None = None, cache: dict | None = None) -> str | None:
    """The originating SOURCE HANDLE of a cleaned blob: the raw's `origin_ref.project` (the datastore
    project-dir name `tap.read_origin` stamped), one field off `raw_meta_of` — the hop itself is the
    single-sourced thing, so dream/glean/review share one spelling for the `--source` operator filter
    (ADR-0022). `cache` is `raw_meta_of`'s (meta dicts, shareable with any other lineage read);
    absent/broken meta → None (never fatal — a `--source` run then simply doesn't match that item).
    One meta hop per call, no content read, no LLM.

    A DOCUMENT source (ADR-0031) carries no `project` — deliberately, because `origin_ref.project`
    also feeds the repo facet (`concepts.repo_label`) and a document must stay subject-empty — so
    its FOCUS handle falls back to `origin_ref.path`: `--source CLAUDE.md` selects the document's
    chunks/events without ever granting it a repo identity."""
    m = raw_meta_of(cleaned_hash, root, cache)
    if not m:
        return None
    origin = m.get("origin_ref") or {}
    return origin.get("project") or origin.get("path")


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
