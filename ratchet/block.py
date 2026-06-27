"""block — the substrate: one driver, per-item commit, streaming progress (ADR-0009).

The stages (tap/weave/chunk/glean/dream) grew at different abstraction levels with ad-hoc
`run()`/`materialize()` APIs. This is their single tested home for the cross-cutting concerns:
enumeration, the done-skip, per-item error isolation, `--max-usd`/`--limit`/`--dry-run`,
crash-safety, and streaming progress. A stage implements only its two stage-specific bits —
`items()` (enumerate inputs) and `process()` (transform one → ingest output blobs) — plus a few
declarative knobs (`name`, `params`, `key()`); the shared `run()` driver does everything else,
IDENTICALLY for every stage.

The crash-safety invariant is 0007's commit-marker-last, applied PER ITEM: `process` ingests the
item's output blobs (each crash-safe via the blobstore's content-then-meta commit), then the driver
writes the item's `processed` marker LAST. A kill or crash keeps every completed item and re-does
only the one in flight — never a whole session's work.

Idempotency is 0007's `processed_index`, lifted into the driver and generalized to per-item: "done"
== a `processed` decision blob exists for `(stage, key(item), *params)`. `done_index` derives the
done-set in one scan; bumping a param (prompt/model/render_version) flips the key → re-do.

One block opts out of per-item commit: dream's coverage-conditioned supersession needs the whole
run's emitted takeaways before any `supersedes` is known, so it sets `commits_per_item=False` and
commits in `finalize`. The flag is ALL-OR-NOTHING — either every output+marker is per-item, or
every output+marker is in finalize, never split — or the crash-safety ordering breaks.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from . import blobstore, config

Item = Any   # opaque to the driver — only the block's key()/process() interpret it


@dataclass
class Done:
    """A driver record for an item it decided to commit — handed to `finalize` for the optional
    cross-item pass. `item` is the opaque enumeration value; `key` is its stable id; n_outputs/
    cost_usd are the in-flight accounting `process` returned. dream reads `item` to recover the
    cluster; per-item stages ignore the list entirely (their default `finalize` is a no-op)."""
    item: Item
    key: str
    n_outputs: int = 0
    cost_usd: float = 0.0


@runtime_checkable
class Block(Protocol):
    """The structural interface a stage satisfies — a `Protocol`, not a base class, so stages stay
    plain modules/objects (no inheritance ceremony). The driver only ever touches these members."""

    name: str                                   # "tap" | "weave" | "chunk" | "glean" | "dream"
    params: tuple[tuple[str, str], ...]         # ordered (key, value) idempotency-param pairs; the
                                                # done-key suffix, stored verbatim in the marker body
    commits_per_item: bool                      # default True; dream sets False (commits in finalize)

    def items(self, root: Path, *, source_id: str | None = None) -> Iterable[Item]:
        """Enumerate inputs. source_id=None → the whole store (--all); set → just that source's."""
        ...

    def key(self, item: Item) -> str:
        """The item's stable, deterministic id — the per-item processed target."""
        ...

    def process(self, item: Item, *, root: Path, run_id: str) -> tuple[int, float]:
        """Transform → INGEST output blobs (crash-safe content-then-meta, each). Return
        (n_outputs, cost_usd). MUST NOT write the processed marker — the driver does that LAST.
        Raising is caught + isolated by run()."""
        ...

    def finalize(self, processed: list[Done], *, root: Path, run_id: str) -> None:
        """OPTIONAL cross-item pass. Default (`no_finalize`) is a no-op; dream is the only override."""
        ...

    def marker_extra(self, item: Item) -> dict:
        """Stage audit fields for the per-item marker's body (glean: n_rejected/n_calls/…). Default {}."""
        ...


# --- defaults a stage mixes in for the optional knobs (so a stage declares only what it overrides) --

# These bind as METHODS (assigned `finalize = no_finalize` on the class), so each takes the leading
# `self`/`_self` the descriptor protocol passes — a module-level function set as a class attribute is
# an instance method. A stage that needs neither just inherits these and declares nothing.

def no_finalize(_self, processed: list[Done], *, root: Path, run_id: str) -> None:
    """The default `finalize` — a no-op. A per-item stage (tap is the exception, flushing its cursor)
    has no cross-item dependency, so it inherits this rather than writing an empty method each."""
    return None


