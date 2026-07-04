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

DOCUMENT MODE (`--file PATH`, ADR-0031): ingest an explicit file — the owner's hand-written
CLAUDE.md today, a fetched PDF/webpage dump tomorrow — VERBATIM as a `document` raw blob. The
file's absolute path is its `source_id` AND its session identity (see `read_document`); the same
fingerprint cursor and version fold apply, so a re-tap of an unchanged file no-ops and a changed
file mints a new VERSION of the same source.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import blobstore, block, config

DEFAULT_DATASTORE = Path.home() / ".claude" / "projects"
OUT_NOUN = "raw"   # the per-item output noun the Progress bar/line shows (tap copies raw transcript blobs)


def encode_project(path: Path) -> str:
    """The project-dir name Claude Code derives from a session's cwd: every '/' and '.' becomes '-'
    (so `/home/sulin/.local/share/ratchet` → `-home-sulin--local-share-ratchet`). Reproduced here for
    ONE purpose — recognizing the dir ratchet's OWN `claude -p` completer calls land in. The completer
    runs with `cwd=data_root` (completer.py), so Claude Code logs each extract/route call as a session
    under `encode_project(data_root)`; without skipping it, the next `tap` re-ingests ratchet's own
    generated runs as if they were learnings. This couples to a Claude Code naming convention — a
    pragmatic read-side heuristic, not a contract; the `--include-self` escape hatch exists for when it
    is wrong, and `--exclude` covers anything else (e.g. `-tmp-*` test fixtures)."""
    return re.sub(r"[/.]", "-", str(path))


def discover(datastore: Path, project: str | None = None, *,
             exclude: tuple[str, ...] = (), skip_self: Path | None = None) -> Iterator[Path]:
    """Yield transcript `.jsonl` paths under the datastore. `project` keeps only dirs whose name CONTAINS
    it; `exclude` drops any dir whose name contains any listed substring; `skip_self` (a path, normally
    `data_root`) drops the dir ratchet's own completer runs land in — that dir and any nested-cwd
    children (`encode_project(skip_self)` and `…-`-prefixed names). The skip is a default the operator
    can lift; it is NOT a silent hard rule."""
    if not datastore.exists():
        return
    self_name = encode_project(skip_self) if skip_self is not None else None
    for proj_dir in sorted(datastore.iterdir()):
        if not proj_dir.is_dir():
            continue
        name = proj_dir.name
        if project and project not in name:
            continue
        if self_name is not None and (name == self_name or name.startswith(self_name + "-")):
            continue  # ratchet's own claude -p runs (cwd inside data_root) — don't eat our own tail
        if any(x in name for x in exclude):
            continue
        yield from sorted(proj_dir.glob("*.jsonl"))


def _parse_since(s: str) -> datetime:
    """Parse a `--since` selector (an ISO date or datetime) to a tz-aware UTC cutoff. A bare date
    (`2026-06-01`) reads as midnight UTC; a naive datetime reads as UTC — file mtimes are compared in
    UTC (`datetime.fromtimestamp(mtime, timezone.utc)`). Raises `ValueError` on an unparseable string;
    `main()` catches it and `ap.error`s, so a bad date fails fast rather than silently selecting nothing."""
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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


