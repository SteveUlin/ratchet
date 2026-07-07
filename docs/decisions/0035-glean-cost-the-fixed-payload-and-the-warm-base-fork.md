# 0035 — glean cost: the fixed payload, the tool-less call, and the warm-base fork

- Status: accepted — offline suites green (`test_completer`, `test_glean` extended); the default-on
  slim flags ship, the warm-base fork ships OPT-IN pending a live A/B pilot
- Date: 2026-07-06
- Supersedes: —
- Extends: ADR-0004 (glean's `claude -p` seam), ADR-0009 (the Block driver — warm mode is still one
  per-chunk item), ADR-0019 (the concept digest glean injects), ADR-0025 (tap's self-skip — it covers
  the fork sessions), ADR-0026 (the model points, the code copies — why a tool-less call is safe),
  ADR-0027 (explained, measured-before-default knobs — the opt-in stance).

Code (`completer.py`: the `_TOOLLESS_FLAGS`, the `Completion` cache fields, the `.warm`/`.fork`
session chain; `glean.py`: `FORK_PROMPT_VERSION`, `--warm-base`, `_warm_base_user`/`_global_digest`/
`_warm_session_for`, the cache tallies + per-chunk marker fields) is the source of truth; this records
the *why*.

## Context

Each glean chunk is a fresh `claude -p` process. Measured cost per chunk was ~$0.0646-equivalent
against only ~$0.0075 of visible content — a roughly **9× FIXED payload** rides every call, independent
of the excerpt. Two misconceptions had let it hide:

1. **`--allowedTools ""` is a PERMISSION filter, not a payload filter.** An empty allowlist stops the
   model from USING a tool (the load-bearing max-turns guard: a transcript excerpt is full of tool
   calls, and with a tool available the model sometimes calls one, burning the single turn →
   `error_max_turns`). But the built-in tool SCHEMAS still land in context regardless. Removing them
   needs `--disallowedTools "*"` (code.claude.com/docs/en/cli-reference). Slash-command definitions,
   MCP server schemas, and hook wiring ride the same way — all dead weight for a call that never uses
   any of them.

2. **Cross-invocation caching was CLAIMED but never verified.** The completer docstring asserted "calls
   after the first read the cached prefix at ~0.1×", but the code parsed input/output tokens and
   DISCARDED the cache fields — so the claim was unfalsifiable, and likely false: a cross-invocation
   prompt-cache read needs a byte-stable prefix over Haiku's 4,096-token minimum, and the oneshot
   path's shared prefix (flags + system prompt) may fall under it. A documented property no code
   enforces or even measures is the house's cardinal sin.

The deeper structure: glean's per-chunk prompt = the extraction instructions (system) + the numbered
excerpt + the **concept digest** ("what we already know", ADR-0019). The digest is the same for every
chunk in a tick (below the budget it is content-identical), yet it is re-sent on every one of a
thousand calls — an O(chunks) re-transmission of an O(1)-per-tick payload.

## Decision

**Instrument the cache first; slim every call by default; add an OPT-IN warm-base fork mode that pays
the digest once per tick.**

