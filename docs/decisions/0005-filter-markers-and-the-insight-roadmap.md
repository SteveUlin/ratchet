# 0005 — glean's filter: scored markers, structural pre-gates, and the insight roadmap

- Status: accepted
- Date: 2026-06-26
- Supersedes: 0004 §"Event format" (the single `signal` becomes scored multi-label `markers`), 0004
  §"CLI cost characteristic" (the binding now disables tools via an empty allowlist + is resilient)
- Superseded by: —

Code (`ratchet/glean.py`) is the source of truth for the formats; this records the *why*. The design
is grounded in a prior-art research pass (see References) and hardened by an adversarial review.

## Context

ADR-0004 shipped glean as one combined filter-and-extract call producing `event`s (a trusted byte
span + an untrusted summary + a single `signal` kind). The next step is an explicit **filter**: a set
of cheap classifiers — "markers" — that say *why* a learning is worth surfacing (was Claude
surprised? an insightful realization? a research result?), so a future synthesis layer can route and
cluster. This ADR records the filter design, the resolved combined-vs-cascade-vs-fork question, and
what is deferred.

## Decisions

### Markers replace the single `signal` — a multi-label, scored filter classification

An event now carries `markers`: a score in [0,1] per kind, multi-label (a learning can be both a
surprise and a research result). V0 kinds:

- **surprise** — an expectation broke: a command or test failed, an assumption was wrong, or the user
  corrected/redirected the work. (Bayesian surprise is *belief-change*, not rarity — see "deferred".)
- **insight** — a non-obvious realization about the person, project, or how to do something well.
- **research** — a researched finding or external fact established during the session.

Markers **classify, they do not gate.** The gate stays "is this a durable learning at all", so a
plain preference (all markers low) still comes through — because for a learning miner the costly
error is the invisible **false negative**, a durable learning dropped forever. The filter is tuned
for **recall**; precision is the job of the downstream judge and the human review gate.

### Cheap, no-LLM structural pre-gates feed the model as priors (never as gates)

`structural_cues` byte-scans a chunk for high-recall failure shapes (`[error]`, `Traceback (most
recent call last)`, `AssertionError`, `npm ERR!`, `… failed`, non-zero exit, …) and for a short
corrective user turn (repair/negation cues). Detected cues are passed to the model as text hints. A
regex marker alone is **never** emitted — an `Error:` may be quoted in docs, a user "no" may answer a
question — so the cue raises a prior and the LLM adjudicates. They never drop a chunk (that would cost
recall); the only skip remains the conservative `has_signal_potential`.

### Combined (one call) NOW; the 2-stage cascade and per-marker forks are the deferred upgrade

The research compared three shapes for a cheap (Haiku-tier) extractor: (a) one combined call →
markers + insights; (b) a 2-stage cascade → a cheap marker FILTER call, then INSIGHT extraction only
on survivors; (c) N forked specialized classifier calls per chunk. Its headline favored (b) and
deferred (c). We ship **(a)**, deliberately, for reasons the research's own cost model supports once
applied to *this* binding:

- **(a)'s only cited flaw — "pays full generative output on every chunk" — does not hold here.** An
  extraction prompt returns `{"events":[]}` for a no-signal chunk: the expensive generative output
  (span selection + summary) is produced only when there *is* signal. So (a) already captures the
  cascade's cost win (cheap output on no-signal chunks) in one call.
