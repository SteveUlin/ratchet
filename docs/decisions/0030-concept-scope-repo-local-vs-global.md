# 0030 — concept scope: repo-local vs global

- Status: accepted — implemented 2026-07-04 (all 27 offline suites green)
- Date: 2026-07-04
- Supersedes: — (the second concept axis ADR-0029 anticipated; lands the repo-SCOPING ADR-0020
  deferred)
- Superseded by: —

Code (`resolve.scope_repo_of` + the claim view's `scope_repo`; `dream.SCOPE_GLOBAL`/`clean_scope`/
`concept_scopes`; `review --scope`/`--set-scope`; `generate --repo` + the region's scope note;
`status`'s CONCEPTS scope split) is the source of truth; this records the *why*.

## Context

The kind facet (ADR-0029) typed WHAT a concept is — conduct vs lookup. It left WHERE untyped: a
lesson about one repo's fleet harness is behavioral, true, and reviewed, yet projecting it into the
global CLAUDE.md spends every other project's rules budget on a rule that fires nowhere. It belongs
in that repo's CLAUDE.md. Same failure shape as 0029 — the only lever was reject, which throws away
a true belief — on a second, orthogonal axis.

## Decision

Every concept carries a **scope**: a repo label (applies in that repo — `generate --repo` routes it
into that repo's CLAUDE.md) or `global` (applies everywhere — the default projection). The same
trust boundary as kind, with one mirror-image difference: **the proposal needs no LLM**, because
where a lesson was learned is already in the evidence.

1. **Derivation proposes.** A claim's `scope_repo` derives at fold time from its live evidence's
   subject keys (`resolve.scope_repo_of`): every quote in ONE repo → that repo's label; 2+ repos or
   none → `global`. A multi-repo lesson is de facto general, and a lesson with no known home applies
   everywhere — pinning narrower is what the reviewer's override is for. Deterministic, recomputed
   per fold like support and scope-of, stored nowhere.
2. **Review confirms.** The card shows `SCOPE: <repo> (derived)` only when non-global (the same
   card-noise judgment as kind). `--accept` records the scope on the decision (default = the
   derivation; `--scope` overrides); `--set-scope <concept> <scope> [--reason]` re-scopes an
   existing concept — the backfill path, valid targets only (a decision on a retired concept would
   resurrect it, exactly set_kind's guard). The vocabulary is **open** — any repo label the reviewer
   names — so the only refusal is a blank scope: coercion still absorbs derivation noise (absent →
   global), but a reviewer's explicit empty string is an error, never a place.
3. **The view derives.** `dream.load_concepts` attaches each concept's scope from decisions only:
   latest `set_scope` > the accept's recorded scope > `global` (the legacy default) — the same
   latest-decision-wins fold as kind, immune to garden re-versioning for the same reason.
4. **generate routes.** The default projection takes behavioral ∧ global — the global CLAUDE.md gets
   only what applies everywhere. `--repo X` projects behavioral ∧ scope=X instead, for `--target
   ~/X/CLAUDE.md`. The region's scope note sits beside the kinds note and states the filter plus
   where the excluded concepts live; a `--repo` matching no concept's scope is refused WITH the
   scopes present (ADR-0027 — a typo must not silently project nothing).
5. **status counts.** The CONCEPTS line gains the scope split only when any non-global exist:
   `3 valid (2 behavioral · 1 reference; 1 scoped: claude-bus×1)`.

## Why this shape

- **Two axes stay orthogonal.** Kind answers "does this shape conduct"; scope answers "where". A
  reference fact can be repo-scoped; a behavioral rule can be global. Folding scope into kind
  (`reference-claude-bus`…) would have multiplied the vocabulary instead of crossing two facets.
- **The evidence proposes because it can.** Kind needed the LLM (shape is a judgment); scope is a
  fact the subject keys already carry, so an LLM hop would add an untrusted proposer where a
  deterministic derivation exists. The reviewer's decision stays authoritative either way.
- **Coercion is global-first, asymmetric like kind's.** A wrongly-global rule is visible in the
  global region, the card, and the diff; a wrongly-scoped rule silently vanishes from the CLAUDE.md
  everyone reads. So every ambiguous scope reads `global` and narrowing is always an explicit call.
- **Open vocabulary, not a registry.** Repo labels come from the world (cwd basenames), not a
  schema; a closed set would go stale with every new project. The store itself is the registry:
  generate refuses a scope no concept carries, listing what exists.

## Consequences

- Cards gain `scope_repo`, accepts gain `--scope`, and `--set-scope` backfills concepts accepted
  before the axis — additive; legacy concepts read `global` untouched, and per-repo CLAUDE.mds
  become one `generate --repo X --target ~/X/CLAUDE.md` away.
- The scope note makes every region self-describing on both axes; a reader of the global CLAUDE.md
  learns repo-local rules exist without seeing them.
- Deferred: deriving a DEFAULT scope for already-accepted concepts from their evidence facets (the
  backfill stays reviewer-driven via `--set-scope`), and a per-repo staged default target.
