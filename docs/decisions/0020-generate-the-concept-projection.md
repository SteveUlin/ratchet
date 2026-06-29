# 0020 — generate: the mechanical projection of concepts into a marked CLAUDE.md region

- Status: accepted — implemented 2026-06-28 (all 18 offline suites green, incl. the new `test_generate`)
- Date: 2026-06-28
- Supersedes: —
- Superseded by: —

Code (`ratchet/generate.py` — `project`/`_render_body`/`_trigger`, the `START`/`END` markers +
`_region_span`/`_splice`/`apply`/`diff`/`default_target`) is the source of truth; this records the *why*.
This is **Section 5** — the LAST functional stage. It CLOSES THE LOOP: transcript → cleaned → chunk →
event → takeaway → concept → **CLAUDE.md**.

## Context

Every stage up to here MAKES knowledge: tap/weave/chunk/glean/dream mine and synthesize, review GATES, the
gardener (concepts/garden) STRUCTURES. The concept layer is the result — ratchet's curated source of truth,
what survives human review (ADR-0008). But a concept the model never reads changes nothing. The loop only
closes when a reviewed concept reaches the place Claude Code actually consults: a CLAUDE.md.

The framing that drives the whole design: **CLAUDE.md is NOT the knowledge store.** The concepts are. A
CLAUDE.md is a LEAN, GENERATED PROJECTION of the concepts — a downstream communication channel, the way a
website is a projection of a database, not the database. That inverts the naïve "append a learned rule to
CLAUDE.md" model (which makes the file the store, and then has no answer for retraction, dedup, or bloat) and
hands generate four properties for free: it is a pure function of the valid set, it is re-runnable, retraction
falls out of the input, and the file stays bounded because history lives in the blobs.

One more pressure shaped it: a CLAUDE.md is a HUMAN artifact too. A projection that overwrote the whole file
would be unusable — the human has hand-written rules generate must never touch. So generate cannot own the
file; it can only own a fenced-off REGION of it.

## Decisions

### 1. A MECHANICAL transform, no LLM — and not a Block

generate reads `review.valid_concepts` (= `dream.load_concepts`) and renders. There is NO model call: the
concept `statement` is already human-reviewed text, so it is used VERBATIM. Re-wording it with an LLM would
re-open an untrusted hop AFTER the gate — the one place the pipeline has earned trust — to buy polish; not
worth it. Determinism is the payoff: same valid set → byte-identical region, which is what makes `--apply`
idempotent and the diff reviewable.

It is also NOT a `block.Block` (ADR-0009). A Block is a per-item driver (per-event, per-chunk, per-concept)
with budget/resume/commit-per-item. generate is a GLOBAL projection over the WHOLE valid set at once —
structurally like `review`'s gate, which is also global, not a Block. There is nothing to resume and no
per-item cost; forcing it into the driver would add machinery with no job.

### 2. TAG-LED grouping; the de-redundant rule + repo-trigger + provenance-marker format

Concepts are GROUPED BY their PRIMARY tag — `facets["tags"][0]` (the gardener's managed theme, ADR-0014, read
off the same `digest_context` facet pass). The heading is the tag as a markdown section (`## jj`, `##
environment`); an untagged concept falls to a trailing `## general` bucket. The projection is THEME-shaped.

This REPLACES the v1 facet-cluster grouping (the `**Shared file `/repo/foo.py`:**` headings + `<!-- cluster
-->` comments + `_shared_basis`, all removed). v1 grouped by cluster because a cluster is a complete
partition and tags are nominally multi-membership; the call was wrong in PRACTICE. Two pressures flipped it:

- **Provenance-shaped reads like a database dump, not a CLAUDE.md.** The reviewer's case: the cluster grouping
  "married a nix rule and a fish rule by accident of a shared file" — provenance co-location is not thematic
  kinship, so the outline grouped rules by where-they-came-from, not what-they're-about. sulin's real
  `~/.claude/CLAUDE.md` is 100% THEME-shaped (`# Communication`, `# Environment & Tools`, `## jj`) with tight
  imperative bullets and zero provenance noise — the target shape is theme, full stop.
