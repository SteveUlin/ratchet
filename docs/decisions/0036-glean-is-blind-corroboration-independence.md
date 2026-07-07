# 0036 — glean is blind: the concept layer starves the signal maturity depends on

- Status: accepted — offline suites green (all 30; `test_glean`/`test_dream`/`test_completer` updated)
- Date: 2026-07-06
- Supersedes in part: ADR-0019 (the glean-side concept digest + the per-event `relevance` verdict),
  ADR-0035 (the warm-base fork + the `--pilot-report` A/B). What SURVIVES from 0035: R0 (the
  `Completion` cache fields + the run/marker `cache r/w` instrumentation) and R1 (the slim tool-less
  `_TOOLLESS_FLAGS`) — independent cost wins with nothing to do with the digest.
- Extends: ADR-0028 (statement-first resolve — the SOLE novelty consumer now), ADR-0026 (the model
  points, the code copies — the trust anchor, untouched), ADR-0004 (glean's `claude -p` seam).

Code (`glean.py`: BLIND `SYSTEM_PROMPT`/`DOC_SYSTEM_PROMPT`, `_user_prompt` without the digest,
`build_event`/`event_content` without `relevance`, `PROMPT_VERSION = "glean/5"`; `events.py`:
`salience` == `intrinsic_salience`, `W_REL` gone) is the source of truth; this records the *why*.

## Context

ADR-0019 (4b) had glean inject the global concept digest — "WHAT WE ALREADY KNOW" — into every
extraction prompt and judge each event's `relevance` against it: `novel` / `known` / `contradicts`.
That verdict fed exactly one place, `events.salience`, which multiplied it in as `W_REL`
(`known` ×0.4). Salience is the order the resolve/dream queue drains under a `--limit` / `--max-usd`
cap.

sulin caught the bug: **this breaks corroboration, the mechanism claim-maturity is built on.** A
claim matures because the SAME lesson recurs across DISTINCT sessions (ADR-0028's support count is
distinct-session, not distinct-event). But a lesson recurring in a new session is, by construction,
already in the store — so glean, primed with the digest, marks that re-occurrence `known`. `known`
sinks its salience ×0.4; under any budget cap the sunk event never reaches resolve; the corroboration
never happens. The digest starves the exact signal maturity depends on. The queue term meant to save
budget by deprioritizing "settled" knowledge instead suppresses the second, third, fourth sighting
that would have promoted a thin claim to a trusted one.

There is a second, structural problem underneath. With the digest in the prompt, `glean(chunk)` is no
longer a pure function of the chunk — its output depends on the whole concept layer's state at run
time. Extraction is PRIMED by what prior sessions produced. So "independent corroboration" is a lie:
the later sighting was shaped by the earlier one it is supposed to independently confirm. An event's
identity and content should fall out of the chunk alone; letting global state leak into per-chunk
extraction couples two things that must stay separable.

ADR-0035's warm-base fork existed ONLY to amortize this ~8KB digest across a tick's chunks. Remove the
digest and its whole reason to exist evaporates — worse, a `--warm-base` that still seated the digest
would re-introduce the global view through the back door.

## Decision

**glean extracts BLIND — a pure function of the chunk. Novelty-vs-the-store lives in exactly one
place, resolve, which already does it and USES a re-occurrence to corroborate rather than suppress
it.**

1. **Strip the digest and `relevance` from glean.** `SYSTEM_PROMPT` and `DOC_SYSTEM_PROMPT` lose the
   "judge this against WHAT WE ALREADY KNOW" clause; `_user_prompt` no longer injects the digest;
   `build_event`/`event_content` no longer produce or project a `relevance` field. The model sees the
   numbered excerpt, the structural cues, and nothing about the concept layer. `RELEVANCE_KINDS`,
   `clean_relevance`, and the `GleanBlock` digest seam (`_relevant_digest`, `_digest_ctx`,
   `_facet_cache`, the disable latch) are gone.

2. **`salience` == `intrinsic_salience`.** The `W_REL` multiplier is deleted from `events.py`. The
   priority-queue score is again the event's own durable value (confidence × weighted marker mass) —
   blind to the store. `forget` still names `intrinsic_salience` so eviction stays pinned to an
   event's own value.

3. **`PROMPT_VERSION` → `glean/5`, `DOC_PROMPT_VERSION` → `glean-doc/2`.** The version identifies the
   prompt; a blind prompt is a different prompt. The bump RE-OPENS frozen chunks for OPTIONAL blind
   re-glean — forward-only, budgeted, the glean/2→3→4 precedent. Old glean/4 events stay valid until a
   re-glean; re-gleaning blind may RECOVER events the digest once suppressed as `known`.

4. **Remove the warm-base fork and the pilot entirely.** `--warm-base`, `_warm_base_user`,
   `_global_digest`, `FORK_PROMPT_VERSION`, and the `completer` session chain (`.warm`/`.fork`,
   `--session-id`/`--resume`/`--fork-session`) go; so does the `--pilot-report` A/B machinery
   (`pilot_report`, `_render_pilot`, `PILOT_*`). glean's non-doc path is now a single blind
   `self.complete(system, prompt)` call.

## Why this shape

- **One novelty consumer, and it is the right one.** resolve's statement-first matching (ADR-0028) is
  the deterministic, single home for "have we seen this before?" — and it is CONSTRUCTIVE where the
  glean verdict was destructive: a match INCREMENTS support and can mature a claim; a non-match mints a
  new claim. It costs $0 on the deterministic-reject mass and one bounded call otherwise. Novelty
  belongs where a re-occurrence is USED, never where it gets sunk before it is counted. resolve is
  UNCHANGED by this work — we are removing a redundant, harmful second judgment, not moving one.

- **Blind extraction restores encapsulation.** `glean(chunk)` a pure function of the chunk means two
  runs, two stores, two orderings all extract the same events from the same bytes. Corroboration is
  only meaningful between INDEPENDENT observations; independence requires that the later extraction not
  be primed by the earlier. Purity buys that for free.

- **The salience term was never load-bearing for recall — only for order — and it ordered wrongly.**
  `relevance` never gated (ADR-0019 was careful there); it only reordered. But reordering under a
  budget cap IS a silent drop: a sunk event that never clears the cap is, operationally, discarded.
  The "cheap early / precise late" cascade 0019 reached for is real, but glean was the wrong stage to
  put the cheap filter in — it fired before corroboration could ever be counted.

- **R0 and R1 survive on their own merits.** The cache-token instrumentation answers "does the CLI's
  cross-invocation cache fire?" — a measured fact worth keeping regardless of the digest. The
  tool-less flags trim dead schema payload from every pure text→JSON call. Neither touches prompt
  TEXT or the digest; both stay.

## Consequences

- glean is cheaper and simpler: no per-chunk digest render, no facet pass, no warm-base session
  bookkeeping, no pilot fold. The prompt is the excerpt and its cues.
- On a young or cold store the change is invisible (every verdict was `novel` anyway); on a mature
  store it stops the digest suppressing recurrences — the corroboration signal flows to resolve intact.
- Old event blobs may carry a stale `relevance` field; it is deliberately NOT projected by
  `event_content`, so nothing reads it and it never perturbs a fold. A blind glean/5 re-extraction
  forks a clean version (latest wins).
- ADR-0019's provenance-relevant digest (`concept_digest(relevant_to=…)`, `digest_context`,
  `chunk_facets`) stays in `concepts.py` — dream/synthesize/generate still consume it as the trusted
  prior for SYNTHESIS. Only glean's use of it is retired.
- **Deferred, explicitly not built (sulin's carve-out):** a FUTURE per-chunkset LOCAL working memory —
  an intra-session scratch the chunks of ONE session share while gleaning it. That is
  corroboration-SAFE (it never spans sessions, so it cannot pre-prime a cross-session recurrence) and
  can be citation-safe IF it carries model SUMMARIES, not raw transcript text (evidence must still be
  bytes the code copies by line number, ADR-0026). It is a different mechanism from the global digest
  this ADR removes, and it is not designed or built here.