1. **Measure caching (R0).** `Completion` grows `cache_read_tokens` / `cache_creation_tokens` (parsed
   from the envelope's `usage.cache_read_input_tokens` / `cache_creation_input_tokens`, default 0). They
   ride glean's per-chunk `processed` marker (beside `input_tokens`/`output_tokens`/cost) and the run
   summary's `cache r/w` figure. The docstring now states the TRUTH: caching is *possible*
   cross-invocation but gated on a ≥4,096-token byte-stable prefix, and these fields are how we KNOW
   whether it fired — never again an unverified claim.

2. **Slim every call, default-on (R1).** The shared `_TOOLLESS_FLAGS` add `--disallowedTools "*"`
   (delete the tool schemas), `--strict-mcp-config` (no MCP servers — we pass no `--mcp-config`),
   `--disable-slash-commands` (no skill/command definitions), and `--settings '{"disableAllHooks":true}'`
   (no hook payload or side effects). `--allowedTools ""` is KEPT as the proven max-turns guard: the two
   flags bind distinct things (may USE a tool vs the schema EXISTS in context), so neither is redundant.
   Each ratchet call is a pure text→JSON function — the model never touches a file, the pipeline does all
   I/O (ADR-0026) — so every one of these is dead payload, and stripping it changes no prompt TEXT (no
   PROMPT_VERSION bump). `CLAUDE_CODE_ATTRIBUTION_HEADER=0` was investigated and NOT wired: no such env
   var is documented in the installed CLI (2.1.201) — attribution is a commit/PR byline concern, absent
   from a tool-less pure call anyway.

3. **The warm-base fork, opt-in (R2).** `--warm-base` (glean only) builds the concept digest ONCE per
   tick, seats it — with the system prompt — in a warm BASE session (`completer.warm(system, base_user)`,
   a first call under a fresh `--session-id`), and then FORKS that base per chunk
   (`completer.fork(system, user, session_id)` = `--resume <id> --fork-session`). Each fork's turn is the
   numbered excerpt ALONE; the digest is inherited from the base, not re-sent. The fork's first request
   reads the base's PROMPT CACHE (code.claude.com/docs/en/prompt-caching), and subscription auth
   auto-requests the 1-hour cache TTL. CLI flags are NOT persisted across a resume, so the fork re-passes
   the same `--system-prompt` and every slim flag byte-identically to the warm call — enforced by sharing
   `_TOOLLESS_FLAGS` and the same `system`. The fork path runs under a distinct done-key
   (`FORK_PROMPT_VERSION = "glean/5-fork"`, rode in `params`), so it never touches oneshot glean/4's
   markers.

4. **The warm digest is the GLOBAL ordering.** Oneshot orders the digest per-chunk by facet overlap
   (`relevant_to`); the warm base is ONE shared prefix, so it must be chunk-independent — the global
   entrenchment ordering (dream's). Below `DIGEST_BUDGET=80` concepts the two are content-identical (every
   concept renders, only the ORDER differs); PAST 80 the global ordering truncates the least-entrenched
   tail, which can differ from what per-chunk relevance would keep. An accepted tradeoff, recorded at the
   knob (`glean.GleanBlock._global_digest`): the cache win of paying the digest once buys a
   coarser-but-still-bounded novelty signal past 80 concepts.

## Why this shape

- **Fork dissolves the quadratic AND the contamination hazard at once.** Carrying one long conversation
  and appending chunk after chunk would grow context without bound and, worse, let chunk N see chunks
  1..N−1 — cross-chunk contamination that would corrupt the per-chunk relevance verdict. A fork inherits
  the base and NOTHING else: every fork sees base + its own excerpt, so the isolation glean depends on is
  structural, not disciplined. The digest is transmitted once (the base) and cache-READ by every fork,
  turning an O(chunks) re-send into O(1)-per-tick + a cheap cache read per chunk.

- **Opt-in until measured (ADR-0027).** The cache economics are a live, subscription-specific empirical
  question — whether the fork's cache read actually fires, and how the warm base's one-time cost trades
  against the per-chunk savings. The default MUST NOT flip on a plausible story. `--warm-base` ships off;
  the R0 instrumentation makes the A/B a direct read of marker input-vs-cache tokens, before vs after.

- **The tool-less call is safe BECAUSE the pipeline owns all I/O.** Stripping tools is not a
  capability loss — the model was never allowed to act. It points at lines; the code copies the bytes
  and does every write (ADR-0026). Removing the schemas just stops paying to describe capabilities the
  trust model already forbids.

- **Subscription framing.** The unit of spend here is not dollars but CHUNKS PER RATE-LIMIT WINDOW: the
  batch throttling is a token-rate bucket SHARED with the driving interactive session. Slimming the fixed
  payload and cache-reading the digest both shrink the tokens each chunk costs, so more chunks clear per
  window. (An API key would unlock the Batch API — 50% cheaper, async — the sanctioned batch surface
  ADR-0004 anticipated; a one-line footnote for when that access exists.)

## Consequences

- Every glean/resolve/dream `claude -p` call now carries the slim flag set by default; the run summary
  and per-chunk markers report `cache r/w`, so a permanent no-caching regime is visible, not assumed.
- `glean --warm-base` runs the fork mode under glean/5-fork; a plain (non-session) completer raises at
  construction rather than failing deep in a chunk. Warm mode warms one base PER MODE (transcript vs
  document use distinct system prompts); the warm base's one call per tick is a bounded fixed overhead,
  not folded into the per-chunk `--max-usd` tally — the pilot accounts it from the base session's own
  usage.
- **tap's self-skip covers the fork flood (ADR-0025), confirmed.** The completer pins `cwd=data_root`, so
  every warm/fork session Claude Code writes lands under `~/.claude/projects/<encode_project(data_root)>/`
  — exactly the `self_name` `tap.discover` drops (`name == self_name or name.startswith(self_name + "-")`).
  Fork mode multiplies the session-JSONL count, but they all fall in the one skipped project dir, so tap
  never re-ingests its own extraction runs. No change to tap needed; `--include-self` still lifts the skip.
- **`glean --pilot-report` — the drain IS the experiment.** No chunk is ever re-gleaned to measure:
  every `--warm-base` tick is real forward progress, and its markers also feed a $0 read that folds all
  glean markers into fork-vs-oneshot cohorts. The confound it must correct is the greedy drain (richest
  chunks first, so the fork cohort — added after the oneshot baseline — is structurally poorer); the fix
  is stratification by `structural_score` (extracted as the shared pointer-only, date-blind part of
  `priority()`, so the report's covariate is literally the signal the queue ordered by) and direct
  standardization to the fork cohort's band distribution. The flip-to-default rule is three named,
  legible knobs (ADR-0027): `PILOT_MIN_FORK=50` (band ratios stop being single-digit noise),
  `PILOT_YIELD_FLOOR=0.85` (adjusted fork yield within ≤15% of oneshot), `PILOT_COST_CEIL=0.70` (the
  mechanical caching win shows in cost/chunk). The report prints two honest caveats: the warm-base call
  writes no marker (fork cost/chunk optimistic on small ticks) and a fork error writes no marker
  (reliability reads from run summaries). First read (n=9 fork, 2026-07-06): all chunks land in one score
  band so stratification is not yet correcting anything, within-band fork yield trails oneshot (1.78 vs
  2.98) but far below the count floor — verdict correctly "keep gathering."
- Deferred: flipping `--warm-base` on by default (awaits the pilot's rule tripping); a per-source-kind
  warm base beyond the current per-mode split; the Batch API binding (API-key surface, ADR-0004's
  backlog); folding the warm base's own cost into the budget gate + its own marker (bounded per-tick,
  measured separately for now, so the pilot's cost/chunk is currently fork-optimistic).