- **On the CLI binding (sulin's choice), the cascade's cost win evaporates.** Its advantage needs the
  chunk cached across the filter and extract calls (re-read multiplier `c`: 1.0→0.10). The two stages
  use *different* system prompts, and the CLI gives no breakpoint control to cache the chunk across
  them — so survivors re-pay the full chunk input (`c≈1.0`), which *outweighs* the tiny output saved,
  since ratchet's chunks are large and its outputs tiny (one span + one sentence). The cascade's
  closed form (`two-stage wins iff s·(c·C+O) < O'`) only favors (b) when `c=0.10`, i.e. on the raw
  **API** binding with explicit `cache_control`.
- **Forking (c) is parity, not a quality jump, for 4 markers.** The decomposition evidence (cheap
  models suffer "task interference"; a generative multi-label call can't emit independent per-marker
  confidences) is real, but with only three/four markers we sit in the "combined is fine" zone, and
  Dichotomic Prompting's honest result is efficiency parity. Forking buys clean per-marker
  confidences and independent thresholds — operational value to **earn with a gold set**, not pay for
  speculatively.

So: combined now; **the 2-stage cascade is the right move *when we switch the binding to the raw
Anthropic API*** (caching makes it cheap and the focused filter sharpens recall); **fork a single
marker only when a labeled gold set shows it confuses** with a neighbor (prime suspects: insight ↔
research ↔ surprise). Both are PROMPT_VERSION-bump-cheap re-extractions over the same frozen chunks.

### The CLI binding disables tools (robustly) and is resilient

A transcript excerpt is full of tool calls, and with tools available the model sometimes calls one
itself, burning the single `--max-turns 1` turn → `error_max_turns`, exit 1, no result. The binding
now passes an **empty `--allowedTools`** allowlist: the model literally cannot call a tool, disabled
*without naming any tool* (naming them in `--disallowed-tools` breaks glean if the CLI renames a tool,
and doesn't even stop the max-turns trip). The completer parses the JSON envelope **even on a non-zero
exit** (it carries the real error/subtype) and **retries with backoff** (transient overload). A call
that still fails raises `CompleterError`; `extract_chunkset` isolates **any** exception from the
injected completer **per-chunk** — one bad call never aborts a run (tap's per-file philosophy; only
ratchet's own parse/verify stays unguarded, so its bugs surface). Errored chunks are counted and
reported. The LLM seam — `Completion`, the `Completer` contract, the CLI binding — lives in its own
module `ratchet/completer.py`, so a second binding (urllib → the API) is a sibling file, not a
function wedged into the extractor.

## Roadmap (deferred, in priority order — from the research)

1. **`relevance-to-known-how (±)` marker via NLI** against retrieved current CLAUDE.md/skill lines
   (`contradict → −` high salience, `neutral → 0`, `entail → +`/drop). This *is* Bayesian surprise
   (belief-change) and stops re-mining what the developer already wrote — the first post-V1 addition,
   deferred only because it needs retrieval the other markers don't.
2. **Quote-⊨-summary entailment** — a cheap check that the trusted quote entails the untrusted
   summary; demote paraphrase drift or fall back to a quote-derived template. Upgrades the summary
   from "untrusted" to "evidence-supported".
3. **Importance score (1–10) per event** and **failure→recovery contrast pairs** (pair a surprise
   chunk with its resolution; extract the delta — the most CLAUDE.md-shaped lesson).
4. **A hand-labeled gold set** to pick marker thresholds and measure per-marker F1/calibration —
   combined-vs-forked is an empirical A/B on *our* model, not a settled prior. Plus a periodic audit
   of REJECTED chunks to estimate the false-negative rate the pipeline can't see from inside.
5. **`dream`** (synthesis) and **`takeaways queue` → `review` → `concept files` → `generate`** — the
   downstream blocks; see the project memory for the full research-backed design and naming.

## Known limits (deferred, accepted)

- **A *partially* errored chunkset is marked done** (some chunks succeeded, some failed after
  retries) — its failed chunks' events are lost until a PROMPT_VERSION/model bump re-keys it (tap's
  "counted, not retried" precedent). A chunkset where *every* call failed (zero successes — a
  transient outage or a TTL-reclaimed cleaned blob) writes no marker and is retried next run. Any
  exception from the injected completer is isolated per-chunk; only ratchet's own logic (parse/verify)
  is left unguarded so its bugs surface.
- **Marker confidences are not independently calibrated** (one generative call; probability mass
  competes). Tolerable while markers are advisory; revisit (fork / self-consistency) when a marker
  becomes load-bearing for routing.
- **No cross-stage / cross-fork chunk caching on the CLI** — the reason combined wins now and the
  cascade waits for the API binding.

## References

FrugalGPT — model cascades (arXiv:2305.05176); cascade economics (arXiv:2507.03834); IFScale
instruction-density decay (arXiv:2507.11538); multi-label "spiky softmax" (arXiv:2505.17510);
Dichotomic Prompting (arXiv:2511.03830); multi-problem prompting (arXiv:2406.10786); Bayesian surprise
(Itti & Baldi, PMC2860069); Reflexion (arXiv:2303.11366). Downstream (dream/queue): Generative Agents
reflection (arXiv:2304.03442), Mem0, Letta sleep-time compute (arXiv:2504.13171), QualIT
(arXiv:2409.15626), Traceable Text (arXiv:2409.13099). Full synthesis in project memory.
