"""tap — locate new/changed Claude Code transcripts and copy them into the blobstore.

Datastore (mutable, external) -> blobstore (immutable, versioned). No LLM. Idempotent: a
re-run reads only changed files and copies only new content. One unreadable file never aborts
the run.

    python -m ratchet.tap --dry-run        # show what would be copied
    python -m ratchet.tap                  # copy new transcripts into the blobstore
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import blobstore, config

DEFAULT_DATASTORE = Path.home() / ".claude" / "projects"


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


def _latest_index(root: Path) -> dict[str, tuple]:
    """source_id -> (fetched_at, hash) for the newest committed blob, built in ONE scan."""
    idx: dict[str, tuple] = {}
    for m in blobstore.iter_meta(root):
        sid = m.get("source_id")
        key = (m.get("fetched_at", ""), m.get("content_hash", ""))
        if sid and (sid not in idx or key > idx[sid]):
            idx[sid] = key
    return idx


def tap(datastore: Path, project: str | None = None, limit: int | None = None,
        dry_run: bool = False) -> str:
    run_id = f"tap-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    root = config.ensure_layout()
    _sweep_partials(root)
    state = load_fetch_state(root)
    latest = _latest_index(root)  # source_id -> (fetched_at, hash); kept current in-run
    located = copied = skipped = errored = 0

    for path in discover(datastore, project):
        located += 1
        if limit is not None and copied >= limit:
            continue
        key = str(path)
        try:
            st = path.stat()
            fp = [st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()]
            prior = state.get(key)
            if prior is not None and prior[:2] == fp:
                skipped += 1
                continue  # cheap tier: (size, mtime) unchanged since last tap — no read

            text, origin = read_origin(path)
            sid = origin["session_id"]
            h = blobstore.blob_hash(text)
            state[key] = [*fp, h]  # update cursor even on a dedup-skip (no re-read next run)

            if blobstore.has(h, root):
                skipped += 1
                continue  # content already stored (e.g. touched or reverted)

            prev = latest.get(sid, ("", None))[1]
            ver = "v1" if prev is None else f"v+ (prev {prev[:8]})"
            if dry_run:
                print(f"would copy {path.name}  {origin['lines']:>5} lines  {h[:12]}  {ver}")
                copied += 1
                continue
            fetched_at = datetime.now(timezone.utc).isoformat()
            blobstore.ingest(text, source_kind="transcript", source_id=sid, origin_ref=origin,
                             fetched_at=fetched_at, prev=prev, h=h, root=root)
            latest[sid] = (fetched_at, h)
            copied += 1
            print(f"copied {path.name}  {origin['lines']:>5} lines  {h[:12]}  {ver}")
        except OSError as e:
            errored += 1
            print(f"skip   {path.name}  (unreadable: {type(e).__name__})")
            continue

    if not dry_run:
        save_fetch_state(root, state)
    tail = f", errored {errored}" if errored else ""
    print(f"\n{run_id}: located {located}, copied {copied}, skipped {skipped} (unchanged){tail}")
    return run_id


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="tap",
                                 description="Copy new Claude Code transcripts into the blobstore.")
    ap.add_argument("--datastore", type=Path, default=DEFAULT_DATASTORE,
                    help=f"transcript root (default: {DEFAULT_DATASTORE})")
    ap.add_argument("--project", help="only project dirs whose name contains this string")
    ap.add_argument("--limit", type=int, help="cap number copied this run")
    ap.add_argument("--dry-run", action="store_true", help="show what would be copied; no writes")
    args = ap.parse_args(argv)
    tap(args.datastore, project=args.project, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