- **The multi-tag/untagged objection dissolves under a PRIMARY-tag partition.** Take each concept's FIRST tag
  (sorted facets → deterministic), untagged → `general`. Now every concept lands in EXACTLY ONE group — the
  same clean partition the cluster gave, but theme-shaped. Multi-tag is not ambiguous (the primary wins),
  untagged is not orphaned (the bucket catches it). The very property that justified cluster is preserved.

Each concept renders as a RULE — a bullet carrying three things:
- its **`statement` verbatim** (already vetted — the rule text itself);
- a **repo trigger** where a repo exists — "When working in &lt;repo&gt;: …" — state-conditioning (AutoGuide):
  the repo is the strongest "where am I" provenance facet. The THEME now lives in the HEADING (the tag), so the
  trigger no longer echoes it — heading carries the WHAT, trigger the WHERE, no redundancy. A concept with no
  repo renders UNCONDITIONALLY (just its statement). Any path-shaped repo is basenamed (no `/home/sulin/…`
  leaks into a rule).
- a **provenance marker** — a trailing `<!-- c-id -->` HTML comment. This is the trust chain reaching the
  PROJECTION: a reader greps the id back to its concept → re-validated evidence → raw transcript (the chain
  ADR-0013/0008 maintain, now reaching the generated output). An HTML comment, not visible text, so the rule
  reads clean while staying traceable in source — unobtrusive AND greppable, the right call kept from v1.

Order is groups by descending size then tag name (the `general` bucket always last), members by entrenchment
(distinct cited sessions desc) then id — fully deterministic and order-stable, the idempotency precondition.
The projection renders ALL valid concepts (no `budget` truncation — unlike the digest, this is not a bounded
prompt; bloat is bounded by the gardener's consolidation + review's maturity gate, not by a cap here).

### 3. The MARKED region + refresh-in-place — bloat bound, human content preserved

generate owns ONLY a delimited span: `<!-- ratchet:generated START … -->` … `<!-- ratchet:generated END -->`.
Everything outside is human-owned and byte-preserved. `--apply` locates the span (`_region_span`) and REPLACES
it in place (`_splice`), creating it at the end of the file when absent. Refresh-in-place — not append — is
the bloat bound: the region is rewritten each run from the current valid set, so it never accretes; the
file's size tracks the live concept count, and history lives in the concept blobs, not in stale CLAUDE.md
lines. A LONE, DUPLICATED, or out-of-order marker is treated as a CORRUPTED/AMBIGUOUS region and REFUSED
(`_region_span` raises, `apply` writes nothing) rather than guessed at — the safe failure is to touch
nothing. The MULTIPLICITY refusal (`count(START) != 1 or count(END) != 1`) is the load-bearing case: a
CLAUDE.md that DOCUMENTS the markers (an example block above the real region) has them twice, and
find-the-first would splice the WRONG span — refusing on a count mismatch is what makes "human content is
never clobbered" structural, not merely probable. The empty valid set still renders a well-formed empty region (a
sentinel comment), so an empty store refreshes idempotently and reads as "projection empty," never "did
generate run?"

### 4. Retraction for free

The projection's input is the VALID set, where a retired/superseded/split concept is simply ABSENT (its latest
decision folds it out — ADR-0008/0015). So the next `project`/`--apply` DROPS its rule. This is the
systems-memory "retraction" requirement (a rejected belief must UNMAKE its downstream config, not merely stop
being added): because generate is a pure function of the valid set and refreshes the whole region, a concept
leaving the set removes its rule with ZERO extra mechanism. Re-apply with unchanged concepts is byte-identical
(a no-op write is skipped) — the same determinism, the other direction.

### 5. The stage → diff → apply flow as the SECOND review gate; the safe staged-target default

The concept gate (review, ADR-0008) decides what is TRUE. A second, distinct gate decides what lands in a real
CLAUDE.md, and the DIFF IS that gate:
- `generate` / `--dry-run` — print the projected region (no write).
- `--diff` — a stdlib `difflib` unified diff of the proposed region vs the target's CURRENT one — exactly what
  `--apply` would change, for the human to read FIRST.
- `--apply` — splice the projection into the target's marked region.

