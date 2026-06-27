"""tap — locate new/changed Claude Code transcripts and copy them into the blobstore.

Datastore (mutable, external) -> blobstore (immutable, versioned). No LLM. Idempotent: a
re-run reads only changed files and copies only new content. One unreadable file never aborts
the run.

    python -m ratchet.tap --dry-run        # show what would be copied
    python -m ratchet.tap                  # copy new transcripts into the blobstore

tap runs on the uniform `block` substrate (ADR-0009): the item is a transcript file (surfaced
through the fingerprint cursor), `process` ingests its raw blob, and `block.run` gives tap the
same `--all`/`--source-id`/`--limit`/`--dry-run`/streaming-progress surface as every stage. Two
dedup tiers stack: the (size, mtime, hash) cursor — tap's REAL idempotency, surviving a
content-identical touch the processed marker cannot express — filters unchanged files INSIDE
`items()` (so they are not even examined); the blobstore's content-addressing no-ops a
re-ingest of identical bytes. The per-session processed marker is written for Report uniformity;
it is cosmetic (the cursor already skipped). cost is always 0 (no LLM) — `--max-usd` is inert.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import blobstore, block, config

DEFAULT_DATASTORE = Path.home() / ".claude" / "projects"
OUT_NOUN = "raw"   # the per-item output noun the Progress bar/line shows (tap copies raw transcript blobs)


def discover(datastore: Path, project: str | None = None) -> Iterator[Path]:
    """Yield transcript `.jsonl` paths under the datastore, optionally filtered by project dir."""
    if not datastore.exists():
        return
    for proj_dir in sorted(datastore.iterdir()):
        if not proj_dir.is_dir():
            continue
        if project and project not in proj_dir.name:
            continue
        yield from sorted(proj_dir.glob("*.jsonl"))


def read_origin(path: Path) -> tuple[str, dict]:
    """Return (raw text, origin_ref backlink) for a transcript file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    cwd = branch = None
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = cwd or rec.get("cwd")
        branch = branch or rec.get("gitBranch")
        if cwd:
            break
    st = path.stat()
    origin = {
        "path": str(path),
        "project": path.parent.name,
        "session_id": path.stem,
        "cwd": cwd,
        "git_branch": branch,
        "size_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "lines": len(lines),
    }
    return text, origin


# --- tap-owned mutable fetch-state cursor (the crawler's "last-seen fingerprint"), kept
#     separate from the immutable blobstore so a touched/reverted file is not re-read forever ---

def _state_path(root: Path) -> Path:
    return root / "state" / "fetch_state.json"


def load_fetch_state(root: Path) -> dict[str, list]:
    p = _state_path(root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_fetch_state(root: Path, state: dict) -> None:
    # No fsync: the cursor is a rebuildable optimization, not ground truth — a lost write
    # just forces one re-read next run.
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".partial")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _sweep_partials(root: Path) -> None:
    """Reclaim temp files leaked by a hard crash mid-write (disk hygiene)."""
    tmp = root / "tmp"
    if tmp.exists():
        for f in tmp.glob("*.partial"):
            try:
                f.unlink()
            except OSError:
                pass


# --- the Block: tap as a uniform stage (item = a transcript file via its cursor; ADR-0009) -----

