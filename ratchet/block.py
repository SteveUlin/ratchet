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

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from . import blobstore, config

Item = Any   # opaque to the driver — only the block's key()/process() interpret it


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

    def finalize(self, *, root: Path, run_id: str) -> None:
        """OPTIONAL cross-item pass over state the block accumulated on ITSELF during the run (dream's
        `_pending`, tap's dirty cursor). The driver hands it NO item list — a finalize block tracks its
        own per-item state on the instance. Default (`no_finalize`) is a no-op; dream/tap override."""
        ...

    def marker_extra(self, item: Item) -> dict:
        """Stage audit fields for the per-item marker's body (glean: n_rejected/n_calls/…). Default {}."""
        ...

    def priority(self, item: Item) -> float:
        """The item's processing PRIORITY — the one composable knob that turns enumeration into a
        priority queue (ADR-0010 §8). The driver stably sorts items DESCENDING by this before the
        --limit cap, so the highest-value work runs first and a budget/limit ceiling takes the top
        slice. Default (`no_priority`) returns 0.0, so a stable sort preserves enumeration order — a
        stage that does not care (tap/weave/chunk/glean) stays byte-for-byte identical. dream orders
        its working set by event salience."""
        ...


# --- defaults a stage mixes in for the optional knobs (so a stage declares only what it overrides) --

# These bind as METHODS (assigned `finalize = no_finalize` on the class), so each takes the leading
# `self`/`_self` the descriptor protocol passes — a module-level function set as a class attribute is
# an instance method. A stage that needs neither just inherits these and declares nothing.

def no_finalize(_self, *, root: Path, run_id: str) -> None:
    """The default `finalize` — a no-op. A per-item stage (tap is the exception, flushing its cursor)
    has no cross-item dependency, so it inherits this rather than writing an empty method each."""
    return None


def no_marker_extra(_self, item: Item) -> dict:
    """The default `marker_extra` — no audit fields. Stages with per-item forensics (glean) override."""
    return {}


def no_priority(_self, item: Item) -> float:
    """The default `priority` — every item ties at 0.0. Python's sort is stable, so a uniform priority
    leaves enumeration order untouched: a stage that never opts in (tap/weave/chunk/glean) processes
    in exactly the order it always did. dream overrides with event salience to make a priority queue."""
    return 0.0


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


# --- live progress: spinner + ▰▱ bar + spend on a TTY; idempotent per-item lines when piped --------

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_RESET, _BOLD, _DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
_RED, _GRN, _YEL, _CYN = "\x1b[31m", "\x1b[32m", "\x1b[33m", "\x1b[36m"


