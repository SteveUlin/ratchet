# 0032 — dissect dream: the succession

- Status: accepted 2026-07-05
- Date: 2026-07-05
- Supersedes: — (a structural amendment to ADR-0028's supersession, not a behavior change)
- Extends: 0028 (resolve supersedes dream v2), 0013 (recompute-on-read), 0023/0027 (the machinery
  extracted here).

## Context

ADR-0028 superseded dream v2's route+apply core with `resolve`, but `dream.py` was never dissected:
it still hosts the pipeline's LIVE kernel — the valid-time oracle (ADR-0023/0027), the concept
loaders, the event fold — interleaved with the superseded v2 machinery, under a 58-line module
docstring that presents v2 as the live consolidator. Nine modules import it. The debt compounds:

- **An inverted dependency.** `concepts.py` — the concept layer's own module — imports `dream` for
  `load_concepts`/`valid_concept_ids`, because the loaders grew up inside the stage that first read
  them. Every consumer of the concept layer therefore transits the superseded stage.
- **A lazily-masked cycle.** ~10 function-local imports guard "cycles", several with comments naming
  chains that do not exist (resolve's "concepts→dream→glean→subject cycle guard" is false today;
  generate's admits it merely "matches the convention"). Convention-mimicry hides which lazy imports
  are load-bearing.
- **A de facto public API of underscore names.** ~30 cross-module call sites reach for `_`-prefixed
  functions (`dream._session_valid_times`, `resolve.live_edges`, `garden._concept_blob`, …). An
  underscore name imported by another production module is a lie: the prefix promises "file-local,
  free to change" while the import graph says otherwise.

## Decision

Dissect `dream.py` along the live/legacy seam; promote the lied-about names; make every lazy import
either honest or top-level.

### 1. The extraction map

- **`ratchet/temporal.py`** (new) — the valid-time oracle, the ADR-0023/0027 time-and-maturity
  machinery five modules share and no stage owns: `session_valid_times` (promoted),
  `coalesce_sessions`, `same_sitting`, `recency_weight`, `net_entrenchment`, `net_sessions`, and
  their knobs (`MATURITY_WEIGHT`, `MATURITY_SESSIONS`, `COALESCE_HOURS`,
  `RECENCY_HALF_LIFE_DAYS`), each constant traveling with its why-comment. Imports blobstore +
  config only — leafward of every stage.
- **`ratchet/concepts.py`** gains the concept loaders and vocabulary: `load_concepts`,
  `valid_concept_ids`, `concept_kinds`, `concept_scopes` (the twin decision folds, now one
  parameterized `_facet_fold`), `clean_kind`, `clean_scope`, `CONCEPT_KINDS`,
  `CONCEPT_INVALID_VERBS`, `SCOPE_GLOBAL`, the `VERB_*` constants. The concept module owning
  concept loading kills the inverted concepts→dream edge — which is what unwinds most of the lazy
  imports below.
- **`ratchet/events.py`** (new) — the event-fold view over glean's output, stage-neutral:
  `ResolvedEvent`, `resolve_event` (promoted), `working_set`, `filter_by_source`,
  `event_born_map` (promoted), `salience`, `intrinsic_salience` (promoted), `event_markers`
  (promoted), `evidence_entry`, `contradiction_stats` (promoted), the salience weights
  (`W_SURPRISE`/`W_INSIGHT`/`W_RESEARCH`/`W_REL`), `CONTEXT_BYTES`, and the forget knobs
  (`FORGET_TAU`, `FORGET_SALIENCE_FLOOR`).
- **`dream.py` keeps the legacy arm**: `catalog`, `current_takeaways`, `contradicted_takeaways`,
  `mint_takeaway_id`, the v2 route/apply/update/forget/merge machinery, `DreamBlock`, its CLI.

Two judgment calls on the small shared pieces, both resolved by asking what DOMAIN the code speaks:

- **The coercion kit** — `clip` (promoted from `_clip`), `clean_relation` (promoted),
  `TITLE_MAX`/`WHY_MAX`/`NOTE_MAX` — lands in `concepts.py`. These are the claim→concept FIELD
  SCHEMA: `clean_relation` validates a relation against the valid concept set (concepts' own fold),
  `clip` caps prose whose destination is the concept `statement`, and the caps bound those same
  fields. `completer.py` was the runner-up (they scrub untrusted LLM output, like `clean_score`),
  but completer is the transport seam — schema constants there would smear domain into it. No
  third module is earned.
- **The forget knobs and `contradiction_stats`** land in `events.py`. `FORGET_TAU`/
  `FORGET_SALIENCE_FLOOR` gate eviction FROM the working set (both dream's legacy `forget` and
  resolve's live one default to them), and the eviction floor reads `intrinsic_salience` — the
  knobs live beside the signal they gate. `contradiction_stats` counts distinct events/sessions
  over evidence entries — `evidence_entry`'s fold-side twin.

Two legacy reaches survive BY DESIGN, and only these: `resolve.reset_v2` reads `dream.catalog`
(retiring the v2 takeaways is its whole job), and `resolve._mint` calls `dream.mint_takeaway_id`
(claims deliberately share the v2 `t-…` id space so the reset's `retire` decisions land on the
re-minted ids — `decision_binds` exists for exactly this collision). Everything else that imports
dream imports its legacy arm knowingly (review's queue union) or not at all.

### 2. The underscore-promotion rule

An underscore name imported or called by another production module loses its underscore AT ITS HOME,
and every call site updates. Promoted here: `resolve.live_edges`, `resolve.reject_merge_facts`,
`resolve.decision_binds`, `resolve.load_event`, `resolve.event_subject`; `garden.concept_blob`,
`garden.proposal_blob`; `review.record_decision` (né `_record` — garden documents it and tests
drive it); `weave.truncate` (chunk calls it); `concepts.repo_label` (subject calls it); plus the
dream-kernel promotions listed in §1. Genuinely file-local names keep their underscore; tests may
import those from their home submodule directly (a test pinning internals is the one honest reader
of a private name). `garden/__init__.py` re-exports ONLY clean public names — the test-only
coercers (`_clean_op`, `_clean_assigned`, `_clean_new_tags`, `_op_stakes_of`,
`_mint_op_concept_id`) come off the package surface.

NO back-compat aliases: `dream.net_entrenchment = temporal.net_entrenchment` would be the same lie
continuing under a new spelling. Every call site moves instead.

### 3. The lazy-import policy

A function-local import exists ONLY to break a real import cycle, and carries a one-line comment
naming that cycle. After the extraction the survivors are:

- `concepts` → `garden` (garden.propose/tag import concepts at module load);
- `review` → `garden` (garden.ops imports review at module load);
- `sig` → `resolve` (resolve imports sig at module load);
- `status` → `review`/`garden`/`generate` — these serve DEGRADATION, not cycles: a sibling module
  mid-edit zeroes one census section instead of killing the census, and the comments say so.

Everything else hoists to top level: the loaders moving into `concepts` kills concepts→dream, which
unwinds the glean, subject, resolve, synthesize, and dream-itself guards; generate's
convention-mimicry `review` import hoists with them.

### 4. The end state

`dream.py` is the TOMBSTONED legacy arm: its docstring opens by declaring the supersession
(ADR-0028) and states what the file is NOW — the v2 takeaway machinery review still unions into its
queue, kept until that legacy queue empties. A section banner marks the v2-machinery boundary. When
`dream.catalog` is empty on the live store and review's union feed reads zero takeaways, a future
ADR deletes the module entirely (tests and history ride jj, not the tree).

## Consequences

- Nine importers rewire from `dream.*` to `temporal`/`concepts`/`events`; the tests move with them.
  Moved code moves VERBATIM — docstrings, why-comments, knob comments travel with their code; the
  only new prose is module docstrings, import lines, and location-referencing sentences that would
  otherwise become lies ("kept in dream to avoid a cycle").
- Pure moves change no rendered bytes: the golden-file tests pin this.
- The no-cycle property becomes checkable in one line:
  `python -c "import ratchet.tap, …, ratchet.dream"` at top level, no lazy escape hatches for the
  production import graph.
- `concepts.py` grows toward "the concept layer" (loaders, vocabulary, schema, facet graph) — if it
  keeps growing, a later split should separate the graph view from the loaders; not earned today.