class TapBlock:
    """tap as a `block.Block` — the discover→ingest sweep, on the shared driver. The item is a
    transcript `.jsonl` file (a `Path`); `process` reads it and ingests its raw blob. cost is always
    0 (no LLM), so `--max-usd` is inert.

    Two dedup tiers, and which one is authoritative matters:

    - The (size, mtime, hash) FINGERPRINT CURSOR is tap's REAL idempotency. Its cheap (size, mtime)
      tier is consulted INSIDE `items()`, so an unchanged file is filtered before it ever reaches the
      driver — it is not even examined. The cursor MUST survive a content-identical touch (an mtime
      bump that does not change bytes); after one re-read, the cursor records the new mtime and the
      file is cheap-skipped forever. So the cursor stays the dedup mechanism; do not mistake the
      marker for it.
    - The per-item PROCESSED MARKER is written for Report uniformity (skipped/processed reported the
      same way as every stage). Its `target` is the session id (the lineage-readable id), but the
      done-KEY must encode the file's (size, mtime) FINGERPRINT — see `key()` for why: keying the
      marker on the bare session id would make the shared driver's done-skip permanently retire a
      session after its first ingest, silently DROPPING a later content change before `process` could
      ingest the new version. tap has no run-level idempotency params (params=()), so the only place a
      per-item content discriminator can live is the key itself.

    Crash-safety here is the blobstore's content-then-meta (per-item, automatic); the processed marker
    is informational. The cursor is a per-run rebuildable optimization saved ONCE in `finalize` (no
    fsync) — a lost write just forces one re-read next run."""

    name = "tap"
    commits_per_item = True
    marker_extra = block.no_marker_extra     # no per-item audit fields
    # params is EMPTY: tap has no prompt/model/render version (no logic version to bump). The per-item
    # content discriminator lives in key() instead (params is a run-level constant, so it cannot carry
    # a per-file fingerprint — the done-key's only per-item part is key(item)).
    params: tuple[tuple[str, str], ...] = ()

    def __init__(self, datastore: Path = DEFAULT_DATASTORE, project: str | None = None) -> None:
        # datastore/project scope the enumeration (tap-specific, not block.run knobs). The cursor +
        # in-run latest index live on the instance so items()/process()/finalize() share them; they
        # are (re)loaded at items() start so a re-used instance always reflects on-disk state.
        self.datastore = datastore
        self.project = project
        self._state: dict[str, list] = {}
        self._latest: dict[str, tuple[str, str]] = {}
        self._dirty = False

    def items(self, root: Path, *, source_id: str | None = None) -> Iterator[tuple[Path, list]]:
        """Yield (path, fingerprint) for each transcript file to ingest. The cheap (size, mtime)
        cursor tier runs HERE, so an unchanged file is filtered before the driver examines it (it is
        not counted). The fingerprint is computed once (from the same stat the cheap tier reads) and
        carried with the item so key()/process() never re-stat. --source-id scopes to one session
        (path.stem == session id); --all (default) sweeps the datastore.

        A file that fails to stat is yielded with an empty fingerprint so process() raises on the read
        and the driver isolates it as errored — never silently dropped."""
        _sweep_partials(root)                       # disk hygiene at run start (leaked .partial temps)
        self._state = load_fetch_state(root)
        self._latest = blobstore.latest_index(root)  # source_id -> (fetched_at, hash); current in-run
        self._dirty = False
        for path in discover(self.datastore, self.project):
            if source_id is not None and path.stem != source_id:
                continue
            try:
                st = path.stat()
            except OSError:
                yield (path, [])                    # unstatable → process() raises → errored
                continue
            fp = [st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()]
            prior = self._state.get(str(path))
            if prior is not None and prior[:2] == fp:
                continue  # cheap tier: (size, mtime) unchanged since last tap — never examined
            yield (path, fp)

    def key(self, item: tuple[Path, list]) -> str:
        """The done-key target: the session id PLUS the (size, mtime) fingerprint. Keying on the bare
        session id would make the shared driver's done-skip retire a session after its first ingest,
        dropping a later content change before process() runs (the contract's "key == session id"
        overlooks that the driver's done-skip is unconditional). A content change bumps size → a new
        key → re-processed → the new version ingested. A pure touch (mtime only) bumps mtime → a new
        key too, but that file is read exactly once (then the cursor cheap-skips it), so the re-key is
        harmless (the re-ingest no-ops on the unchanged hash). The key is computable from stat() alone
        (no read), so the done-skip stays cheap and the read stays in process() for error isolation."""
        path, fp = item
        return f"{path.stem}:{fp[0]}:{fp[1]}" if fp else path.stem

    def process(self, item: tuple[Path, list], *, root: Path, run_id: str) -> tuple[int, float]:
        """Read the transcript, update the cursor, ingest its raw blob if new. Returns (1, 0.0) on a
        fresh snapshot, (0, 0.0) if the content already exists. cost is always 0 (no LLM). A raising
        read_origin (unreadable file) propagates → the driver isolates it as errored, no marker, the
        file is retried next run."""
        path, _fp = item
        text, origin = read_origin(path)
        sid = origin["session_id"]
        h = blobstore.blob_hash(text)
        # update the cursor even on a dedup-skip: the (size, mtime, hash) record means next run's cheap
        # tier matches and skips without a re-read. Re-stat (read_origin may have raced a write) so the
        # cursor records the fingerprint of the bytes actually read. self._dirty flags finalize.
        st = path.stat()
        fp = [st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()]
        self._state[str(path)] = [*fp, h]
        self._dirty = True

        if blobstore.has(h, root):
            return (0, 0.0)  # content already stored (e.g. a touched or reverted file)
        prev = self._latest.get(sid, ("", None))[1]
        fetched_at = config.now()
        blobstore.ingest(text, source_kind="transcript", source_id=sid, origin_ref=origin,
                         fetched_at=fetched_at, prev=prev, h=h, root=root)
        self._latest[sid] = (fetched_at, h)
        return (1, 0.0)

    def finalize(self, *, root: Path, run_id: str) -> None:
        """Flush the fingerprint cursor ONCE after the loop — its single end-of-run save (the cursor
        is a per-run rebuildable optimization, no fsync). The driver hands `finalize` NO item list (#6);
        tap tracks its own dirty cursor state on the instance (`self._dirty`/`self._state`). tap uses
        finalize purely for the cursor flush; blob commits stay per-item (the blobstore's content-then-meta)."""
        if self._dirty:
            save_fetch_state(root, self._state)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="tap",
                                 description="Copy new Claude Code transcripts into the blobstore.")
    # tap-specific enumeration scoping (passed into the block instance, not block.run):
    ap.add_argument("--datastore", type=Path, default=DEFAULT_DATASTORE,
                    help=f"transcript root (default: {DEFAULT_DATASTORE})")
    ap.add_argument("--project", help="only project dirs whose name contains this string")
    # uniform block surface (per ADR-0009):
    ap.add_argument("--source-id", help="ingest just this session (path.stem == session id)")
    ap.add_argument("--all", action="store_true",
                    help="sweep the whole datastore (default when no --source-id)")
    ap.add_argument("--limit", type=int, help="cap items EXAMINED this run")
    ap.add_argument("--dry-run", action="store_true", help="list what would be copied; no writes")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--max-usd", type=float, help="(no cost; inert — tap never calls an LLM)")
    args = ap.parse_args(argv)

    blk = TapBlock(datastore=args.datastore, project=args.project)
    # the stage owns its Progress now (the driver only speaks the protocol). None for --quiet/--dry-run;
    # else built from this stage's args + OUT_NOUN. tap has params=() and no LLM cost (cap omitted).
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, params=dict(blk.params), out_noun=OUT_NOUN, verbose=args.verbose)
    block.run(blk, source_id=args.source_id, max_usd=args.max_usd, limit=args.limit,
              dry_run=args.dry_run, progress=progress)


if __name__ == "__main__":
    main()
