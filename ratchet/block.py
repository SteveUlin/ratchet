"""block — the substrate: one driver, per-item commit, streaming progress (ADR-0009).

The stages (tap/weave/chunk/glean/dream) grew at different abstraction levels with ad-hoc
`run()`/`materialize()` APIs. This is their single tested home for the cross-cutting concerns:
enumeration, the done-skip, per-item error isolation, the consecutive-failure breaker,
`--max-usd`/`--limit`/`--dry-run`, crash-safety, and streaming progress. A stage implements only its two stage-specific bits —
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

import statistics
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from . import blobstore, config

Item = Any   # opaque to the driver — only the block's key()/process() interpret it
ScoreOf = Callable[[Item], float]   # a stage's `priority` signal fn, handed to a PriorityStrategy
AgeOf = Callable[[Item], float]     # a stage's `age` signal fn — the SECOND per-item scalar (how long an
                                    # item has waited un-processed) Aging needs, symmetric to ScoreOf. The
                                    # driver hands it alongside score_of; non-Aging strategies ignore it
                                    # and the default `no_age` (0.0) makes aging inert (ADR-0011/0021).


@runtime_checkable
class Block(Protocol):
    """The structural interface a stage satisfies — a `Protocol`, not a base class, so stages stay
    plain modules/objects (no inheritance ceremony). The driver only ever touches these members."""

    name: str                                   # "tap" | "weave" | "chunk" | "glean" | "dream"
    params: tuple[tuple[str, str], ...]         # ordered (key, value) idempotency-param pairs; the
                                                # done-key suffix, stored verbatim in the marker body
    commits_per_item: bool                      # default True; dream sets False (commits in finalize)
    # parallel_safe — an OPTIONAL class attribute (the driver reads it via getattr, default False; NOT
    # a Protocol member, so existing structural Blocks stay Blocks): a block declares
    # `parallel_safe = True` iff its process() calls are mutually INDEPENDENT — nothing one item
    # writes changes what a concurrent item reads. The default is False because serial order can be
    # silently load-bearing: resolve's in-batch read-your-writes fold (each event reads the claims the
    # PREVIOUS event just minted) means two events in flight re-open the duplicate-seed race the fold
    # exists to close. glean opts in (each chunk is independent); everything else clamps to serial.

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
        """The item's value SCORE — the stage-owned half of priority (the POLICY half is the driver's
        `PriorityStrategy`, ADR-0011). This is just the signal: how much does processing THIS item next
        buy us. The default `Greedy` strategy stably sorts items DESCENDING by it before the --limit cap,
        so the highest-value work runs first and a budget/limit ceiling takes the top slice. Default
        (`no_priority`) returns 0.0, so Greedy's stable sort preserves enumeration order — a stage that
        does not care (tap/weave/chunk/glean) stays byte-for-byte identical. dream scores by event
        salience, glean by pre-LLM structural cues."""
        ...

    def age(self, item: Item) -> float:
        """The item's AGE — how long it has waited un-processed, the SECOND per-item signal the `Aging`
        POLICY needs (ADR-0021). Symmetric to `priority`: the STAGE owns the signal (naturally `now() -
        item's fetched_at`, in DAYS — the recency stamp lives in blob meta the strategy can't reach), the
        DRIVER's `PriorityStrategy` owns the policy. Default (`no_age`) returns 0.0 — every item is treated
        FRESH, so `Aging`'s `score + λ·age` collapses to `score` and aging is inert; under the default
        `Greedy` (which ignores age entirely) the stage is byte-for-byte identical. The budget-gated
        backlog stages (glean/dream) override to expose real age so an old item eventually surfaces; the
        cheap deterministic stages (tap/weave/chunk) inherit the 0.0 default — aging is moot for them."""
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
    """The default `priority` SIGNAL — every item ties at 0.0. Under the default `Greedy` policy Python's
    sort is stable, so a uniform score leaves enumeration order untouched: a stage that never opts in
    (tap/weave/chunk/glean) processes in exactly the order it always did. dream overrides with event
    salience, glean with structural cues, to feed the priority queue."""
    return 0.0


def no_age(_self, item: Item) -> float:
    """The default `age` SIGNAL — every item ages at 0.0 (treated FRESH). It makes `Aging` INERT: `score +
    λ·0 == score`, so `--priority aging` on a stage that does not expose age behaves exactly like `Greedy`
    (and `Greedy`/`Arrival` ignore age outright). A stage where backlog-starvation can't happen — the cheap
    deterministic ones (tap/weave/chunk) and the gardener — inherits this rather than wiring a recency read;
    glean/dream override with `now() - fetched_at` so an aged, modestly-scored item climbs the queue."""
    return 0.0


# --- the priority POLICY: a pluggable ordering strategy over the per-stage SIGNAL ------------------

# Priority couples two SEPARABLE concerns. The SIGNAL — `block.priority(item)->float`, the item's value
# score — is OWNED BY THE STAGE (dream's salience, glean's structural cues); it stays above. The POLICY
# — how that signal becomes a processing order — is the DRIVER's, the same for every stage, so it is the
# modular seam: a `PriorityStrategy` the driver applies to the eager enumeration BEFORE --limit (the cap
# takes the head) and BEFORE the backlog count. A stage never sees the policy; selecting one (by code or
# `--priority`) re-orders every stage at once without touching a single stage. The default reproduces the
# pre-strategy driver byte-for-byte, so this is a refactor + extension, never a behavior change.

@runtime_checkable
class PriorityStrategy(Protocol):
    """The ordering POLICY — `order(items, score_of, age_of) -> list[item]`, where `score_of` is the
    stage's `block.priority` and `age_of` its `block.age`. The driver hands it the eager enumeration and
    BOTH per-item signal functions; the strategy returns the processing order. A `Protocol` (not a base
    class) so a strategy is any object exposing `order` — stdlib-only, no inheritance, no registry coupling.

    `age_of` is the seam EXTENSION ADR-0011 foresaw (the single foreseeable pressure point), realized in
    ADR-0021: `Aging` needs a SECOND per-item scalar — age — that `order(items, score_of)` alone could not
    carry. It is KEYWORD-DEFAULTED to None so the extension is NON-BREAKING: `Greedy`/`Arrival` accept-and-
    ignore it, a caller passing only `score_of` (dream's dry-run preview) still works, and only `Aging`
    reads it. The driver always passes `block.age` (every block has it via the `no_age` mixin)."""

    def order(self, items: list[Item], score_of: ScoreOf, age_of: AgeOf | None = None) -> list[Item]:
        ...


class Greedy:
    """The DEFAULT policy: a STABLE descending sort by score — highest-value work first, a budget/limit
    ceiling takes the top slice (ADR-0009/0010 §8). Byte-for-byte the pre-strategy driver: `sorted(...,
    reverse=True)` is the same stable Timsort the old in-place `items.sort(key=…, reverse=True)` ran, so
    the default `no_priority` (0.0 everywhere) still preserves enumeration order and every stage is
    identical. Greedy is value-of-information optimal under a myopic, stationary signal — the right
    default; the anti-starvation/anti-ossification policies below trade myopic optimality for fairness.

    `age_of` is accepted (the extended seam) but DELIBERATELY IGNORED — Greedy sorts by score ALONE, so
    its body stays byte-identical to the pre-Aging driver and the default path provably never reorders."""

    def order(self, items: list[Item], score_of: ScoreOf, age_of: AgeOf | None = None) -> list[Item]:
        return sorted(items, key=score_of, reverse=True)


class Arrival:
    """Identity: enumeration order, score IGNORED. The trivial alternative that PROVES the seam — same
    items, same driver, a different processing order with zero stage change. (It is also where a
    no-signal stage already lands via the stable Greedy default on a uniform 0.0 score, so `--priority
    arrival` only differs once a stage emits a non-uniform signal.)"""

    def order(self, items: list[Item], score_of: ScoreOf, age_of: AgeOf | None = None) -> list[Item]:
        return list(items)


# UNTUNED — the fairness/hot-work dial (ADR-0021). Age is in DAYS (`now() - fetched_at`), score is the
# stage's O(1)-O(3) signal (glean's structural cues, dream's salience), so λ is the dollars of effective
# priority an item gains PER DAY of waiting: at 0.05/day a backlogged item climbs +1.0 every 20 days, so
# the lowest-scored item overtakes a one-point score gap in ~20 days and a full salience gap in ~2 months
# — the worst-case latency bound that turns a months-long backlog from "never drained" into "eventually
# drained". Too HIGH and aging swamps the score (FIFO, hot work starves); too LOW and the long tail still
# starves (Greedy). The right value trades fairness vs. hot-work throughput and wants a GOLD SET to fit —
# this is a defensible default, not a fitted one. A single module-level constant so retuning is one edit.
AGING_LAMBDA = 0.05


class Aging:
    """The ANTI-STARVATION policy: a STABLE descending sort by `effective(item) = score + λ·age` (ADR-0021).
    Greedy PROVABLY STARVES the long tail — an item that never tops the score is never in the top slice a
    persistent `--limit`/`--max-usd` budget takes, so under sustained high-priority load its wait → ∞
    (classic priority-queue starvation; queueing theory). Aging is the standard fix: a low-score item's
    effective priority CLIMBS with the time it has waited (`λ·age`) until it overtakes fresher high-score
    arrivals, so every item is processed in BOUNDED time — worst-case latency ≈ (score gap)/λ. It is the
    Multilevel-Feedback-Queue / Unix-scheduler aging trick (and PER's ε-floor in additive, deterministic
    form): trade a little of Greedy's myopic value-of-information optimality for fairness to the backlog.

    The seam EXTENSION (ADR-0011's one foreseen pressure point) lives HERE: Aging is the only shipped
    strategy that reads `age_of`. With the default `no_age` (0.0 everywhere) `effective == score`, so Aging
    collapses to Greedy and is INERT — it only bites on the stages that expose real age (glean/dream).
    `age_of=None` (a caller that passes only `score_of`) is treated as all-fresh, same collapse. Stable,
    like Greedy, so equal-`effective` items keep enumeration order (all-equal ages == Greedy ordering: age
    adds a uniform constant, no reorder)."""

    def order(self, items: list[Item], score_of: ScoreOf, age_of: AgeOf | None = None) -> list[Item]:
        age = age_of if age_of is not None else (lambda _it: 0.0)   # None → all-fresh: Aging collapses to Greedy
        return sorted(items, key=lambda it: score_of(it) + AGING_LAMBDA * age(it), reverse=True)


# The name→strategy registry the CLIs' `--priority` resolves against. Values are zero-arg FACTORIES (the
# classes themselves), so each run gets a FRESH instance — load-bearing for a future STATEFUL strategy
# (Stochastic/PER carries a seeded RNG; a shared singleton would leak draw state across runs).
PRIORITY_STRATEGIES: dict[str, Callable[[], PriorityStrategy]] = {
    "greedy": Greedy,
    "arrival": Arrival,
    "aging": Aging,
}


def priority_strategy(name: str) -> PriorityStrategy:
    """Resolve a `--priority` name to a FRESH strategy instance via the registry. An unknown name raises
    (argparse `choices=` normally catches it first; this guards a programmatic caller)."""
    try:
        return PRIORITY_STRATEGIES[name]()
    except KeyError:
        raise ValueError(f"unknown priority strategy {name!r}; "
                         f"choose from {sorted(PRIORITY_STRATEGIES)}") from None


# EXTENSION SEAM (ADR-0011): a new policy is a new `PriorityStrategy` class registered above. The spine is
# value-of-information — Greedy is myopically optimal; these trade a little of that for anti-starvation /
# anti-ossification. `Aging` (above) is BUILT — it realized the one foreseeable PRESSURE POINT ADR-0011
# recorded: `score + λ·age` needs a SECOND per-item scalar (age) the original `order(items, score_of)`
# seam did not carry (items are opaque; `fetched_at` lives in blob meta the strategy can't reach), so the
# seam was EXTENDED with a symmetric `age_of` fn (+ a `Block.age`/`no_age` mixin) — the single bend the ADR
# foresaw, now straightened (ADR-0021). `Greedy` stays byte-identical (it ignores `age_of`). Still UNBUILT:
#   Stochastic — PER (Prioritized Experience Replay): sample WITHOUT replacement with `P ∝ score^α` plus
#                an ε-floor so every item keeps nonzero mass (anti-starvation) and the high scorers don't
#                ossify the head (anti-ossification). Needs a SEEDED `random.Random` (a constructor arg)
#                for deterministic tests — exactly why the registry hands out fresh instances per run.
#   RankBased  — PER's outlier-robust variant: `P ∝ 1/rank(score)`, insensitive to score scale, so one
#                runaway salience cannot monopolize the head. Same seeded-RNG sampling shape as Stochastic.
# Stochastic + RankBased drop in with ZERO further seam change (RNG via the constructor, value via
# `score_of`); `age_of` is already threaded for any future age-aware policy.


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


# --- the scores report: the pending queue's value curve, read-only (--scores, --dry-run's sibling) --

# The operator question this answers: "what is my next --limit/--max-usd tick going to BUY, and what
# does the backlog's value curve look like behind it?" A capped tick takes the TOP slice of the
# priority order, so the score DISTRIBUTION — not any single item — is what tells you whether the cap
# is skimming cream off a long flat tail or splitting a cliff. Pure string, no writes, no LLM.

SCORES_BAR_WIDTH = 40   # widest histogram bar, in '#'s: one '#' per item while the peak bin fits,
                        # proportionally scaled past that (a nonzero bin never rounds to an empty bar
                        # — presence must stay visible). One edit here retunes every stage's report.
SCORES_TOPN = 5         # items shown at each end: the next tick's buy vs the longest wait


def _score_histogram(scores: list[float], buckets: int) -> list[str]:
    """Equal-width ASCII histogram rows over `scores` — `[lo, hi)  count  bar` per bin, the LAST bin
    closed (`]`) so the max score lands inside it instead of falling off the edge. Two degradations,
    both graceful: all-equal scores collapse to ONE closed row (a zero bin width has nothing to
    spread — and would divide by zero), and the caller guards empty. Bars scale to the WIDEST bin
    (see SCORES_BAR_WIDTH)."""
    lo, hi = min(scores), max(scores)
    if lo == hi:
        n = len(scores)
        return [f"  [{lo:.3f}, {hi:.3f}]  {n:>4}  " + "#" * min(n, SCORES_BAR_WIDTH)]
    buckets = max(1, buckets)
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for s in scores:
        counts[min(int((s - lo) / width), buckets - 1)] += 1   # float edge-noise clamps into the last bin
    peak = max(counts)
    rows = []
    for i, c in enumerate(counts):
        close = "]" if i == buckets - 1 else ")"
        bar = "#" * (c if peak <= SCORES_BAR_WIDTH else max(1, round(c * SCORES_BAR_WIDTH / peak))) \
            if c else ""
        rows.append(f"  [{lo + i * width:.3f}, {lo + (i + 1) * width:.3f}{close}  {c:>4}  {bar}".rstrip())
    return rows


def scores_report(blk: Block, *, root: Path, source_id: str | None = None,
                  priority: str = "greedy", buckets: int = 12) -> str:
    """The pending queue's priority-score distribution, rendered read-only — enumerate items exactly
    as `run()` would (same `items()`, same `done_index` split, same `--priority` registry), then show
    the operator the value curve a capped tick draws from: summary stats, an equal-width histogram,
    and the SCORES_TOPN items at each end of the processing order. Under the Aging policy a SECOND
    histogram of EFFECTIVE scores (`score + λ·age`, the ordering the tick actually uses) sits beside
    the raw one, so what aging changes is visible rather than asserted. Composable exactly like
    --dry-run: no writes, no LLM, no markers."""
    root = config.ensure_layout(root)
    strat = priority_strategy(priority)
    aging = isinstance(strat, Aging)
    pvals = tuple(v for _, v in blk.params)
    done = done_index(blk.name, root)
    items = list(blk.items(root, source_id=source_id))
    pending = [it for it in items if (blk.key(it), *pvals) not in done]
    lines = [f"{blk.name} scores — {len(pending)} pending · {len(items) - len(pending)} done · "
             f"policy {priority}"]
    if not pending:
        lines.append("  queue empty — nothing pending under the current filters")
        return "\n".join(lines)

    scores = [blk.priority(it) for it in pending]
    lines.append(f"score: min {min(scores):.3f} · median {statistics.median(scores):.3f} · "
                 f"mean {statistics.mean(scores):.3f} · max {max(scores):.3f}")
    lines.append("")
    lines.append(f"score histogram ({buckets} equal-width bins over {len(pending)} pending):")
    lines.extend(_score_histogram(scores, buckets))
    if aging:                                      # show what aging CHANGES, next to what it changed
        effective = [blk.priority(it) + AGING_LAMBDA * blk.age(it) for it in pending]
        lines.append("")
        lines.append(f"effective histogram (score + λ·age, λ={AGING_LAMBDA}/day — the order the "
                     f"tick uses):")
        lines.extend(_score_histogram(effective, buckets))

    # the two ends of the ACTUAL processing order (the strategy's, not a re-derivation): what the
    # next tick buys first vs what waits longest. Greedy/Arrival ignore age_of; only Aging reads it.
    ordered = strat.order(pending, blk.priority, blk.age)

    def _item_line(it) -> str:
        s = blk.priority(it)
        if not aging:
            return f"  {blk.key(it)}  score {s:.3f}"
        a = blk.age(it)
        return f"  {blk.key(it)}  eff {s + AGING_LAMBDA * a:.3f} · score {s:.3f} · age {a:.1f}d"

    lines.append("")
    lines.append(f"top {min(SCORES_TOPN, len(ordered))} — the next tick buys these first:")
    lines.extend(_item_line(it) for it in ordered[:SCORES_TOPN])
    rest = ordered[SCORES_TOPN:]
    if rest:
        lines.append(f"bottom {min(SCORES_TOPN, len(rest))} — waits longest:")
        lines.extend(_item_line(it) for it in rest[-SCORES_TOPN:])
    return "\n".join(lines)


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
    breaker_tripped: bool = False  # K CONSECUTIVE failures aborted the tick (see BREAKER_ERRORS) —
                               # a systemic wall, not flaky items; the aborted remainder is pending
    would_process: int = 0     # --dry-run only
    pending: int = 0           # un-done items STILL in the store after this run (the amortized backlog):
                               # full enumeration minus markers minus what this run processed. >0 means
                               # a capped/budgeted run left work for the next tick (errored items count).


class ProxyReport:
    """The shared shape of a stage's run-report — a thin WRAPPER, not a copy, over the uniform `Report`
    the driver populated PLUS the stage's Block instance. It forwards every uniform Report field by reading
    THROUGH the wrapped Report via @property (never copied → never stale, the anti-desync discipline);
    each stage SUBCLASSES this and adds its genuinely-extra tallies as @property reads off the block
    (`self._blk`), which the Report has no place for. One base for glean/dream/garden's report wrappers —
    so the uniform forwarding lives in ONE place and only the stage-specific surface differs (ADR-0009)."""
    def __init__(self, report: Report, blk) -> None:
        self._report = report
        self._blk = blk

    @property
    def run_id(self) -> str:
        return self._report.run_id
    @property
    def examined(self) -> int:
        return self._report.examined
    @property
    def processed(self) -> int:
        return self._report.processed
    @property
    def skipped(self) -> int:
        return self._report.skipped
    @property
    def errored(self) -> int:
        return self._report.errored
    @property
    def outputs(self) -> int:
        return self._report.outputs
    @property
    def cost_usd(self) -> float:
        return self._report.cost_usd
    @property
    def stopped_on_budget(self) -> bool:
        return self._report.stopped_on_budget
    @property
    def breaker_tripped(self) -> bool:
        return self._report.breaker_tripped


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
    uncluttered). `--quiet` → no Progress; `--verbose` → the per-item lines also print above the bar.

    THE BAR TRACKS WHAT ENDS THE TICK (see _draw_bar): unleashed, the item walk; under `cap`
    (--max-usd), spend/cap labeled `$so-far/$cap`; under `limit` (--limit), processed/limit labeled
    `n/limit lim`; both → whichever leash is closer to tripping. Skips never drive a leashed bar —
    they keep their own `· N skip` counter."""

    def __init__(self, stage: str, *, cap: float | None = None, limit: int | None = None,
                 params: dict | None = None, out_noun: str = "out", verbose: bool = False,
                 stream=None):
        # `total` is NOT here: the driver owns enumeration, so it knows the count only at start() time
        # (after items() + --limit). The stage builds this Progress from its args BEFORE the driver
        # enumerates, so total cannot be a constructor arg — it arrives in start().
        self.stage, self.cap, self.limit, self.out_noun = stage, cap, limit, out_noun
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

    def start(self, *, total: int, todo: int, already: int, backlog: int = 0) -> None:
        """Open the run: the driver passes this run's enumerated `total` (post --limit) plus the done-skip
        split (`todo`/`already`), and the FULL un-done `backlog` (pre --limit). When the backlog exceeds
        this run's `todo`, a `--limit`/budget is deferring work, so the line surfaces "<N> pending" — the
        amortization signal: how much is still un-done at this stage beyond the slice this tick takes. A
        TTY spawns the animator thread here, once the count is known."""
        self.total = total
        ps = " · ".join(f"{k}={v}" for k, v in self.params.items())
        cap = f" · cap {self._c(_YEL, f'${self.cap:.2f}')}" if self.cap is not None else ""
        pend = f" · {self._c(_YEL, f'{backlog} pending')}" if backlog > todo else ""
        self._println(f"{self._c(_BOLD, self.stage)}: {self.total} items · {todo} to do · "
                      f"{already} done{pend}{cap}" + (f" · {self._c(_DIM, ps)}" if ps else ""))
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
        # The bar answers ONE question: "how close is this tick to ending?" — so its fraction must
        # track whatever ENDS the tick. Unleashed, that is the full drain: the item walk (seen/total,
        # skips included — a skip IS walked). But a leashed run ends at the leash, not the drain: a
        # --max-usd tick stops at the cap and a --limit tick at its cap of processed calls, so the
        # item-walk fraction can never approach 100% there (and its numerator quietly disagrees with
        # the done/total counter beside it). Under a leash the bar tracks the leash instead — spend/cap
        # for the budget, processed/limit for the limit — labeled so the reader knows which. Skips
        # never drive a leashed bar (they cost nothing against either leash); they keep their own
        # `· N skip` counter. PRECEDENCE when both leashes are set: the run ends at the FIRST leash to
        # trip, so the one CLOSER to tripping — the max of the two fractions — drives the bar; the
        # label names the driving leash. Fractions clamp at 1.0: the last call may overshoot the cap
        # (the gate checks spend BEFORE a call), and 100% must mean "this tick is done", not 104%.
        seen = self.done + self.skipped + self.errored
        budget_frac = min(self.cost / self.cap, 1.0) if self.cap else None
        limit_frac = min(self.done / self.limit, 1.0) if self.limit else None
        budget_drives = budget_frac is not None and (limit_frac is None or budget_frac >= limit_frac)
        if budget_frac is None and limit_frac is None:
            frac = seen / self.total if self.total else 1.0        # unleashed: the item walk, as ever
            label = ""
        elif budget_drives:
            frac = budget_frac
            label = self._c(_YEL, f"${self.cost:.2f}/${self.cap:.2f}") + " "
        else:
            frac = limit_frac                                      # the limit leash drives
            label = f"{self.done}/{self.limit} lim "
        width = 24
        filled = int(round(width * frac))
        bar = _GRN + "▰" * filled + _DIM + "▱" * (width - filled) + _RESET
        skip = f" · {self.skipped} skip" if self.skipped else ""
        err = f" · {self._c(_RED, f'{self.errored} err')}" if self.errored else ""
        # the trailing spend stays UNLESS the budget label already shows it beside the bar.
        spend = "" if budget_drives else \
            " · " + self._c(_YEL, f"${self.cost:.2f}") + (f"/${self.cap:.2f}" if self.cap is not None else "")
        self.stream.write(f"\r\x1b[2K{self._c(_CYN, _SPIN[self._frame])} {self.stage} {label}{bar} "
                          f"{int(frac * 100)}% · {self.done}/{self.total} · "
                          f"{self.outputs} {self.out_noun}{skip}{err}{spend}")
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

# The parallel-worker CAP (the leash on `run(parallel=N)`). Every `claude -p` worker drains the SAME
# account-level token bucket the interactive session uses — the 5-hour-window quota is per account,
# not per process — so parallelism buys LATENCY (overlapping subprocess waits), never capacity.
# Past the bucket's refill rate extra workers are INERT, not dangerous: they cycle 429 backoff (the
# completer's ride-out sleeps) while throughput flatlines — over-parallelizing can't even burn the
# 5-hour window faster. So the cap is a waste guard, not a safety rail. 10 is a generous leash for
# small haiku calls (glean); where the knee actually sits on a given plan tier is unpublished — run
# --parallel high while away from the keyboard, drop to 1-2 while working interactively (workers
# starve the foreground session's share of the bucket). A principle-driven, adjustable default
# (one edit here), enforced as a clamp with a stderr note — never a silent hard rule.
PARALLEL_MAX = 10

# The consecutive-failure BREAKER (the tripwire on a doomed tick). K CONSECUTIVE item failures abort
# the tick: a LONE flaky item is already isolated per item (counted `errored`, no marker, retried next
# run), but an UNBROKEN failure run means a systemic wall — the usage window exhausted, auth dead, the
# network gone — and every further call just burns the completer's fast-fail retries + backoff against
# it (a glean tick once ground through 1,300+ consecutive doomed calls, most of a 53-minute run). Any
# success or skip resets the count, so a corpus with scattered bad items never trips. Tripping is
# CLEAN: aborted items are unmarked and simply pending next tick — nothing lost, the next run retries
# once the wall lifts. 10 is high enough that a burst of genuinely bad items (real failures arrive
# scattered, not unbroken) rides through, low enough that a dead wall costs ~10 doomed calls, not the
# rest of the run. A principle-driven, adjustable default (one edit here; `--breaker-errors K` per run
# on the stages that expose driver knobs; 0 disables outright — the escape hatch), announced with a
# stderr note when it trips — never a silent hard rule.
BREAKER_ERRORS = 10


def run(block: Block, *, source_id: str | None = None, max_usd: float | None = None,
        limit: int | None = None, dry_run: bool = False, priority: PriorityStrategy | None = None,
        progress: Progress | None = None, root: Path | None = None, parallel: int = 1,
        breaker_errors: int = BREAKER_ERRORS) -> Report:
    """Drive a block over its items, IDENTICALLY for every stage. The per-item contract:

      1. enumerate (eager, so the bar knows the total) → 1b. ORDER by the `priority` POLICY (default
      Greedy: stable descending by the stage's signal) → 2. --limit cap → 3. done-skip (a marker for
      (key, *params)) → 4. budget gate (--max-usd, clean exit) → 5. dry-run list-only → 6. process
      (commit blobs) → 7. the marker LAST (per-item commit) → 8. progress.tick → 9. optional finalize.

    PRIORITY is split like PROGRESS: the SIGNALS are the stage's — the value SCORE (`block.priority(item)`)
    and the wait-time AGE (`block.age(item)`, the ADR-0021 extension) — and the POLICY (`priority.order`,
    ADR-0011) is the driver's, default `Greedy`. The order is applied to the eager enumeration BEFORE the
    cap and the backlog count, so the cap takes the top slice and dream's `commits_per_item=False` path is
    unaffected (it re-orders the same working set, commits in finalize). Default Greedy ignores age, so a
    non-Aging run is byte-identical; only `--priority aging` reads `block.age` (`no_age` → 0.0 elsewhere).

    The five invariants the driver guarantees so no stage re-implements them:
      PER-ITEM COMMIT — output blobs (in process) then the marker (here, LAST), so a kill/crash keeps
        every completed item and re-does only the one in flight.
      ERROR ISOLATION — a raising process() counts `errored`, writes NO marker, and the run CONTINUES;
        the item retries next run (its key never entered the done-set).
      BREAKER — isolation's complement: `breaker_errors` (default BREAKER_ERRORS; 0 = off) aborts the
        tick after K CONSECUTIVE failures, because an unbroken run is a systemic wall (usage window /
        auth / network), not K flaky items — every further call would burn retries against it. Any
        success or skip resets the count; aborted items are unmarked, simply pending next tick.
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
    per-item marker; `finalize` does every blob+marker commit off the block's own `_pending`. All-or-nothing.

    PARALLEL (opt-in, ADR-0009's "multithreading-ready" cashed in): `parallel=N>1` overlaps up to N
    process() calls in a thread pool — the LLM call is a subprocess (the GIL releases), so threads give
    true I/O parallelism. Everything else is UNCHANGED: dispatch follows the same priority order, the
    done-skip and budget gate stay at dispatch, each item's marker still lands AFTER its process(), and
    a raising item is isolated exactly as serially (see `_run_pool`). Two clamps guard it: a block that
    has not declared `parallel_safe = True` runs serial (its serial order may be load-bearing — see the
    Protocol note), and N caps at PARALLEL_MAX (a shared token bucket makes more workers backoff, not
    throughput). BUDGET under parallelism is a LEASH, not a contract: the gate checks COLLECTED spend
    at dispatch, so in-flight calls can overshoot --max-usd by up to `parallel` calls' cost — the
    serial gate's own single-call overshoot plus up to (parallel-1) calls already in flight when the
    gate trips. parallel=1 (the default) takes the EXISTING serial loop, byte-identical."""
    root = config.ensure_layout(root)
    run_id = config.run_id()
    parallel = max(1, parallel)
    if parallel > PARALLEL_MAX:
        print(f"{block.name}: --parallel {parallel} clamped to {PARALLEL_MAX} — every worker drains "
              f"the same account token bucket, so more workers buy 429 backoff, not throughput "
              f"(PARALLEL_MAX, one edit in block.py)", file=sys.stderr)
        parallel = PARALLEL_MAX
    if parallel > 1 and not getattr(block, "parallel_safe", False):
        print(f"{block.name}: not parallel-safe (parallel_safe is False — its serial order may be "
              f"load-bearing, e.g. resolve's read-your-writes fold); running serially", file=sys.stderr)
        parallel = 1
    if dry_run:
        parallel = 1                               # a dry run makes no calls — nothing to overlap
    breaker_errors = max(0, breaker_errors)        # <=0 → off (a negative must not trip on the first error)
    priority = priority or Greedy()                # resolve the default HERE, not as a shared default-arg
                                                   # instance, so every run gets a FRESH strategy (a future
                                                   # seeded/stateful policy must not leak state across runs).
    params = block.params                          # snapshot once (a stage must not mutate mid-run)
    pvals = tuple(v for _, v in params)
    done = done_index(block.name, root)            # one scan
    items = list(block.items(root, source_id=source_id))   # eager: the bar + startup summary need the total
    items = priority.order(items, block.priority, block.age)  # apply the POLICY over the stage's TWO signals —
                                                   # SCORE (`priority`) + AGE (`age`); see the run() docstring +
                                                   # ADR-0011/0021. Default Greedy ignores age and is a stable
                                                   # no-op on the 0.0 score, so a non-opting stage is identical.
    backlog = sum(1 for it in items if (block.key(it), *pvals) not in done)  # FULL un-done count, pre-limit:
                                                   # the amortized backlog this run draws from (AMORTIZATION
                                                   # VISIBILITY — what is still pending at this stage, not
                                                   # just this tick's slice). Same O(total) the loop pays.
    if limit is not None:
        items = items[:limit]                      # --limit caps items EXAMINED (the first `limit`)
    report = Report(stage=block.name, run_id=run_id)

    if progress:                                   # the driver owns enumeration → it computes the counts
        todo = sum(1 for it in items if (block.key(it), *pvals) not in done)
        progress.start(total=len(items), todo=todo, already=len(items) - todo, backlog=backlog)

    if parallel > 1:                               # the pooled lane — same contract, N calls in flight
        _run_pool(block, items, done=done, pvals=pvals, params=params, report=report,
                  progress=progress, max_usd=max_usd, root=root, run_id=run_id, workers=parallel,
                  breaker_errors=breaker_errors)
    else:
        streak = 0                                 # consecutive failures — the breaker's signal
        for item in items:                         # the serial lane — the original loop, untouched
            report.examined += 1
            k = block.key(item)
            if (k, *pvals) in done:
                report.skipped += 1
                streak = 0                         # a skip breaks the run of failures (not a wall signal)
                if progress:
                    progress.tick(k, "skipped")
                continue
            if max_usd is not None and report.cost_usd >= max_usd:
                report.stopped_on_budget = True
                break                              # clean exit; committed-so-far persists
            if dry_run:
                report.would_process += 1          # list-only; NO process, NO LLM, NO marker
                if progress:
                    progress.tick(k, "dry_run")
                continue
            try:
                n_out, cost = block.process(item, root=root, run_id=run_id)  # blobs committed inside
            except Exception:
                report.errored += 1                # per-item isolation; run continues, item retried
                streak += 1
                if progress:
                    progress.tick(k, "errored")
                if breaker_errors and streak >= breaker_errors:
                    report.breaker_tripped = True  # K unbroken failures = the wall, not the items
                    break                          # stop the tick; the remainder is simply pending
                continue
            streak = 0                             # a success proves the seam is alive
            report.processed += 1
            report.outputs += n_out
            report.cost_usd += cost
            if block.commits_per_item:             # default True; dream commits in finalize instead
                write_processed(block.name, k, params, n_outputs=n_out, cost_usd=cost,
                                run_id=run_id, extra=block.marker_extra(item), root=root)  # marker LAST
                done.add((k, *pvals))              # so a duplicate key later this run also skips
            if progress:
                progress.tick(k, "done", outputs=n_out, cost=cost)

    report.pending = backlog - report.processed    # what's STILL un-done after this run: a capped/budgeted
                                                   # tick leaves >0 (errored items stay pending — no marker);
                                                   # a full drain (or --dry-run, processed=0) leaves backlog.
    if progress:
        progress.stop()
    if report.breaker_tripped:                     # after progress.stop(): the note must not race the bar.
        print(f"{block.name}: {breaker_errors} consecutive errors — tripping the breaker; "
              f"{report.pending} items left pending; likely the usage window / rate wall "
              f"(BREAKER_ERRORS; --breaker-errors 0 to disable)", file=sys.stderr)
    if not dry_run:
        block.finalize(root=root, run_id=run_id)   # no-op default; dream/tap act on their own state
    return report


# --- the pooled lane: same per-item contract, up to N process() calls in flight (opt-in) ------------

def _run_pool(blk: Block, items: list[Item], *, done: set[tuple], pvals: tuple,
              params: tuple[tuple[str, str], ...], report: Report, progress: Progress | None,
              max_usd: float | None, root: Path, run_id: str, workers: int,
              breaker_errors: int = BREAKER_ERRORS) -> None:
    """`run`'s parallel lane — the same per-item contract with up to `workers` process() calls in
    flight (the LLM call is a subprocess: the GIL releases, so threads overlap the real wait). The
    division of labor keeps every shared mutation SINGLE-THREADED, so no lock beyond Progress's own:
    WORKERS run only `process` + `marker_extra` (read in the worker, right after ITS process, so a
    stage's per-item audit state stays owned by the thread that made it — glean keeps `_last`
    thread-local for exactly this); the MAIN thread owns dispatch (priority order, done-skip, budget
    gate) and collection (Report tallies, the marker, progress.tick).

    Dispatch is a BOUNDED WINDOW: at most `workers` futures in flight; a full window lands at least
    one finished item before the next dispatch, so collected spend stays fresh. The budget gate checks
    COLLECTED spend, so the run can overshoot --max-usd by up to `workers` calls' cost — the serial
    gate's one, plus up to (workers-1) already in flight when it trips (the run() docstring's leash).
    On a budget stop the window DRAINS rather than cancels: an in-flight call is paid work whose blobs
    process() already committed, so it lands normally (marker + Report), never orphans. Per-item
    semantics are otherwise the serial lane's: blobs inside process(), the marker AFTER (per-item
    commit), a raising item isolated as `errored` with no marker (retried next run).

    The BREAKER follows the budget-stop shape: `breaker_errors` consecutive COLLECTED failures (a
    landed success resets the count) flag `breaker_tripped`, which stops DISPATCH at the next gate;
    the in-flight window drains normally, so a straggling success still lands with its marker (the
    drain may collect a few more failures — bounded by the window). run() prints the one stderr note
    after the drain, when the pending count is final — the serial lane's exact line."""
    def work(item: Item) -> tuple[int, float, dict]:
        n_out, cost = blk.process(item, root=root, run_id=run_id)     # blobs committed inside
        return n_out, cost, blk.marker_extra(item)   # audit fields read in THIS worker (see docstring)

    inflight: dict = {}                            # future → its item key; main-thread-only
    streak = 0                                     # consecutive COLLECTED failures — the breaker's signal

    def land(futures) -> None:
        """Collect finished futures — MAIN thread only, the sole mutator of Report/markers/progress."""
        nonlocal streak
        for f in futures:
            k = inflight.pop(f)
            try:
                n_out, cost, extra = f.result()    # re-raises whatever process() raised
            except Exception:
                report.errored += 1                # per-item isolation, exactly the serial contract
                streak += 1
                if breaker_errors and streak >= breaker_errors:
                    report.breaker_tripped = True  # stop DISPATCH (the loop's gate); in-flight drains
                if progress:
                    progress.tick(k, "errored")
                continue
            streak = 0                             # a landed success proves the seam is alive
            report.processed += 1
            report.outputs += n_out
            report.cost_usd += cost
            if blk.commits_per_item:
                write_processed(blk.name, k, params, n_outputs=n_out, cost_usd=cost,
                                run_id=run_id, extra=extra, root=root)   # marker LAST, per item
                done.add((k, *pvals))
            if progress:
                progress.tick(k, "done", outputs=n_out, cost=cost)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in items:
            report.examined += 1
            k = blk.key(item)
            if (k, *pvals) in done:
                report.skipped += 1
                if progress:
                    progress.tick(k, "skipped")
                continue
            while len(inflight) >= workers:        # window full → land finished work before dispatching
                finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                land(finished)
            if report.breaker_tripped:
                break                              # stop DISPATCH; the drain below lands in-flight work
            if max_usd is not None and report.cost_usd >= max_usd:
                report.stopped_on_budget = True
                break                              # stop DISPATCH; the drain below lands in-flight work
            inflight[pool.submit(work, item)] = k
        land(list(inflight))                       # drain: every in-flight item lands (paid work commits)