The default `--target` is a STAGED path, `$RATCHET_DATA_DIR/generated/CLAUDE.md`, NEVER a real CLAUDE.md. The
safe path must be the DEFAULT path: a tool whose default overwrites the user's hand-tuned config is a footgun,
so the human points `--target` at a real CLAUDE.md (or copies the region out) DELIBERATELY. Two gates, two
questions — "is this belief real?" (review) and "do I want this rule in this file?" (the diff) — kept separate.

### 6. Deferred follow-ups

v1 is exactly: valid concepts → ONE marked CLAUDE.md region, mechanical, refresh-in-place, retraction,
stage/diff/apply. Deliberately deferred:
- **skills / `.claude/rules/*`** — projecting a concept into a runnable skill or a scoped rules file is a
  richer target than a flat region; v1 proves the projection mechanism on the simplest target first.
- **repo-SCOPING** — deciding WHICH CLAUDE.md a concept belongs to (per-repo routing off its provenance
  facets) is real, but it multiplies the target surface; v1 is one region the human aims.
- **LLM polish** of the rule text — re-phrasing the verbatim statement into tighter rule language is the one
  place a model could help, but it re-introduces an untrusted hop past the gate; earn it later, against a gold
  set, behind the diff.

## Consequences

- **Good:** the loop CLOSES — a reviewed concept now reaches CLAUDE.md, the place Claude Code reads. The
  projection is a pure, deterministic function of the valid set, so it is re-runnable, idempotent, and gives
  retraction for free; the marked region bounds bloat and preserves human content; the staged default makes
  the safe path the default path; the diff is a real second gate. Additive — a new module + CLI + flake app,
  no change to any existing stage; the 17 prior offline suites stay green. `test_generate` pins the contract:
  tag-led, theme-grouped + provenance-marked + de-redundant rendering, the primary-tag/`general` partition, the
  multiplicity-refusal (markers twice → raise, file untouched), human content preserved above AND below on
  `--apply`, a retired concept vanishing on re-project, byte-identical re-apply, and the empty projection.
- **Costs / open questions / second looks:**
  - **Grouping by cluster vs tag was the call most worth a second look — and it got one** (Decision 2 above is
    the resolution: tag-led, primary-tag partition + `general` bucket, replacing facet-cluster). What remains
    open is the PRIMARY-tag choice: a multi-tag concept shows under ONE heading only (its first, alphabetically)
    — fine while the gardener keeps tags few and sharp, but a concept that genuinely spans two themes is
    single-homed. A cross-listing (the rule under each of its tags) is the revisit if that bites; it trades the
    clean partition for duplication, so it waits for evidence the single-home loses signal.
  - **The repo trigger** ("When working in &lt;repo&gt;: …") is a coarse "where" — a repo NAME, basenamed. A
    path glob might condition better, and a concept spanning several repos shows only its first. Untuned, like
    the weights elsewhere; the format is a knob, pending real CLAUDE.md use.
  - **The provenance marker is an HTML comment** — invisible in rendered markdown, present in source. That
    favors a clean rule for the model over a visible `· [c-id]` a human skimming RENDERED output would see; if
    tracing-while-reading matters more than rule cleanliness, the visible form is a one-line change.
  - **No repo-scoping** means every concept projects into the ONE target the human aims, regardless of which
    repo it came from — fine for a single shared CLAUDE.md, wrong once concepts span unrelated projects. That
    is the deferred scoping work, not a bug.

## References

ADR-0008 (review — the human gate; the concept is the source of truth and CLAUDE.md/skills are ALWAYS gated
output GENERATED from it, the inversion this stage realizes; `valid_concepts`, whose absence-on-retire gives
retraction for free). ADR-0007 (every artifact is a blob; the valid set is a derived fold, history lives in the
blobs — which is why the file can be refreshed-in-place and bounded). ADR-0013 (the concept facet substrate —
the provenance facets the grouping clusters on and the repo trigger reads). ADR-0014 (the gardener's managed
tags — the semantic theme that now LEADS the grouping, the primary tag per concept the headings carry).
ADR-0018 (the concept digest — `digest_context`, the single facet pass this projection reuses to read each
concept's statement + tags off the graph nodes). The acceptance test: `tests/test_generate.py`.
