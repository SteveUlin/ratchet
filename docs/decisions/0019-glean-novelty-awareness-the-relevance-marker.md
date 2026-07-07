# 0019 — glean's novelty-awareness: the relevance marker vs the concept digest

- Status: accepted — implemented 2026-06-28 (all 17 offline suites green, incl. the 3a/3b/3c + 4a goldens)
- Date: 2026-06-28
- Supersedes: —
- **Superseded in part by ADR-0036**: the glean-side digest + per-event `relevance` verdict are REMOVED
  — injecting "what we already know" into extraction and sinking `known` events in salience starved the
  cross-session corroboration claim-maturity depends on. Novelty-vs-the-store now lives solely in resolve
  (ADR-0028). The provenance-relevant digest itself (`concept_digest`/`digest_context`) survives for
  dream/synthesize/generate; only glean's consumption of it is retired.

Code (`ratchet/glean.py` — `RELEVANCE_KINDS`/`clean_relevance`, the prompt + `build_event`/`event_content`,
`GleanBlock._relevant_digest`; `ratchet/concepts.py` — `chunk_facets`, `digest_context`, `concept_digest`'s
`relevant_to`; `ratchet/dream.py` — `W_REL` in `salience`) is the source of truth; this records the *why*.
This is **4b**: glean reads the 4a digest and emits a per-event relevance verdict that orders dream's queue.

## Context

ADR-0005's deferred roadmap led with item #1: a **relevance-to-known-how** marker — judge each extracted
learning against what the developer ALREADY knows, so the pipeline stops re-mining settled knowledge and
spends its synthesis budget on what is genuinely new. It "*is* Bayesian surprise (belief-change)" and was
deferred for one reason: it "needs retrieval the other markers don't" — glean had nothing to judge against.

4a (ADR-0018) built that something: `concept_digest`, a bounded, structured, IN-PROMPT render of the concept
layer — "what we already know." 4a wired it into dream; it explicitly named glean's relevance check as the
SECOND consumer. 4b is that consumer. glean's cheap per-chunk Haiku call already extracts events + scores
markers (surprise/insight/research); now it ALSO judges each event's RELEVANCE against the digest and stores
the verdict, and dream's salience reads it to order the working set novel-first.

The 4a review left one sharp design note: glean must read a **provenance-relevant** digest, not dream's global
most-entrenched one. The digest is bounded (`budget`) and truncates least-corroborated-first; a chunk's
near-duplicate may match a THIN, single-session concept that global truncation would drop — and a dropped
concept reads as absent, so the event would be judged falsely `novel`. The fix has to keep the concepts most
likely to cover THIS chunk against the cut.

## Decisions

### 1. The relevance verdict — `novel` / `known` / `contradicts`, recall-first coercion

Each event gains a `relevance` field, judged against the injected digest:
- **novel** — nothing in what we already know covers this; it is new information.
- **known** — a known concept already states this; it is not new.
- **contradicts** — it OVERTURNS or corrects something a concept asserts (a belief change — the most
  important to surface).

It is a single categorical verdict, not a per-kind score like `markers`: relevance is one question
("is this new vs the store?") with three mutually-exclusive answers, where the markers are independent
multi-label axes. It **classifies, it does not gate** — like the markers (ADR-0005), it never drops an event;
it only feeds dream's salience ORDERING. Coercion is RECALL-FIRST and single-sourced in `clean_relevance`:
an unknown or missing verdict → `novel`, never `known`. The asymmetry is the whole point — `novel` keeps an
event in the queue, `known` sinks it, so doubt must resolve toward "process it." A pre-4b (glean/2) event blob
has no field; `event_content` reads it defensively → `novel`, so an old event never sinks on a missing field.
The PROMPT_VERSION bump (`glean/2` → `glean/3`) re-gleans existing chunks under the sharper prompt and
re-stamps the real verdict (latest version wins); fine on a dev store, and the trust anchor (quote-substring
verification) is untouched.

### 2. The PROVENANCE-RELEVANT digest — `relevant_to`, the false-novelty fix

