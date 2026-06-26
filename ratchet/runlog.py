"""runlog — the append-only producer-run substrate shared by the LLM stages (glean, weigh, …).

A stage processes items and skips ones already done. Each run writes two append-only shards under the
data dir: an OUTPUT stream (`events/<stage>-<run_id>.jsonl`) and a PROCESSED ledger
(`state/<stage>-processed-<run_id>.jsonl`). Output is written durable-first and the processed marker
last (the commit) — so a crash reprocesses and never leaves a false 'done' (ADR-0004). Shards are
`.partial` until a clean exit renames them; readers glob the final names and merge, so a crashed
run's output is invisible. Single writer per shard ⇒ plain append is safe. The processed ledger is
keyed by stage-defined fields, so a re-run skips done items and a bumped prompt/model (a new key)
re-does them over the same frozen inputs.

This is the crash-safety-critical core, factored out of the stages so it lives in ONE tested place.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


def sweep_partials(root: Path) -> None:
    """Reclaim shard temps leaked by a hard crash mid-write (readers already ignore them)."""
    for sub in ("events", "state"):
        d = root / sub
        if d.exists():
            for f in d.glob("*.jsonl.partial"):
                try:
                    f.unlink()
                except OSError:
                    pass


def read_stream(stage: str, root: Path | None = None) -> list[dict]:
    """Every committed record from a stage's output shards (`.partial` shards — a crashed run's
    output — are ignored). A faithful RAW reader: duplicates by id may appear after a crash-window or
    a re-extraction; consumers dedup by id (ADR-0004 §"The event store")."""
    root = root or config.data_root()
    d = root / "events"
    out: list[dict] = []
    if not d.exists():
        return out
    for shard in sorted(d.glob(f"{stage}-*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def processed_index(stage: str, key_fields: tuple[str, ...], root: Path | None = None) -> set[tuple]:
    """The done-set: the `key_fields` tuple from every processed-ledger record, merged across shards.
    A stage skips a key already here; bumping a key field (prompt/model/…) re-does the item."""
    root = root or config.data_root()
    d = root / "state"
    done: set[tuple] = set()
    if not d.exists():
        return done
    for shard in sorted(d.glob(f"{stage}-processed-*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                done.add(tuple(r[f] for f in key_fields))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _append(handle, record: dict) -> None:
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _commit_or_drop(part: Path, final: Path, n: int) -> None:
    if n:
        os.replace(part, final)        # atomic within the filesystem
    else:
        part.unlink(missing_ok=True)   # an empty shard is clutter, not a record


class ShardRun:
    """A run's two append-only shards (OUTPUT + PROCESSED ledger), written `.partial` then committed
    on a CLEAN exit (a non-empty shard renames into place; an empty one is dropped; an *exception*
    leaves both `.partial` for the next `sweep_partials` — the crash path discards uncommitted work).
    `emit` an output record durable-first; `mark` the processed marker last (the commit ordering that
    makes a crash reprocess rather than leave a false 'done'). Use as a context manager."""

    def __init__(self, stage: str, run_id: str, root: Path):
        self._out_final = root / "events" / f"{stage}-{run_id}.jsonl"
        self._pr_final = root / "state" / f"{stage}-processed-{run_id}.jsonl"
        self._out_part = self._out_final.with_name(self._out_final.name + ".partial")
        self._pr_part = self._pr_final.with_name(self._pr_final.name + ".partial")
        self._n_out = self._n_pr = 0

    def __enter__(self) -> "ShardRun":
        self._out_f = open(self._out_part, "w", encoding="utf-8")
        self._pr_f = open(self._pr_part, "w", encoding="utf-8")
        return self

    def emit(self, record: dict) -> None:
        _append(self._out_f, record)
        self._n_out += 1

    def mark(self, record: dict) -> None:
        _append(self._pr_f, record)
        self._n_pr += 1

    def __exit__(self, exc_type, *_) -> bool:
        self._out_f.close()
        self._pr_f.close()
        if exc_type is None:           # clean exit only — a propagating error discards the partials
            _commit_or_drop(self._out_part, self._out_final, self._n_out)
            _commit_or_drop(self._pr_part, self._pr_final, self._n_pr)
        return False                   # never suppress an exception
