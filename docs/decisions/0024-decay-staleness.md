# 0024 — decay/staleness: quiet un-corroborated concepts surface a retire proposal

- Status: accepted — implemented 2026-06-28 (all 21 offline suites green, incl. the new `test_staleness`)
- Date: 2026-06-28
- Supersedes: — (nothing). Fulfills the "Decay (TODO)" the third-temporal-feature section of ADR-0023 named.
- Superseded by: —
- Extends: ADR-0012 (WEAKEN — handles moved-on, this handles quiet), ADR-0023 (recency-trust — the same
  valid-time signal, one layer up), ADR-0016/0017 (the ops proposer + the tier-2 gate this rides).

Code (`ratchet/garden.py` — `concept_last_corroborated`/`stale_concepts`/`_stale_retire_desc`/`propose_stale`,
`STALENESS_DAYS`/`STALE_MODEL`, wired into `propose_main` as the `--stale`/default sub-pass; reusing
`dream._session_valid_times`/`config.age_days`/`queue_proposal`/`op_stakes`/`RESOLVE_VERBS`) is the source of
truth; this records the *why*.

## Context

A reviewed concept is the most trusted artifact in the system, and it never expires. WEAKEN (ADR-0012) demotes
a concept you **moved on from** — a contradicting observation lands, net entrenchment drops, the takeaway
un-graduates. But sulin's framing exposed the gap: *"outdated almost always means you moved on — a
contradiction, already handled. But a learning that just goes **QUIET** — a topic you haven't touched in
months — isn't flagged today."* A concept can rot by **pure disuse**: never contradicted, just never
re-lived. Nothing demotes it, so it lingers in `valid_concepts`, feeds dream's belief-change judgments and
`generate`'s projection, and quietly mis-represents what sulin still believes.