`concept_digest` gains `relevant_to=None`. When it is a chunk's facet set, the budget ranking keys on facet
OVERLAP with that set FIRST (`facet_score`, reusing the 3a machinery — shared file > repo > tool, plus the
temporal bonus), then entrenchment, then id. So the concepts most likely to already cover THIS chunk survive
truncation, and a near-duplicate of a thin, single-session concept is judged `known`, not falsely `novel`.
When `relevant_to is None` (dream's global use), the ranking is pure ENTRENCHMENT — byte-identical to 4a, so
dream and the digest golden are unchanged. The ordering is deterministic either way (the tie-break chain ends
at id).

A chunk's facet set comes from `chunk_facets(cleaned_hash)`: a chunk IS a concept with one piece of evidence
(its own cleaned blob), so it folds straight through `concept_facets` — the same provenance re-parse, no second
copy, the same `{repos, files, tools, sessions, time_range}` shape `facet_score` consumes. The chunk knows its
`cleaned_hash`; glean computes the facets from it just before the LLM call.

**Cost seam (`digest_context`).** A per-chunk relevant_to ordering means a per-chunk render — but the expensive
part (the `concept_graph` raw re-parse, ADR-0013) is run-invariant. So `concept_digest` splits into
`digest_context` (the facet pass + per-concept statements/relations) and the cheap per-`relevant_to` render.
`GleanBlock` caches the context on the instance — built once on the first signal chunk, re-rendered per chunk
— so the O(concepts) re-parse is paid once per run, not once per chunk. The digest build is ADVISORY and
recall-first: any failure to build it degrades to the empty sentinel rather than costing an extraction (the
model then sees nothing known and defaults every event to `novel`). Only the trust anchor is load-bearing; the
relevance context is not.

### 3. Recall-first wiring into salience — `known` SINKS, never hard-drops

`dream.salience` multiplies the marker mass by a relevance term `W_REL` = {contradicts: 1.5, novel: 1.0,
known: 0.4}. So dream drains novel/contradicting events FIRST and already-known ones LAST. Three choices, all
recall-first:

- **`known` only reorders.** It sinks an event in the priority queue (deferred under `--limit`/`--max-usd`),
  it does NOT drop it. The invisible false-negative — silently discarding a learning we WRONGLY called known —
  is the costly error (ADR-0005's recall stance); a deferred event is still there next tick. `known` is never
  zero, so it never falls out on the relevance signal alone. forget's existing eviction is UNCHANGED — its
  conjunctive gate, aged AND low-salience, already handles low-value stragglers, and it stays reversible. The
  DECOUPLING that makes "4b adds no new drop path" literally true: `salience` = `_intrinsic_salience` (the
  pre-4b conf×marker-mass) × `W_REL`, but `forget` gates on `_intrinsic_salience`, NOT `salience`. So
  relevance scales the salience-ORDER only — it can never DRIVE an eviction. Without the split, a `known`
  (×0.4) verdict could flip an aged, modest-marker event from surviving to forget-eligible (a NEW drop path,
  violating recall-first); gating forget on the relevance-free score closes that hole. A pre-4b event reads
  novel×1.0 → `salience` == `_intrinsic_salience`, so the forget byte-stream and the dream goldens are
  unchanged.
- **`novel` is the NEUTRAL ×1.0 default.** "Boost" and "sink" are relative to the deprioritized `known` tier;
  pinning novel at 1.0 means a pre-4b event (coerced to novel) keeps its EXACT prior salience, so 4b perturbs
  only events glean actually marked `known`/`contradicts` — minimal disruption, and the dream goldens stay
  green untouched.
- **`contradicts` boosts highest.** A belief change is the most valuable thing to surface — the cheap, EARLY
  echo of dream's precise LATE weaken judgment (below).

Only `salience`'s ORDERING changes; dream's route/apply LOGIC is untouched. glean's structural pre-`priority`
(which chunks first) is also untouched — relevance is post-LLM, so it can only feed dream's salience, never
glean's chunk order.

### 4. The cheap-early / precise-late cascade

glean's relevance is the CHEAP, EARLY complement to dream's PRECISE, LATE belief-change judgment, not a
duplicate. glean spends one Haiku call per event against a bounded digest to get a coarse novel/known/
contradicts signal — enough to ORDER the queue so dream reaches the surprising events first under budget.
dream then makes the real call: its router's `weaken` verdict and the symmetric contradiction stat (ADR-0012)
decide, per catalog takeaway, whether a belief actually demotes — with the full catalog, the synthesizer, and
net-entrenchment behind it. Early ordering is recall-cheap and wrong-cheap (a mis-ordered event is merely
processed sooner or later); late demotion is precision-expensive and must be right. Putting the cheap filter
first and the precise judge last is the FrugalGPT cascade shape ADR-0005 already chose for combined-vs-cascade
— here across STAGES, not within one call.

## Consequences

- **Good:** glean finally closes ADR-0005's roadmap-#1 — the Bayesian-surprise-vs-the-store marker the design
  wanted since the start. dream spends its synthesis budget novel-first and reaches belief changes early. The
  change is additive at the seams: events gain one field (defensive-read for old blobs), the digest gains one
  kwarg (entrenchment ordering unchanged when absent), salience gains one multiplier (×1.0 for the common
  case). The 4a digest test, the dream/concepts/weaken goldens, and the 3a/3b/3c views are all unchanged,
  proven by the suites staying green. The acceptance tests pin the contract: glean stores the coerced verdict
  (unknown → novel), `concept_digest(relevant_to=…)` floats a thin provenance-relevant concept over an
  entrenched irrelevant one, and `salience` ranks contradicts > novel > known for equal events.
- **Costs / open questions / second looks:**
  - The relevance→salience weights `W_REL` are UNTUNED magic numbers, like every weight here — pending a gold
    set. The ratio that matters is contradicts > novel > known with known bounded above zero; the exact values
    (1.5 / 1.0 / 0.4) are the call most worth revisiting against observed routing.
  - Should `known` ever be DROPPABLE? Deliberately no, here: the false-negative is the costly error, and
    forget's reversible conjunctive gate already evicts genuine stragglers without a relevance-driven drop. A
    HARD known-drop (skip synthesis entirely) is a precision optimization to earn with a gold set that shows
    the `known` verdict is reliable — not to pay for speculatively while it is one cheap Haiku call's opinion.
  - The relevance verdict shares the markers' uncalibrated-confidence limit (one generative call; ADR-0005) —
    advisory, which is exactly why it only orders and the digest build degrades gracefully.
  - Per-chunk digest cost is the `digest_context` cache's reason to exist; if the concept set ever outgrows an
    in-prompt digest, the same queryable-index move ADR-0018 §5 defers applies here too.
  - **The `weigh → relevance` framing.** ADR-0005's deferred roadmap-#1 was a `weigh` marker — a relevance
    WEIGHT. It shipped here as a categorical relevance VERDICT (novel/known/contradicts), not a continuous
    weight: relevance is one question with three mutually-exclusive answers, where a per-kind score would
    over-promise a precision one cheap Haiku call can't deliver. The roadmap item is honored in intent (judge
    each learning against what's already known), re-shaped in form.
  - **The per-chunk digest COST is real and uneven.** The digest rides EVERY signal-bearing chunk — including
    the majority that yield zero events — and on a COLD or small store every verdict is `novel` by construction
    (nothing to be known against), so the whole relevance pass is pure overhead until the concept layer fills
    in. It is bounded (the `digest_context` cache pays the O(concepts) re-parse once per run) and
    self-correcting (the overhead shrinks to signal as the store matures), but it is not free on a young store.
  - **The API-caching seam.** Provenance-relevance (`relevant_to`) earns its keep only ABOVE `DIGEST_BUDGET`:
    below the cap the same concept SET renders for every chunk — only REORDERED — so per-chunk rendering buys
    nothing a global digest wouldn't. A single global digest hoisted into a CACHED system prefix would then
    beat per-chunk rendering on cost, once glean moves off the CLI completer (which doesn't cache across the
    per-chunk calls). Moot today — glean is on the uncached completer and the store is below budget — but the
    crossover is: above budget, keep per-chunk relevance; below it and on a cached prefix, hoist one global
    digest.

## References

ADR-0018 (the concept digest — the in-prompt "what we already know" this reads; `digest_context`/`concept_digest`
extended here; the bounded-by-budget, no-embeddings bet 4b inherits and the false-novelty risk 4a flagged for 4b
to fix). ADR-0005 (glean's markers — classify-don't-gate, recall-first, the invisible-false-negative argument;
**the deferred roadmap-#1 relevance/`weigh` marker this finally builds**, and the combined-vs-cascade economics
decision 4 extends across stages). ADR-0010 (dream v2 — `salience` as the priority-queue key decision 3 scales;
the working set this orders; §8 process-by-salience). ADR-0012 (the weaken path — dream's precise LATE
belief-change judgment that glean's cheap EARLY `contradicts` complements). ADR-0013 (the facet substrate —
`facet_score`/`facet_overlap` reused for `relevant_to` and `chunk_facets`). The acceptance tests:
`tests/test_glean.py` (the stored verdict + coercion), `tests/test_concept_digest.py` (the relevant_to ordering),
`tests/test_dream.py` (salience ranks contradicts/novel above known).