def no_marker_extra(_self, item: Item) -> dict:
    """The default `marker_extra` — no audit fields. Stages with per-item forensics (glean) override."""
    return {}


# --- the done-set + the processed marker (0007's per-input decisions, generalized to per-item) ------

def done_index(name: str, root: Path) -> set[tuple]:
    """The done-set for a stage in ONE scan: every `processed` decision blob for this stage, folded to
    its `(target, *ordered-param-values)` key. Generalizes glean/dream's `processed_index` — the only
    change is the params are read GENERICALLY off the body (in marker-write order) instead of the
    hard-coded `(prompt_version, model)`. The key omits `name` because `decisions_for(..., stage=name)`
    already filters by stage (matching today's per-stage `processed_index`).

    A param's order in the key follows `body['params']` — the ordered list the marker stored — so it
    lines up with `run`'s `(key, *pvals)` lookup regardless of dict iteration order. Markers missing
    that list (none today; a guard against a hand-written body) are skipped rather than mis-keyed."""
    done: set[tuple] = set()
    for b in blobstore.decisions_for(None, root, verb="processed", stage=name):
        target = b.get("target")
        params = b.get("params")
        if not target or not isinstance(params, list):
            continue
        # params is a list of [key, value] pairs (JSON has no tuples); key on the values, in order.
        done.add((target, *(p[1] for p in params if isinstance(p, list) and len(p) == 2)))
    return done


def write_processed(name: str, key: str, params: tuple[tuple[str, str], ...], *, n_outputs: int,
                    cost_usd: float, run_id: str, extra: dict, root: Path) -> None:
    """Write the per-item `processed` decision blob — the 0007 commit marker, lifted out of glean/dream
    and parameterized over the stage. This is `glean._write_processed`/`dream._write_processed` made
    generic: target = the item key; the ordered params live BOTH spread at the top level (audit
    readability, matching today's bodies) AND as `params` (the authoritative ordered list `done_index`
    keys on, so the done-key survives any dict reordering). `extra` carries the stage's audit fields
    (glean: n_rejected/n_calls/cleaned_hash; dream: event_ids/dropped).

    The body is UNIQUE per logical fact (target+stage+params+run_id+at), so `blob_hash` never conflates
    two distinct decisions; source_id == its own content_hash, prev=None (decisions are never
    re-versioned), fetched_at=at (the recency the marker-fold sorts on)."""
    at = config.now()
    body = {
        "verb": "processed", "target": key, "stage": name,
        "run_id": run_id, "at": at,
        # both forms: spread for human/audit readability, the ordered list for the done-key.
        **{k: v for k, v in params},
        "params": [[k, v] for k, v in params],
        "producer": {"stage": name, "model": dict(params).get("model"),
                     "run_id": run_id, "at": at},
        "n_outputs": n_outputs, "cost_usd": round(cost_usd, 8),
        **extra,
    }
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s), prev=None,
                     origin_ref={"stage": name, "run_id": run_id}, fetched_at=at, root=root)


# --- the shared Report (one shape for every stage's CLI surface + driver contract) ------------------

@dataclass
class Report:
    """One Report for every stage — the uniform CLI/driver surface. Stages keep richer in-memory
    result objects ONLY where another consumer needs them (dream's `RunReport.takeaways`, glean's
    per-result events), exposed via the block INSTANCE; the driver itself speaks only this."""
    stage: str
    run_id: str
    examined: int = 0          # items pulled from items() (capped by --limit)
    processed: int = 0         # items whose process() ran and committed
    skipped: int = 0           # items with a processed marker for (key, *params)
    errored: int = 0           # items whose process() raised (isolated, retried next run)
    outputs: int = 0           # total output blobs ingested (events, takeaways, cleaned, …)
    cost_usd: float = 0.0
    stopped_on_budget: bool = False
    would_process: int = 0     # --dry-run only


# --- the streaming progress printer (reads ONLY Report counters + the per-item tuple) ---------------