def read_document(path: Path) -> tuple[str, dict]:
    """Return (verbatim text, origin_ref backlink) for a `--file` document (ADR-0031).

    VERBATIM: the raw blob is the TRUE file bytes, so the decode is STRICT — a non-UTF-8 file
    raises, the driver isolates it as errored, and nothing mangled ever enters the store (contrast
    `read_origin`'s errors="replace", right for transcripts where one bad byte must not cost a
    whole session). `mtime` is the file's save time — the document's VALID-TIME (when the owner
    last asserted its content), the clock recency/decay weighting reads.

    SESSION IDENTITY — the document's epistemology, deliberate (ADR-0031): the session is the
    file's stable PATH, never per-version — ALL versions of one file are ONE session. Maturity
    counts DISTINCT sessions, so a document asserts a rule ONCE no matter how often it is saved or
    re-tapped: resolve's exact-dup fast path deterministically corroborates a re-tapped identical
    rule into its claim at ZERO added maturity (same session), an edited rule seeds a new claim or
    adjudicates against LIVED claims (those carry different sessions — allowed), and maturity comes
    only from the owner's real sessions living the rule, or his direct accept at review.

    No `project`/`cwd` — also deliberate: `origin_ref.project` feeds the repo facet
    (`concepts._repo_label`), and a document must stay subject-EMPTY (seed-only via subject,
    scope-derives-global); its `--topic` FOCUS handle is the `path` fallback in
    `blobstore.project_of` instead."""
    text = path.read_text(encoding="utf-8")
    st = path.stat()
    origin = {
        "path": str(path),
        "session_id": str(path),   # path-as-session: one file = one session, forever (see above)
        "size_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
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
    priority = block.no_priority             # arrival order — no salience to prioritize on
    age = block.no_age                       # cheap deterministic stage — aging is moot (ADR-0021)
    # params is EMPTY: tap has no prompt/model/render version (no logic version to bump). The per-item
    # content discriminator lives in key() instead (params is a run-level constant, so it cannot carry
    # a per-file fingerprint — the done-key's only per-item part is key(item)).
    params: tuple[tuple[str, str], ...] = ()

    def __init__(self, datastore: Path = DEFAULT_DATASTORE, project: str | None = None,
                 last: int | None = None, since: str | None = None,
                 exclude: tuple[str, ...] = (), skip_self: Path | None = None,
                 files: tuple[Path, ...] = ()) -> None:
        # datastore/project/last/since scope the enumeration (tap-specific FETCH SELECTION, not block.run
        # knobs — selection is per-source, owned by the fetcher; ADR-0022). `--last`/`--since` SELECT which
        # to-ingest candidates exist; the driver's `--limit` caps how many are EXAMINED — distinct levers,
        # both useful (e.g. `--last 200` then a smaller `--limit` per tick). The cursor + in-run latest
        # index live on the instance so items()/process()/finalize() share them; they are (re)loaded at
        # items() start so a re-used instance always reflects on-disk state.
        self.datastore = datastore
        self.project = project
        self.last = last
        self.since = since
        self.exclude = exclude
        self.skip_self = skip_self
        # DOCUMENT MODE (--file, ADR-0031): a non-empty `files` REPLACES the datastore sweep with this
        # explicit list. Normalized to absolute, resolved paths at construction because the path IS the
        # source/session identity — `~/x` and `/home/…/x` must be ONE session, not two.
        self.files = tuple(Path(f).expanduser().resolve() for f in files)
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
        and the driver isolates it as errored — never silently dropped.

        DOCUMENT MODE (`files` set): the explicit list IS the selection, so the sweep's selectors
        (project/last/since/skip_self) don't apply — only the cursor cheap tier does, identically.
        --source-id matches the full path (the document's session id), not a stem."""
        _sweep_partials(root)                       # disk hygiene at run start (leaked .partial temps)
        self._state = load_fetch_state(root)
        self._latest = blobstore.latest_index(root)  # source_id -> (fetched_at, hash); current in-run
        self._dirty = False
        if self.files:
            for path in self.files:
                if source_id is not None and str(path) != source_id:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    yield (path, [])                # unstatable → process() raises → errored, retried
                    continue
                fp = [st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()]
                prior = self._state.get(str(path))
                if prior is not None and prior[:2] == fp:
                    continue  # cheap tier: (size, mtime) unchanged since last tap — never examined
                yield (path, fp)
            return
        since_dt = _parse_since(self.since) if self.since else None
        # FETCH SELECTION (`--last`/`--since`, ADR-0022) narrows the SURVIVORS of the cursor skip — "the
        # last N I haven't already pulled" / "only since this date". It buffers candidates ONLY when a
        # selector is active; with neither set the loop stays a pure stream, so a no-knob run is
        # byte-identical to before (same items, same inline order — incl. an unstatable file's position).
        selecting = self.last is not None or since_dt is not None
        buf: list[tuple[Path, list, float]] = []     # (path, fingerprint, raw mtime) survivors to select over
        for path in discover(self.datastore, self.project,
                             exclude=self.exclude, skip_self=self.skip_self):
            if source_id is not None and path.stem != source_id:
                continue
            try:
                st = path.stat()
            except OSError:
                yield (path, [])                    # unstatable → process() raises → errored. Never
                continue                            # suppressed by --last/--since (it has no mtime to select on)
            fp = [st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()]
            prior = self._state.get(str(path))
            if prior is not None and prior[:2] == fp:
                continue  # cheap tier: (size, mtime) unchanged since last tap — never examined
            if not selecting:
                yield (path, fp)
                continue
            if since_dt is not None and datetime.fromtimestamp(st.st_mtime, timezone.utc) < since_dt:
                continue                            # --since: modified before the cutoff — not selected
            buf.append((path, fp, st.st_mtime))
        if selecting:
            if self.last is not None:               # --last N: the N most-recently-MODIFIED survivors. Stable
                buf.sort(key=lambda c: c[2], reverse=True)   # sort by mtime desc; ties keep discover order
                buf = buf[:self.last]
            for path, fp, _mtime in buf:
                yield (path, fp)

    def key(self, item: tuple[Path, list]) -> str:
        """The done-key target: the session id PLUS the (size, mtime) fingerprint. Keying on the bare
        session id would make the shared driver's done-skip retire a session after its first ingest,
        dropping a later content change before process() runs (the contract's "key == session id"
        overlooks that the driver's done-skip is unconditional). A content change bumps size → a new
        key → re-processed → the new version ingested. A pure touch (mtime only) bumps mtime → a new
        key too, but that file is read exactly once (then the cursor cheap-skips it), so the re-key is
        harmless (the re-ingest no-ops on the unchanged hash). The key is computable from stat() alone
        (no read), so the done-skip stays cheap and the read stays in process() for error isolation.

        A DOCUMENT's key carries the FULL path (its session id) — the stem would collide every
        repo's `CLAUDE.md` into one done-target."""
        path, fp = item
        sid = str(path) if self.files else path.stem
        return f"{sid}:{fp[0]}:{fp[1]}" if fp else sid

    def process(self, item: tuple[Path, list], *, root: Path, run_id: str) -> tuple[int, float]:
        """Read the file, update the cursor, ingest its raw blob if new. Returns (1, 0.0) on a
        fresh snapshot, (0, 0.0) if the content already exists. cost is always 0 (no LLM). A raising
        read (unreadable file, or a non-UTF-8 document) propagates → the driver isolates it as
        errored, no marker, the file is retried next run. Document mode differs only in the reader
        (`read_document`: verbatim, path-as-session) and the raw blob's `source_kind`; the cursor,
        version fold (`prev`), and content-address dedup are shared."""
        path, _fp = item
        if self.files:
            text, origin = read_document(path)
            source_kind = "document"
        else:
            text, origin = read_origin(path)
            source_kind = "transcript"
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
        blobstore.ingest(text, source_kind=source_kind, source_id=sid, origin_ref=origin,
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
    ap.add_argument("--exclude", action="append", default=[], metavar="SUBSTR",
                    help="skip project dirs whose name contains this substring (repeatable; e.g. -tmp- test fixtures)")
    ap.add_argument("--include-self", action="store_true",
                    help="DON'T auto-skip ratchet's own data-dir project (its claude -p completer runs); "
                         "by default tap skips it so it never re-ingests its own generated transcripts")
    # FETCH SELECTION (ADR-0022) — owned by the fetcher (selection is per-source), distinct from the
    # driver's --limit (which caps items EXAMINED): --last/--since SELECT which to-ingest candidates exist.
    ap.add_argument("--last", type=int, metavar="N",
                    help="FETCH SELECTION: only the N most-recently-MODIFIED to-ingest files (after the cursor skip)")
    ap.add_argument("--since", metavar="ISO-DATE",
                    help="FETCH SELECTION: only files modified at/after this ISO date (e.g. 2026-06-01)")
    # DOCUMENT MODE (ADR-0031) — an explicit-file source, replacing the datastore sweep:
    ap.add_argument("--file", action="append", type=Path, default=[], metavar="PATH",
                    help="DOCUMENT MODE: ingest this file verbatim as a `document` source (repeatable; "
                         "e.g. ~/.claude/CLAUDE.md). The file's absolute path is its source id AND its "
                         "session — all versions of one file are ONE session, so a document can never "
                         "self-mature by re-taps (ADR-0031). Replaces the datastore sweep; the same "
                         "cursor makes re-taps of an unchanged file no-ops")
    # uniform block surface (per ADR-0009):
    ap.add_argument("--source-id", help="ingest just this session (path.stem == session id; "
                                        "with --file: the full path)")
    ap.add_argument("--all", action="store_true",
                    help="sweep the whole datastore (default when no --source-id)")
    ap.add_argument("--limit", type=int, help="cap items EXAMINED this run")
    ap.add_argument("--dry-run", action="store_true", help="list what would be copied; no writes")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--max-usd", type=float, help="(no cost; inert — tap never calls an LLM)")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy (inert — tap emits no per-item signal, so every policy is arrival order)")
    args = ap.parse_args(argv)
    if args.since is not None:                       # fail fast on a bad date rather than silently selecting nothing
        try:
            _parse_since(args.since)
        except ValueError:
            ap.error(f"--since {args.since!r} is not an ISO date/datetime (e.g. 2026-06-01)")
    if args.file:
        # the datastore-sweep selectors would be silently inert alongside an explicit file list —
        # refuse the combination rather than hide the rule (the ADR-0027 posture).
        for flag, val in (("--project", args.project), ("--last", args.last),
                          ("--since", args.since), ("--exclude", args.exclude or None)):
            if val is not None:
                ap.error(f"{flag} selects datastore transcripts; --file names its files explicitly "
                         f"— the two don't compose (run them as separate taps)")

    # By default skip the project dir ratchet's OWN completer runs land in (cwd=data_root) so tap never
    # re-ingests its generated `claude -p` transcripts; `--include-self` lifts it. (ADR-0025)
    skip_self = None if args.include_self else config.data_root()
    blk = TapBlock(datastore=args.datastore, project=args.project, last=args.last, since=args.since,
                   exclude=tuple(args.exclude), skip_self=skip_self, files=tuple(args.file))
    # the stage owns its Progress now (the driver only speaks the protocol). None for --quiet/--dry-run;
    # else built from this stage's args + OUT_NOUN. tap has params=() and no LLM cost (cap omitted).
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, params=dict(blk.params), out_noun=OUT_NOUN, verbose=args.verbose)
    block.run(blk, source_id=args.source_id, max_usd=args.max_usd, limit=args.limit,
              dry_run=args.dry_run, priority=block.priority_strategy(args.priority), progress=progress)


if __name__ == "__main__":
    main()
