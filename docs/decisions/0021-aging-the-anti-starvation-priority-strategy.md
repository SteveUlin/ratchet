# 0021 — Aging: the anti-starvation priority strategy (score + λ·age)

- Status: accepted — implemented 2026-06-28 (all 18 offline suites green, incl. the extended `test_priority`)
- Date: 2026-06-28
- Supersedes: —
- Superseded by: —
- Extends: ADR-0011 (the pluggable priority POLICY) — realizes the one extension it foresaw.

Code (`ratchet/block.py` — `Aging`/`AGING_LAMBDA`, the `age_of`/`Block.age`/`no_age` seam extension;
`ratchet/config.py` `age_days`; `glean.GleanBlock.age` + `dream.DreamBlock.age`) is the source of truth;
this records the *why*.

## Context

This is operability work for a months-long backlog. ADR-0011 made the ordering POLICY a pluggable
`PriorityStrategy` and shipped `Greedy` (stable descending by the stage's value SIGNAL) as the default —
value-of-information optimal under a myopic, stationary signal. But `Greedy` **provably starves the long
tail**. The pipeline runs under a persistent `--limit`/`--max-usd` ceiling, so each tick processes only the
top slice of the score-ordered enumeration. An item that never tops the score is never in that slice — so
it is never processed. This is textbook **priority-queue starvation**: in a non-preemptive priority queue
fed faster than it drains, the expected wait of a low-priority job grows without bound as high-priority
load → capacity (queueing theory — as ρ_high → 1, W_low → ∞). With glean/dream gated by an LLM budget and a
steady inflow of fresh, high-salience events, a modestly-valued old chunk/event waits *forever*. For a
backlog measured in months that is not a tail risk; it is the default outcome.

ADR-0011 recorded this honestly as the **one foreseeable pressure point**: `Aging` (`score + λ·age`) is the
standard fix, but it needs a *second* per-item scalar — **age** — that the original `order(items,
score_of)` seam does not carry (items are opaque to the driver; an item's recency `fetched_at` lives in
blob meta the strategy can't reach). So `Aging` could not just slot in like `Stochastic`/`RankBased`; it
had to **extend the seam**. This ADR straightens that single bend.

## Decisions

### The seam extension: a second per-item SIGNAL, symmetric to the score

The block already owns the value SIGNAL (`block.priority(item) -> float`); the driver owns the POLICY. We
add the **symmetric** age signal, same shape and same ownership split:

- **`Block.age(item) -> float`** — the item's AGE (how long it has waited un-processed), naturally `now() -
  item's fetched_at` in **DAYS**. Default mixin **`no_age` → 0.0**, exactly like `no_priority`: a stage
  declares `age` only if it opts in.
- **`PriorityStrategy.order(items, score_of, age_of=None)`** — `age_of` is the new third argument, the
  block's `age` bound method. It is **keyword-defaulted to None** so the extension is **non-breaking**: a
  caller passing only `score_of` (dream's `--dry-run` preview) still works, and a None `age_of` is treated
  as all-fresh.
- The driver passes both: `items = priority.order(items, block.priority, block.age)` — same place in the
  pipeline as ADR-0011's single line, so every ADR-0009 invariant holds (ordering runs on the eager
  enumeration before `--limit` and the backlog count; dream's `commits_per_item=False` path is untouched).
- `AgeOf = Callable[[Item], float]` aliases the fn type for clarity, paralleling `ScoreOf`.

**`Greedy` stays byte-identical.** It accepts `age_of` and *ignores* it — its body is still `sorted(items,
key=score_of, reverse=True)`. And `no_age` returns 0.0 everywhere, so even `Aging` collapses to `Greedy`
on a stage that does not expose age. The default path provably never reorders; every prior suite and stage
is unchanged (the byte-identical guard in `test_priority` + all 18 suites green).

### `Aging` — `effective(item) = score + λ·age`, stable-descending

```
class Aging:
    def order(self, items, score_of, age_of=None):
        age = age_of if age_of is not None else (lambda _it: 0.0)
        return sorted(items, key=lambda it: score_of(it) + AGING_LAMBDA * age(it), reverse=True)
```

A low-score item's effective priority **climbs with the time it has waited** until it overtakes fresher
high-score arrivals, so every item is processed in **bounded time** — worst-case latency ≈ (score gap)/λ.
This is the Multilevel-Feedback-Queue / Unix-scheduler **aging** trick (and PER's ε-floor in additive,
deterministic form): trade a little of Greedy's myopic value-of-information optimality for **fairness to
the backlog**. The sort is stable, like Greedy, so equal-`effective` items keep enumeration order — and
**all-equal ages reproduce Greedy exactly** (age adds a uniform constant, which cannot reorder).

Registered as `"aging"` in `PRIORITY_STRATEGIES`, so it is selectable by code (`priority=Aging()`) and on
**every** stage via `--priority aging` (the CLIs already read `choices=sorted(PRIORITY_STRATEGIES)`).

### `λ` (`AGING_LAMBDA`) is UNTUNED — the fairness/hot-work dial

`λ` is the dollars of effective priority an item gains **per day** of waiting. It is the explicit
fairness↔hot-work tradeoff: too **high** and age swamps the score (FIFO — hot, high-value work starves
behind stale junk); too **low** and the long tail still starves (back toward Greedy). The default
**`AGING_LAMBDA = 0.05`/day** is a *defensible* value, not a *fitted* one: with a score gap of ~2 (glean's
and dream's signals live in an O(1)–O(3) range), a backlogged item overtakes in ~40 days and any one-point
gap in ~20 — turning a months-long backlog from "never drained" into "drained within a couple of months".
The right value wants a **gold set** to fit against; it is flagged UNTUNED at its definition and is a single
module-level constant so retuning is one edit. (Every weight in dream is similarly untuned, by the same
pending-gold-set reasoning.)

### Age = `now() - fetched_at`, wired only where starvation can happen

`config.age_days(stamp)` turns a blob's ISO `fetched_at` (the blobstore's recency stamp, ADR-0007) into
fractional days, **degrading to 0.0 ("treat as fresh") on a missing/unparseable stamp** — a recency we
can't read must never crash ordering, and 0.0 just leaves the item un-boosted (the safe direction). It is
wired on the two **budget-gated** stages, where backlog-starvation actually bites:

- **glean** — a chunk's age = `now() - get_meta(cleaned_hash).fetched_at` (the chunk is a span into that
  cleaned blob — how long the material has waited un-gleaned). Cheap: one **cached** meta read (chunks
  share a cleaned_hash, so `_age_cache` reads each blob's stamp once), no content slice, no LLM — it keeps
  the amortized-queue's O(1)-per-item promise.
- **dream** — an event's age = `now() - get_meta(event_blob).fetched_at` (how long the learning has waited
  un-consolidated — the anti-starvation complement to `forget`'s straggler eviction). The event→`fetched_at`
  map is built **lazily** on the first `age()` call (`_event_born_map`) and reused, so a Greedy run pays
  nothing and ordering stays O(n log n).

The cheap, deterministic stages (tap/weave/chunk) inherit `no_age` — no LLM budget, no long-tail backlog,
so aging is moot. The gardener (garden/garden_ops) also inherits it, but for a different reason worth being
honest about: `garden_ops` IS budget-gated and leader-keyed (a low-tension cluster can be perpetually
skipped — the same starvation triad glean/dream have), yet its backlog is bounded-SMALL (dozens–hundreds of
concepts/clusters vs. a months-long backlog of thousands of chunks/events), so `Greedy` drains it before
starvation bites; aging there is deferred (a one-line `def age` opt-in, `garden_ops` the likeliest first).
Because `Greedy` (the default) never calls `age_of`, none of these reads ever runs unless `--priority aging`
is selected.

## Consequences

- **Good:** the long tail can no longer starve — `--priority aging` bounds every backlogged item's wait;
  the seam's one foreseen extension is realized symmetrically (a second SIGNAL the stage owns, the driver
  threads, the policy reads) with `Greedy` byte-identical and every stage that opts out untouched; the new
  ordering is testable in isolation (the `test_priority` crossover pins it off the live λ).
- **Costs / known limits:** `λ` is UNTUNED — its value is a real fairness/hot-work tradeoff awaiting a gold
  set; ship it as an operability knob, not a tuned default. Age is read from `fetched_at`, which is "when
  the blob was written", a slight proxy for "when the work became eligible" (close enough — they differ by
  one upstream stage's lag). Aging is per-run, not learned across runs (same limit ADR-0011 noted). The
  age read is wired only on glean/dream; if a future budget-gated stage appears it must opt in (one
  `def age`), and tap/weave/chunk/garden deliberately do not.

## References

ADR-0011 (the pluggable priority POLICY this extends — the `age_of`/second-signal pressure point it
recorded, now built). ADR-0009 (the uniform Block + driver; the single ordering line `age_of` joins).
ADR-0010 §8 (the salience `priority` signal Aging reorders on dream). ADR-0007 (`meta.fetched_at`, the
recency stamp age reads). Systems prior-art: priority-queue **starvation** and **aging** (Multilevel
Feedback Queue, the classic Unix scheduler's priority decay); queueing theory (low-priority wait → ∞ as
high-priority load → capacity). Prioritized Experience Replay (Schaul et al. — the ε-floor's anti-
starvation role, here in additive deterministic form). The acceptance test `tests/test_priority.py` §5–§6
(the crossover, the equal-age/no-age collapse to Greedy, determinism + stability, code/name/CLI selection).