def _default_progress(report: Report, item: Item, key: str, n_out: int, cost: float, *,
                      dry_run: bool = False, errored: bool = False) -> None:
    """One line per item as it LANDS (not batched) — the fix for "no visible progress" on a backfill.
    It reads ONLY the stage-agnostic `Report` counters + the per-item `(key, n_out, cost)`, so the
    SAME printer serves every stage verbatim. `--quiet` passes `progress=None`; `--json` passes the
    block's structured `json_progress`."""
    k12 = key[:12]
    if dry_run:
        print(f"would {report.stage} {k12}")
        return
    if errored:
        print(f"  ! {report.stage} {k12} errored")
        return
    print(f"{report.stage}  {report.examined} examined · {report.processed} done · "
          f"{report.skipped} skip · {report.outputs} out · ${report.cost_usd:.2f}   {k12}")


# --- the driver: one loop, identical for every stage ------------------------------------------------

def run(block: Block, *, source_id: str | None = None, max_usd: float | None = None,
        limit: int | None = None, dry_run: bool = False,
        progress: Callable | None = _default_progress, root: Path | None = None) -> Report:
    """Drive a block over its items, IDENTICALLY for every stage. The per-item contract:

      1. enumerate → 2. examine (cap by --limit) → 3. done-skip (a marker for (key, *params)) →
      4. budget gate (--max-usd, clean exit) → 5. dry-run list-only → 6. process (commit blobs) →
      7. write the marker LAST (per-item commit) → 8. progress as it lands → 9. optional finalize.

    The four invariants the driver guarantees so no stage re-implements them:
      PER-ITEM COMMIT — output blobs (in process) then the marker (here, LAST), so a kill/crash keeps
        every completed item and re-does only the one in flight.
      ERROR ISOLATION — a raising process() counts `errored`, writes NO marker, and the run CONTINUES;
        the item retries next run (its key never entered the done-set).
      IDEMPOTENCY — `done_index` skips items with a marker for (key, *params); a bumped param flips the
        key, so the item re-processes.
      BUDGET/LIMIT — `--limit` caps items EXAMINED (before the done-skip, so re-running a done corpus
        still examines `limit` and stops); `--max-usd` stops cleanly, committed-so-far persists.

    dream opts out of per-item commit (`commits_per_item=False`): process() ingests nothing durable
    and returns (0, cost) for the budget gate; the driver writes NO per-item marker; `finalize` does
    every blob+marker commit. The flag is all-or-nothing (see module docstring)."""
    root = config.ensure_layout(root)
    run_id = config.run_id()
    params = block.params                          # snapshot once (a stage must not mutate mid-run)
    pvals = tuple(v for _, v in params)
    done = done_index(block.name, root)            # one scan
    report = Report(stage=block.name, run_id=run_id)
    committed: list[Done] = []

    for item in block.items(root, source_id=source_id):
        if limit is not None and report.examined >= limit:
            break                                  # --limit caps items EXAMINED, before the done-skip
        report.examined += 1
        k = block.key(item)
        if (k, *pvals) in done:
            report.skipped += 1
            continue
        if max_usd is not None and report.cost_usd >= max_usd:
            report.stopped_on_budget = True
            break                                  # clean exit; committed-so-far persists
        if dry_run:
            report.would_process += 1              # list-only; NO process, NO LLM, NO marker
            if progress:
                progress(report, item, k, 0, 0.0, dry_run=True)
            continue
        try:
            n_out, cost = block.process(item, root=root, run_id=run_id)   # blobs committed inside
        except Exception:
            report.errored += 1                    # per-item isolation; run continues, item retried
            if progress:
                progress(report, item, k, 0, 0.0, errored=True)
            continue
        report.processed += 1
        report.outputs += n_out
        report.cost_usd += cost
        if block.commits_per_item:                 # default True; dream commits in finalize instead
            write_processed(block.name, k, params, n_outputs=n_out, cost_usd=cost,
                            run_id=run_id, extra=block.marker_extra(item), root=root)  # marker LAST
            done.add((k, *pvals))                  # so a duplicate key later this run also skips
        committed.append(Done(item=item, key=k, n_outputs=n_out, cost_usd=cost))
        if progress:
            progress(report, item, k, n_out, cost)

    if not dry_run:
        block.finalize(committed, root=root, run_id=run_id)   # no-op default; dream commits here
    return report