class Progress:
    """A block run's live progress, stdlib-only. On a TTY: ONE in-place line — an animated spinner, a
    `▰▱` bar with a percentage, a `done` count and a running spend, in color — redrawn by a daemon
    thread every 100 ms so it MOVES even during a slow LLM call (the "still alive" signal). Piped (not
    a TTY): one IDEMPOTENT, self-contained line per processed item — its own key · outputs · cost,
    never a running total — so a log survives reordering and a future PARALLEL run needs no format
    change. The aggregate counters live behind a lock, so worker threads may `tick()` concurrently:
    multithreading-ready (ADR-0009). `skip`/`err` show only when nonzero (a clean first run is
    uncluttered). `--quiet` → no Progress; `--verbose` → the per-item lines also print above the bar."""

    def __init__(self, stage: str, *, cap: float | None = None, params: dict | None = None,
                 out_noun: str = "out", verbose: bool = False, stream=None):
        # `total` is NOT here: the driver owns enumeration, so it knows the count only at start() time
        # (after items() + --limit). The stage builds this Progress from its args BEFORE the driver
        # enumerates, so total cannot be a constructor arg — it arrives in start().
        self.stage, self.cap, self.out_noun = stage, cap, out_noun
        self.params, self.verbose = params or {}, verbose
        self.stream = stream if stream is not None else sys.stderr
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.total = 0
        self.done = self.skipped = self.errored = self.outputs = 0
        self.cost = 0.0
        self._frame = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _c(self, code: str, s) -> str:
        return f"{code}{s}{_RESET}" if self.tty else str(s)    # color ONLY on a TTY

    def start(self, *, total: int, todo: int, already: int) -> None:
        """Open the run: the driver passes the enumerated `total` (it owns enumeration) plus the
        done-skip split (`todo`/`already`). A TTY spawns the animator thread here, once the count is known."""
        self.total = total
        ps = " · ".join(f"{k}={v}" for k, v in self.params.items())
        cap = f" · cap {self._c(_YEL, f'${self.cap:.2f}')}" if self.cap is not None else ""
        self._println(f"{self._c(_BOLD, self.stage)}: {self.total} items · {todo} to do · "
                      f"{already} done{cap}" + (f" · {self._c(_DIM, ps)}" if ps else ""))
        if self.tty and self.total:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def tick(self, key: str, outcome: str, *, outputs: int = 0, cost: float = 0.0) -> None:
        """Record ONE item's landing. `outcome` is the single discriminator the driver passes per call
        site — one of "done" | "skipped" | "errored" | "dry_run" — replacing the old bool soup: tick
        branches on it once. A "done" item adds its outputs+cost to the aggregate; a skip/dry-run is a
        bare counter (no per-item line); an errored item logs its line but no outputs/cost."""
        with self._lock:                              # lock-guarded → concurrent ticks are safe
            if outcome == "skipped":
                self.skipped += 1
            elif outcome == "errored":
                self.errored += 1
            elif outcome == "done":
                self.done += 1
                self.outputs += outputs
                self.cost += cost
            # "dry_run" touches no aggregate counter (it is a list-only pass)
        if outcome in ("skipped", "dry_run"):
            return                                    # a skip/dry-run is a counter, not a per-item line
        line = self._item_line(key, outputs, cost, outcome == "errored")
        if not self.tty:
            self._println(line)                       # piped: the idempotent log
        elif self.verbose:
            self._println(line)                       # TTY --verbose: above the bar; it redraws next frame

    def _item_line(self, key: str, outputs: int, cost: float, errored: bool) -> str:
        if errored:
            return f"  {self.stage} {key[:12]} · {self._c(_RED, 'errored')}"
        return f"  {self.stage} {key[:12]} · {outputs} {self.out_noun} · ${cost:.4f}"   # THIS item only

    def _animate(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                self._frame = (self._frame + 1) % len(_SPIN)
                self._draw_bar()

    def _draw_bar(self) -> None:
        seen = self.done + self.skipped + self.errored
        frac = seen / self.total if self.total else 1.0
        width = 24
        filled = int(round(width * frac))
        bar = _GRN + "▰" * filled + _DIM + "▱" * (width - filled) + _RESET
        skip = f" · {self.skipped} skip" if self.skipped else ""
        err = f" · {self._c(_RED, f'{self.errored} err')}" if self.errored else ""
        spend = self._c(_YEL, f"${self.cost:.2f}") + (f"/${self.cap:.2f}" if self.cap is not None else "")
        self.stream.write(f"\r\x1b[2K{self._c(_CYN, _SPIN[self._frame])} {self.stage} {bar} "
                          f"{int(frac * 100)}% · {self.done}/{self.total} · "
                          f"{self.outputs} {self.out_noun}{skip}{err} · {spend}")
        self.stream.flush()

    def _erase(self) -> None:
        if self.tty:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()

    def _println(self, s: str) -> None:
        with self._lock:                              # serialize against the animator's bar draw
            if self.tty:
                self._erase()
            self.stream.write(s + "\n")
            self.stream.flush()

    def stop(self) -> None:
        """Stop the animator and clear the live bar. The STAGE prints the final summary (its own richer
        one, on stdout) — Progress owns only the startup line, the live bar, and the per-item log."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        if self.tty:
            self._erase()


# --- the driver: one loop, identical for every stage ------------------------------------------------

def run(block: Block, *, source_id: str | None = None, max_usd: float | None = None,
        limit: int | None = None, dry_run: bool = False,
        progress: Progress | None = None, root: Path | None = None) -> Report:
    """Drive a block over its items, IDENTICALLY for every stage. The per-item contract:

      1. enumerate (eager, so the bar knows the total) → 2. --limit cap → 3. done-skip (a marker for
      (key, *params)) → 4. budget gate (--max-usd, clean exit) → 5. dry-run list-only → 6. process
      (commit blobs) → 7. the marker LAST (per-item commit) → 8. progress.tick → 9. optional finalize.

    The four invariants the driver guarantees so no stage re-implements them:
      PER-ITEM COMMIT — output blobs (in process) then the marker (here, LAST), so a kill/crash keeps
        every completed item and re-does only the one in flight.
      ERROR ISOLATION — a raising process() counts `errored`, writes NO marker, and the run CONTINUES;
        the item retries next run (its key never entered the done-set).
      IDEMPOTENCY — `done_index` skips items with a marker for (key, *params); a bumped param flips the
        key, so the item re-processes.
      BUDGET/LIMIT — `--limit` caps items EXAMINED (the first `limit`); `--max-usd` stops cleanly,
        committed-so-far persists.

    PROGRESS is fully DECOUPLED: the driver knows only the `Progress` PROTOCOL (start/tick/stop), never
    constructs one, and never reads stage knobs (out_noun, verbosity) off the block. Each stage's main()
    builds its own `Progress` (or None for --quiet/--dry-run) from its args and injects it here. The
    driver owns enumeration, so IT computes `total`/`todo`/`already` and hands them to `start`; per item
    it passes the ONE outcome to `tick`. dream opts out of per-item commit (`commits_per_item=False`):
    process() ingests nothing durable and returns (0, cost) for the budget gate; the driver writes NO
    per-item marker; `finalize` does every blob+marker commit off the block's own `_pending`. All-or-nothing."""
    root = config.ensure_layout(root)
    run_id = config.run_id()
    params = block.params                          # snapshot once (a stage must not mutate mid-run)
    pvals = tuple(v for _, v in params)
    done = done_index(block.name, root)            # one scan
    items = list(block.items(root, source_id=source_id))   # eager: the bar + startup summary need the total
    items.sort(key=block.priority, reverse=True)   # ADR-0010 §8: a PRIORITY QUEUE — highest-value work
                                                   # first. Stable, so the default 0.0 priority preserves
                                                   # enumeration order (other stages byte-identical); the
                                                   # sort precedes --limit so the cap takes the TOP slice.
    if limit is not None:
        items = items[:limit]                      # --limit caps items EXAMINED (the first `limit`)
    report = Report(stage=block.name, run_id=run_id)

    if progress:                                   # the driver owns enumeration → it computes the counts
        todo = sum(1 for it in items if (block.key(it), *pvals) not in done)
        progress.start(total=len(items), todo=todo, already=len(items) - todo)

    for item in items:
        report.examined += 1
        k = block.key(item)
        if (k, *pvals) in done:
            report.skipped += 1
            if progress:
                progress.tick(k, "skipped")
            continue
        if max_usd is not None and report.cost_usd >= max_usd:
            report.stopped_on_budget = True
            break                                  # clean exit; committed-so-far persists
        if dry_run:
            report.would_process += 1              # list-only; NO process, NO LLM, NO marker
            if progress:
                progress.tick(k, "dry_run")
            continue
        try:
            n_out, cost = block.process(item, root=root, run_id=run_id)   # blobs committed inside
        except Exception:
            report.errored += 1                    # per-item isolation; run continues, item retried
            if progress:
                progress.tick(k, "errored")
            continue
        report.processed += 1
        report.outputs += n_out
        report.cost_usd += cost
        if block.commits_per_item:                 # default True; dream commits in finalize instead
            write_processed(block.name, k, params, n_outputs=n_out, cost_usd=cost,
                            run_id=run_id, extra=block.marker_extra(item), root=root)  # marker LAST
            done.add((k, *pvals))                  # so a duplicate key later this run also skips
        if progress:
            progress.tick(k, "done", outputs=n_out, cost=cost)

    if progress:
        progress.stop()
    if not dry_run:
        block.finalize(root=root, run_id=run_id)   # no-op default; dream/tap act on their own state
    return report
