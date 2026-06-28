# 0012 — the WEAKEN path: a symmetric contradiction stat, demotion by net entrenchment

- Status: accepted — implemented 2026-06-28 (all 11 suites green, incl. the golden-file `test_weaken`)
- Date: 2026-06-28
- Supersedes: — (nothing). Completes ADR-0010 §10 (the deferred belief-change/`contradicts` path).
- Superseded by: —

Code (`ratchet/dream.py` + `ratchet/review.py`) is the source of truth; this records the *why*.

## Context

dream v2 (ADR-0010) is **STRENGTHEN-only**. A takeaway accrues `support {events, sessions}` as
corroborating events route to it, and the maturity gate graduates it to review once it crosses
`MATURITY_SESSIONS` distinct sessions. But nothing can DEMOTE it: an event that *contradicts* a catalog
takeaway has nowhere to go — the router can only `strengthen` (more evidence FOR), `new` (a different
lesson), or `noop` (noise). A correction ("actually jj pushes are rejected here, use git") gets misfiled
as `new` or dropped, and the now-wrong takeaway keeps its graduation. ADR-0010 §10 flagged this as the
deferred `contradicts` / belief-change path ("soft revision, not a hard freeze"); this ADR builds it.

The shape is forced by two field anti-patterns:

- **Never auto-delete a contradicted concept.** mem0's documented failure is a hard-DELETE on a
  contradiction — a single noisy event evicts a well-corroborated belief, and the trace is gone. Zep's
  invalidate-don't-delete, AGM's minimal-change, and SSGM's quarantine all say the same: a contradiction
  should *invalidate / demote / quarantine*, never excise. ratchet already treats `stale`/`merge`/`retire`
  as reversible decisions over retained blobs; a contradiction must be the same.
- **A downvote is the symmetric twin of an upvote, not a special case.** ExpeL maintains insights with
  add / upvote / downvote / evict-at-0. dream already has add (`new`) and upvote (`strengthen`) as
  additive BIRCH sufficient-statistics; the missing twin is a downvote maintained in the *same* additive
  style — a contradiction count that accrues exactly like support, not an LLM rewrite.

## Decisions

### 1. A 4th route verdict, `weaken` — asymmetric coercion of an unknown id

`ROUTE_SYSTEM` gains `weaken`: prefer it when the observation is evidence a catalog takeaway is WRONG or
NO LONGER HOLDS (a contradiction/correction), DISTINCT from `new` (a genuinely different lesson).
`PROMPT_VERSION` bumps `dream/2 → dream/3` (the instruction changed, so not-yet-consolidated events
re-route under the sharper prompt; consolidated events never re-enumerate, so nothing already decided
moves).

`_clean_route` coerces the untrusted verdict ASYMMETRICALLY, and the asymmetry is the load-bearing
choice: a `strengthen` of an unknown/null id → `new` (it is still *a* lesson, just not the named one), but
a `weaken` of an unknown/null id → `noop`. **Never mint a "negative" takeaway** — there is no such
artifact, and a contradiction whose target the model wasn't shown is not itself a durable lesson; the
event stays routable on the next prompt bump. The list-number→id mapping in `route` is decision-agnostic,
so it already serves weaken.

### 2. A symmetric contradiction sufficient-statistic on the takeaway

Mirroring the support side (with one deliberate difference — see Costs), a takeaway gains `contradictions {events, sessions}`,
`contradicted_by [event_ids]`, and `contradiction_evidence [...]` (span-verified `evidence_entry`-shaped
entries — **the trust chain extends to contradictions**, each entry re-validated on write, carrying the
session it came from so the distinct-session count needs no parallel list). These are additive BIRCH
stats: closed operations over the summary, correct without the raw events — `merge` unions them, the gate
reads them, dedup-by-event_id keeps them idempotent. Old (dream/2) blobs lack the fields, so **everything
reads them defensively** (`tk.get("contradictions") or {...}`); a no-contradiction takeaway therefore has
`contradictions.sessions == 0`, and behaviour is byte-identical to before.

### 3. `update_contradictions` — the downvote, NO LLM

