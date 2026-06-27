# 0011 — the priority POLICY is a pluggable strategy (driver owns the order, the stage owns the signal)

- Status: accepted — implemented 2026-06-28 (all 10 suites green, incl. the golden-file `test_priority`)
- Date: 2026-06-28
- Supersedes: — (nothing). Refines ADR-0009's driver and ADR-0010 §8's priority knob.
- Superseded by: —

Code (`ratchet/block.py` + each stage's `main()`) is the source of truth; this records the *why*.

## Context

ADR-0010 §8 added one composable knob, `block.priority(item) -> float`, and the ADR-0009 driver turned
it into a priority queue with a single hard-coded line: `items.sort(key=block.priority, reverse=True)`
before the `--limit` cap. That coupled two SEPARABLE concerns into one place:

- the **signal** — *how valuable is processing this item next* — which is genuinely stage-specific
  (dream's event salience, glean's pre-LLM structural cues), and
- the **policy** — *how a column of scores becomes a processing order* — which is NOT stage-specific:
  greedy-descending is one choice among many (anti-starvation aging, stochastic PER sampling,
  rank-based sampling), and the same choice should apply to every stage at once.

sulin: make the policy selectable "either through code or different args to the brick." A hard-coded
greedy sort can't express *fairness* — a slow-burn item that never tops the score starves under a
persistent `--limit`/`--max-usd` ceiling, and a runaway-salience item ossifies the head run after run.
Those are value-of-information tradeoffs the driver should be able to swap without editing a stage.

## Decisions

### The seam: `PriorityStrategy.order(items, score_of)`, default `Greedy`

The **signal stays on the stage** (`block.priority`, unchanged). The **policy becomes a driver object** —
a stdlib `Protocol`:

```
class PriorityStrategy(Protocol):
    def order(self, items, score_of) -> list[item]   # score_of is block.priority
```

The driver replaces its sort with `items = priority.order(items, block.priority)` — same place in the
pipeline, so every ADR-0009 invariant holds: ordering runs on the eager enumeration **before** `--limit`
(the cap takes the head) and **before** the backlog count, and dream's `commits_per_item=False` path is
untouched (it re-orders the same working set, still commits in `finalize`).

Two concrete strategies ship:

- **`Greedy`** (default) — `sorted(items, key=score_of, reverse=True)`. The same stable Timsort the old
  in-place sort ran, so the default `no_priority` (0.0 everywhere) still preserves enumeration order and
  **every existing test and every stage is byte-for-byte identical**. This is the right default: greedy
  is value-of-information optimal under a myopic, stationary signal.
- **`Arrival`** — enumeration order, score ignored. The trivial alternative that *proves the seam*: same
  items, same driver, a different order with zero stage change.

### Selection two ways — code and CLI

- **By code:** `block.run(..., priority=Greedy())` — a keyword-only param defaulting to `Greedy()`.
- **By CLI:** `--priority {greedy,arrival}` on every stage (`tap`/`weave`/`chunk`/`glean`/`dream`),
  resolved through a name→factory registry `PRIORITY_STRATEGIES` via `priority_strategy(name)`, threaded
  from each `main()` into `block.run` (dream/glean via their thin `run` shims). Default `greedy` keeps the
  default path byte-identical. The registry holds **factories** (the classes), so each run gets a FRESH
  instance — load-bearing for a future *stateful* strategy whose RNG state must not leak across runs.

### The extension seam — and the one place it bends

A new policy is a new `PriorityStrategy` class registered in `PRIORITY_STRATEGIES` (the plug-in mechanics
live in `block.py`'s seam note). The spine is value-of-information: `Greedy` is myopically optimal; these
trade a little of that for anti-starvation / anti-ossification (built later):

- **`Stochastic` (PER)** — sample without replacement with `P ∝ score^α` plus an **ε-floor** so every item
  keeps nonzero mass (anti-starvation) and the top scorers don't ossify the head (anti-ossification).
- **`RankBased`** — PER's outlier-robust variant, `P ∝ 1/rank(score)`: insensitive to score scale, so one
  runaway salience can't monopolize the head.

Both drop in with **zero driver/stage change** — RNG via the constructor, value via `score_of`. **`Aging`**
(`score + λ·age`, so a slow-burn item eventually surfaces) is the exception worth recording honestly: it
needs a *second* per-item scalar — age — that the `order(items, score_of)` seam does not carry (items are
opaque; `fetched_at` lives in blob meta the strategy can't reach). So `Aging` **extends the seam** (a second
`age_of` fn, or the driver passing a feature map) rather than slotting into it — the single bend in an
otherwise stage-untouching decoupling.

## Consequences

- **Good:** the ordering policy is one swap-point for the entire pipeline; `Greedy`/`Arrival` ship today
  and the value-of-information family (`Aging`/`Stochastic`/`RankBased`) is a drop-in class each; the
  default is provably unchanged (the byte-identical guard + every prior suite stays green); the signal
  (`block.priority`) and the policy (`PriorityStrategy`) are now testable in isolation.
- **Costs / known limits:** `--priority` is a per-run flag, not persisted state — a strategy that wants
  to learn across runs (true PER) must read its history from the store, as `Aging` would. Stochastic
  strategies need a seed surfaced to the CLI for reproducibility (deferred with the strategies
  themselves). The seam orders the WHOLE enumeration eagerly (same O(total) as today); a streaming /
  top-K heap is a later concern, not a v1 one.

## References

ADR-0009 (the uniform Block + driver this refines — the sort line it replaces). ADR-0010 §8 (the
salience `priority` knob and the order-matters-twice argument: highest-value-first under a ceiling, and
order-dependent incremental routing). Prioritized Experience Replay (Schaul et al. — `P ∝ priority^α`,
the ε-floor, rank-based vs proportional). The golden-file acceptance test `tests/test_priority.py` +
`tests/golden/priority_order.json` (expected-vs-actual order per strategy).
