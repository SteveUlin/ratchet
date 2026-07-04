# 0029 — concept typology: behavioral vs reference

- Status: accepted — implemented 2026-07-03 (all 27 offline suites green)
- Date: 2026-07-03
- Supersedes: — (extends the concept layer of ADR-0008 and the projection of ADR-0020; the §5
  high-confidence view gains a facet, nothing forks)
- Superseded by: —

Code (`dream.CONCEPT_KINDS`/`clean_kind`/`concept_kinds`; the `kind` on `synthesize`'s claim version,
`synth/2`; `review --kind`/`--set-kind`; `generate DEFAULT_KINDS`/`--kinds` + the region's kinds note;
`status`'s CONCEPTS split) is the source of truth; this records the *why*.

## Context

A live review sitting (2026-07) surfaced that **faithfulness and generation-usefulness are
orthogonal**. Claims like "ultracode = env-set effort level", "`--effort` overrides the env var",
"hooks can't read context-window data" pass every gate ratchet has: verbatim evidence, recurrence,
an honest `why`. They are true and worth keeping. And they still don't belong in CLAUDE.md the way
"verify the fix is in the exact artifact before the risky action" does — one is a fact you'd look
up, the other shapes conduct. The concept layer was untyped, so `generate` projected everything the
reviewer accepted: the only way to keep a reference fact OUT of CLAUDE.md was to reject it, which
throws away a true, reviewed belief. The rules budget — a reader's attention inside CLAUDE.md — was
being spent on lookup material.

## Decision

Every concept carries a **kind**: `behavioral` (shapes conduct — prefer X, verify Y before Z) or
`reference` (a mechanism/fact you'd look up). The facet moves through the pipeline on the standing
trust-boundary discipline — the LLM proposes, the reviewer's decision is authoritative:

1. **synthesize proposes.** The Sonnet output gains `"kind"`, one defining sentence in the contract,
   stored on the claim version beside title/why (`PROMPT_VERSION` → `synth/2`). Unknown coerces
   `behavioral` — recall-first: a wrongly-behavioral rule is caught at the review and diff gates; a
   wrongly-reference lesson silently vanishes from generation.
2. **Review confirms.** The pending card shows the proposal (`claim_kind`; a printed line only when
   `reference` — the non-default is the card line worth having). `--accept` records the kind **on
   the decision** (default = the proposal; `--kind` overrides, and an out-of-vocabulary override is
   refused, never coerced — coercion absorbs a model's noise, not a reviewer's typo). A new verb,
   `--set-kind <concept> <kind> [--reason]`, appends a re-kinding decision on an EXISTING concept —
   the backfill path for concepts accepted before the typology, reviewer-owned like retire. It
   refuses non-valid targets: a fresh decision on a retired concept would become its latest
   lifecycle decision and resurrect it.
3. **The view derives.** `dream.load_concepts` attaches each concept's kind from decisions only:
   latest `set_kind` > the accept's recorded kind > `behavioral` (the legacy default). On decisions,
   never the blob, so a garden op re-versioning a concept (merge's evidence union) cannot drop it.
4. **generate filters.** The projection takes `behavioral` only by default (`DEFAULT_KINDS`);
   `--kinds behavioral,reference` widens deliberately, and the region's header note states the
   filter and the excluded count, so a CLAUDE.md reader knows the region is a filtered view.
5. **status counts.** The CONCEPTS line splits: `2 valid (1 behavioral · 1 reference)`.

## Why this shape

- **Reject was the wrong lever.** Without the facet, "true but not a rule" had no verdict: accept
  polluted CLAUDE.md, reject discarded a reviewed belief. The kind separates *is it true* (the
  review gate, unchanged) from *where does it land* (the generation gate, now typed).
- **The reviewer owns the kind, end to end.** The model's proposal is a default, nothing more; the
  accept records the confirmed kind as an append-only decision and `set_kind` outranks it later —
  the same latest-decision-wins fold every other lifecycle state rides (ADR-0007/0008).
- **Recall-first coercion, asymmetric by design.** The two failure modes are not symmetric: a
  reference fact projected as a rule is visible in the card, the region note, and the `--diff`; a
  behavioral lesson mis-typed reference disappears from generation with no surface to catch it. So
  every ambiguous kind reads `behavioral`.
- **An explained, adjustable default — never a hidden rule (ADR-0027).** `--kinds` is the escape
  hatch, its help says why reference sits out (the rules budget is behavioral surface), the region
  states what it excluded, and an empty/invalid selection is refused rather than silently
  projecting nothing.
- **Two kinds, not a taxonomy.** The sitting exposed exactly one axis: does this shape conduct or
  get looked up? More kinds (styles? env facts? per-repo?) are speculative machinery without a
  measured need; the closed vocabulary can grow by a later ADR.

## Consequences

- Cards gain `claim_kind`, accepts gain `--kind`, and the new `--set-kind` verb backfills the
  owner's already-accepted reference concepts (e.g. the ultracode fact) — additive; existing
  consumers keep working, legacy concepts read `behavioral` untouched.
- The `synth/2` bump honestly re-opens prose synthesis for not-yet-prosed claims under the sharper
  contract; already-prosed claims keep their versions (re-synthesis stays `--claim`-demand only).
- `reference` concepts stay first-class everywhere except the default projection: dream's digest,
  the gardener, `review --concepts`, and `--kinds`-widened generation all still see them. A future
  "reference sheet" projection (a second region or file for lookup material) is a natural follow-up
  if the reference set grows.