A new function mirroring `update_support` but on the contradiction side and **always cost 0.0**. A
contradiction *records the conflict*; it does **not** rewrite the `why` (support, why, title ride
untouched — there is no synthesis call, so no write-amplification and no LSM compaction-debt the strengthen
path's drift gate has to bound). It builds a new version of the same id: append the contradicting event to
`contradicted_by`/`contradiction_evidence` IDEMPOTENTLY (dedup by event_id → a re-weaken of an
already-recorded event is a byte-identical TimeMap no-op, so sessions can't inflate), recompute the
contradiction stat, bump `last_seen`. `apply` gains a WEAKEN branch: a vanished target falls back to
`noop` (NOT `new` — a contradiction of a gone takeaway is not a new lesson), else commit the version FIRST
then `_write_consolidated(..., "weaken")` LAST (the same crash invariant as strengthen — the event leaves
the working set incorporated AS a contradiction).

### 4. Demotion via NET entrenchment — single-sourced in `current_takeaways`

The maturity gate's predicate moves from `support.sessions >= min_sessions` to **`support.sessions −
contradictions.sessions >= min_sessions`** (`dream.net_sessions`). A strongly-corroborated takeaway
survives a lone contradiction; a contested one **un-graduates** — it drops out of review's clean feed —
**yet stays in `catalog()`, is never retired/deleted, and re-graduates** the moment corroboration returns
across the bar (the golden-file scenario walks new→strengthen→weaken→un-graduate, and `test_weaken §4`
walks the re-graduation). This is quarantine, not excision (Zep/AGM/SSGM).

The net formula lives in **ONE place**. `review.pending` already rides `dream.current_takeaways`
unchanged; `review.incubating` keeps deriving "below the bar" as `catalog` minus
`current_takeaways`, and its `needs` shortfall calls `dream.net_sessions` rather than re-deriving
`support − contradictions`. No file but `dream` knows the formula.

### 5. `contradicted_takeaways(root)` — quarantine is visible, never silently lost

`catalog()` filtered to `contradictions.events > 0`. The query (not a review UI — deferred) that makes a
demoted takeaway spot-checkable, so a contradiction can never disappear into a takeaway that merely fell
below the gate. `merge` unions the contradiction side for the same reason: a near-dup merge must not drop
a contradiction.

`salience`/priority are **unchanged**: a contradicting event is a surprise signal, already the
highest-weighted marker (ADR-0010 §8), so it is already processed first — no re-weighting needed.

## Consequences

- **Good:** the contradiction half is symmetric with support (same additive-stat machinery, same trust
  chain, same idempotency, same commit-order invariant); demotion is reversible and single-sourced;
  no-contradiction takeaways are provably unchanged (`contradictions.sessions` defaults to 0 → net ==
  support, so every prior `test_dream`/`test_review` stays green); the weaken path spends $0.
- **Costs / known limits:** net entrenchment is a *count* subtraction, not a calibrated belief score — one
  strong contradiction and one weak corroboration weigh equally (a future confidence-weighted net is the
  obvious refinement). A weaken never edits the `why`, so a takeaway can read as still-true while demoted;
  the contradiction evidence is the reviewer's signal, surfaced via `contradicted_takeaways` and the deep
  context window, but a review UI that *shows* the conflict inline is deferred. `evict-at-0` (ExpeL's
  final step) is deliberately NOT implemented — dream forgets *events*, never *takeaways*; a contested
  takeaway is quarantined indefinitely, not evicted.
- The two sides are NOT byte-symmetric: the contradiction side tracks distinct sessions INSIDE its evidence
  entries, while support keeps a parallel `sessions_seen` list. This is deliberate — the contradiction
  shape is the simpler one (its `merge` unions ONE list, support's unions two). The right convergence is to
  migrate SUPPORT *down* to this shape (embed `session_id` in `evidence_entry`, drop `sessions_seen`, share
  one `_stats` helper — a net deletion + full symmetry), NOT to grow the contradiction side a redundant
  list. Deferred (it re-versions the support blob format).

## References

ADR-0010 (dream v2 — the strengthen-only model this completes; §10 deferred the `contradicts` path; §4
the BIRCH sufficient-statistics this mirrors; §8 the surprise-weighted salience left unchanged). ExpeL
(2308.10144 — add/upvote/downvote/evict-at-0 insight maintenance). mem0 (2504.19413 — the hard-DELETE
anti-pattern). Zep/Graphiti (2501.13956 — invalidate, don't delete). AGM belief revision (minimal change).
SSGM (quarantine over deletion). The golden-file acceptance test `tests/test_weaken.py` +
`tests/golden/weaken_end_state.json` (expected-vs-actual end-state of the net-demotion scenario).