The signal already exists — we just weren't reading it on the concept layer. ADR-0023 gave every piece of
evidence a **valid-time** (the session's `mtime` — when the conversation happened) and weights takeaway
entrenchment by it. The concept-layer analogue is one subtraction: `now − the most-recent valid-time among a
concept's evidence`. A concept whose newest backing conversation is many months old has gone stale by disuse.

## Decisions

### `last-corroborated` = the most-recent evidence valid-time — recompute-on-read, never stored

`concept_last_corroborated(concept, valid_times)` walks the **trust chain, read-side**: each evidence
pointer's `cleaned_hash → blobstore.session_of` (cleaned → raw → session id) `→ valid_times[sid]` (the
session's date, `dream._session_valid_times`, reused verbatim). It returns the **newest** such valid-time. The
*most-recent* (not oldest, not average) is the right reduction: one fresh corroboration means the concept is
alive, however ancient its other evidence — exactly the **re-lived** property recency-trust relies on.

It is **recompute-on-read** (the ADR-0013/0023 ethos): no date is written onto the concept. A concept
re-corroborated tomorrow has a newer last-corroborated *for free*, with no stored field to desync — the same
reason `net_entrenchment` recomputes its weights from session ids rather than caching them. The session
valid-times are folded **once** per pass and `now` is pinned **once**, so the pass is one transcript scan and
deterministic under an injected `now`.

A concept with **no datable evidence → None → treated FRESH** (skipped, never flagged). This is **recall-first
**, mirroring `recency_weight`'s missing-date → 1.0: the costly error is retiring a real concept we merely
couldn't date, so an undateable concept is never proposed for retire.

### A DETERMINISTIC staleness pass that PROPOSES retire — never auto-retires (recall-first)

`stale_concepts(root, days=STALENESS_DAYS, now)` returns the valid concepts past the horizon;
`propose_stale(...)` queues a **`retire`-stale `garden_proposal`** for each. The non-negotiable invariant:
**staleness only ever PROPOSES.** A quiet concept rides the **existing tier-2 flow** unchanged — `queue_proposal`,
keyed on the op identity via `mint_proposal_id`, with `op_stakes("retire") = 0.80` (HIGH), so it **always
queues for the 3d human gate and never auto-applies**. The human decides:

- **accept** → `accept_proposal` applies the 3c-i `retire`; the concept drops from `valid_concepts`.
- **reject** → the concept is kept *and* SUPPRESSED: `queue_proposal` reads `latest_decision(pid).verb ∈
  RESOLVE_VERBS` and leaves a resolved proposal standing, so a re-run **never re-nags** (the MemPrompt
  "re-suggests dismissed things" trust-killer the reject suppression exists to close — ADR-0017).

No LLM call — the pass is pure structural arithmetic over valid-times — so it is **cheap to run every `garden
propose`**, wired in as a default sub-pass alongside the LLM cluster proposer (both land as
`garden_proposal`s the human reviews together). `--no-stale` skips it; `--stale-only` runs it ALONE (no API
key); `--stale-days` overrides the horizon. Because the proposal id is the op identity `{retire,
concept_id}`, a staleness retire and an LLM-proposed retire of the **same** concept **unify** into one
proposal — and a prior rejection of either suppresses both.

### The rationale is byte-stable — the age is recompute-on-read, not frozen into the blob

The proposal's rationale anchors on the **stable last-corroborated DATE** plus the threshold (`"stale:
untouched since <date> — no corroboration in over <N>d. Re-confirm or retire?"`), **not** the live "days
ago." This is deliberate and is the one place the implementation departs from the prompt's literal `"<N>d
ago"` phrasing: a live age changes every day, so embedding it would churn a fresh blob version on every run
while the concept stays quiet — violating the codebase's load-bearing *byte-identical no-op* invariant and
ADR-0023's *never store the age, recompute it* ethos. The date conveys the staleness and is fixed while the
concept stays quiet, so a re-run is a true no-op; the CLI still shows the live "~Nd ago" (computed at
presentation, never stored).

### The SELF-CLEARING property — no timeless-vs-changing classification

The elegant payoff, inherited from ADR-0023: we do **not** classify a concept as "timeless preference" vs
"changeable fact." A still-true preference keeps being **RE-LIVED** — fresh sessions cite it, advancing its
last-corroborated inside the horizon — so it simply never enters `stale_concepts`. Only a genuinely-untouched
concept ages past the threshold. Disuse is the signal; no taxonomy, no per-concept flag.

### `STALENESS_DAYS = 270` — UNTUNED, and recall-first so it errs generous

The disuse horizon is a single module constant, **flagged UNTUNED at its definition** — a months scale (~9
months): long enough that an active preference re-lived across sessions never trips it, short enough that an
abandoned topic surfaces within a year. Because the gate only PROPOSES (never auto-retires), a too-*low*
value costs only review attention, never a lost concept — so it errs generous. Wants a gold set, the same
posture as `RECENCY_HALF_LIFE_DAYS`/`MATURITY_WEIGHT` (ADR-0023) and `AGING_LAMBDA` (ADR-0021); one edit
retunes.

## Consequences

- **Good:** pure disuse now has a surface — a quiet concept becomes a "re-confirm or retire?" proposal at the
  same tier-2 gate, with reject-suppression so the gardener never re-nags, pinned by `test_staleness`
  (1)–(4). Recall-first and never-auto-delete are structural: a stale concept is *only ever* PROPOSED.
  Recompute-on-read keeps the facet ethos (no stored date); the pass is deterministic (injectable `now`) and
  NO-LLM (cheap every run). It reuses the entire tier-2 machinery — `queue_proposal`, `op_stakes`, the
  accept/reject verbs, the suppression fold — adding only a deterministic proposal *source*, so the existing
  consolidation/gate/golden are untouched (the suite stays green additively).
- **The temporal trio is complete.** ratchet now has three orthogonal uses of time, sharing only the
  `age_days(stamp, now)` primitive: **Aging** (ADR-0021) ages the BACKLOG *up* by transaction-time (fairness
  of attention); **recency-trust** (ADR-0023) weights EVIDENCE *down* by valid-time at the takeaway gate
  (trust in a belief); **decay/staleness** (this ADR) flags a QUIET concept *down* by valid-time at the
  concept layer (liveness of a concept).
- **Costs / known limits:** `STALENESS_DAYS` is UNTUNED, awaiting a gold set — ship it as an operability
  knob, not a fitted default. Valid-time is the transcript's **mtime**, a good-but-imperfect "when the
  conversation happened" proxy (the same caveat Aging/recency note). **Open question for a second opinion:**
  staleness stands ALONE as a deterministic retire-proposal source rather than feeding the LLM proposer's
  cluster-tension. The deterministic path is honest (the signal is pure arithmetic, no judgment needed) and
  cheap, and a retire is the right ASK; but an alternative would route a stale concept into 3c-ii as a
  tension nudge so the sharp model could propose *refresh/merge* instead of only *retire* (a quiet concept
  might be better folded into a live sibling than dropped). Deferred — the deterministic retire-proposal is
  the recall-first minimum; the richer LLM-mediated treatment is additive on top.

## References

ADR-0012 (WEAKEN/contradiction — the moved-on case; this is its disuse complement). ADR-0023 (recency-trust —
the valid-time signal + `_session_valid_times`/`age_days`/recompute-on-read this reuses one layer up; its
"third temporal feature" section named this Decay TODO). ADR-0015 (the 3c-i `retire` op an accept applies).
ADR-0016 (the ops proposer + `op_stakes`/`queue_proposal`/`mint_proposal_id` the pass rides). ADR-0017 (the
tier-2 gate + reject-suppression — accept applies, reject suppresses). ADR-0013 (recompute-on-read, the facet
ethos). ADR-0021 (Aging — the sibling temporal feature on transaction-time). ADR-0005 (recall-first — the
false-negative is the costly error, so undateable evidence is fresh and nothing auto-retires). The acceptance
test `tests/test_staleness.py` (last-corroborated = newest valid-time; the horizon; accept retires; reject
suppresses; self-clearing).
