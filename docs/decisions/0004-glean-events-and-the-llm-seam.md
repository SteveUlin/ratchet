# 0004 — glean: the LLM seam, the event format, and the append-only event store

- Status: accepted
- Date: 2026-06-26
- Supersedes: 0001 §5 (re-establishes the event format, deferred since V0; spans index the **cleaned**
  blob per ADR-0003, evidence is stored as spans not text, and the id formula is pinned)
- Superseded by: —

Code (`ratchet/glean.py`) is the source of truth for the formats; this records the *why*. Validated
against a real chunkset via the live `claude` CLI and hardened by an adversarial review.

## Context

`glean` is the first LLM stage and the only one:

```
tap → raw blob → weave → cleaned blob → chunk → chunkset → glean → events
                                                        (filter + extract, LLM)
```

It consumes a **materialized chunkset** (bounded, speaker-tagged byte-offset pointers into an
immutable cleaned blob — ADR-0003) and produces **events**: durable, reusable learnings a future
session would be better for knowing — not just corrections and preferences, but decisions and their
rationale, surprises, environment quirks, and reusable facts. Events were deferred out of V0 (the
model/store were removed in ADR-0001); glean re-establishes them. glean **consumes only** — no
re-fetch, no re-render; it resolves a chunk by slicing the cleaned blob, exactly as `chunk.resolve`.

## Decisions

### The LLM seam is injected; the core is pure and tested offline

The only impure step is the model call, so it is injected as a `Completer = (system, user) ->
Completion`. Everything deterministic — prompt build, output parse, **quote verification**, span
math, cost, idempotency — lives in the core and is tested with a fake `Completer` (no network, no
key). The shipped default shells out to the **authed `claude` CLI** (sulin's call: zero key setup,
uses his existing subscription auth). Print mode + a replaced system prompt (no coding-agent prompt)
+ one turn + no tools + a JSON envelope that reports `total_cost_usd` directly — so cost is
authoritative without a price table (a small table is a fallback for any Completer that returns
tokens but no cost). Default model is **`haiku`** (cost-aware; `--model sonnet` for sharper
judgment). The seam keeps the urllib→Anthropic-API binding a drop-in alternative.

**CLI cost characteristic (accepted).** Each headless invocation carries ~17K tokens of Claude Code
context (tool defs, auto-discovered context) as a cached prefix. Running from a CLAUDE.md-free cwd
(the data root) with a byte-stable replaced system prompt keeps that prefix identical across calls,
so calls after the first **read** it at ~0.1× instead of re-writing it (~$0.005/call warm, on
haiku). We do **not** pass `--disallowed-tools` to shrink the prefix: the CLI hard-exits on an
unknown tool name, so naming the tool set would make glean break on any CLI tool-set change — a
catastrophic failure mode for a ~$0.01-per-cold-run saving. `--max-turns 1` keeps the call
single-turn instead. The processed ledger (pay once per chunkset, ever) and `--max-usd` are the
guardrails.

### Filter cheaply, then extract — one combined call

Two facets, cheapest-first. A **free structural pre-filter** drops excerpts that *cannot* carry a
durable learning (too small, or no `[user]`/`[assistant]` turn — pure tool noise or a lone compact
marker), sparing them an API call. Surviving chunks get **one** combined filter-and-extract call
where an empty `{"events":[]}` *is* the "no signal here" verdict. A separate cheap LLM filter pass
was rejected: two passes both pay input-token cost, so combining is cheaper.

### The trust anchor: a quote is trusted iff it is a substring of the chunk

The whole point. The model returns, per learning, a **verbatim quote** (the evidence), a one-sentence
**summary**, a **signal** kind, and a **confidence**. glean accepts the event only if the quote is a
real substring of the chunk's bytes; it records the quote's byte span in the cleaned blob and
re-checks that `cleaned_bytes[span]` equals the quote. A hallucinated, paraphrased, or too-short
quote dies deterministically, before any event is written. Verification is in **bytes** (matching the
chunk pointers) and scoped to the chunk's own slice, so a span is always valid and contiguous within
the cleaned blob. Trust rests on weave/chunk being deterministic and the cleaned blob immutable —
re-rendering reproduces the identical hash, so the span resolves the same forever.

### Event format — a thin pointer, never a copy (refines ADR-0001 §5)

```
{ id, cleaned_hash, evidence[].{byte_start,byte_end}, summary, signal, confidence,
  producer{stage, model, prompt_version, run_id, cost_usd}, supersedes, status }
```

The verbatim quote is **never stored** — it is `get(cleaned_hash)[span]`, recomputable forever
against the immutable blob; storing it would duplicate or risk diverging from the cleaned text
(same reasoning as a chunk pointer, ADR-0003). `summary` is the only model text kept, and it is
**untrusted** until judged. `signal` is coerced to a known kind and `confidence` clamped to [0,1] —
untrusted-field hygiene. `evidence` is a list (one span in V0) to leave room for multi-span events.

- **id = `sha256(cleaned_hash:byte_start:byte_end)[:12]`** of the first span. Span-derived, so two
  runs dedup on the same evidence regardless of model, prompt, or run. **Dedup is a consumer
  concern** (ADR-0001 §6); producers are dumb appenders. A re-extraction that picks slightly
  different quote boundaries is a different id — acceptable; the judge dedups semantically later.

### The event store is a separate append-only log, NOT blobstore blobs

A chunkset→chunks step is a **deterministic** function of the cleaned blob, which is exactly why
`chunk` content-addresses it into the store. An LLM extraction is **not**: the same `derived_from`
yields different bytes across runs, models, and prompt versions — so content-addressing it is
semantically wrong, and a per-run shard naturally spans many cleaned blobs (no single
`derived_from`). The principled line: **the blobstore holds deterministic, content-addressed
content; the event store holds non-deterministic, provenance-stamped, append-only producer output**
(exactly ADR-0001 §1/§4, which this implements).

- Events go to `events/glean-<run_id>.jsonl`, written `.partial` then renamed on clean exit; a
  reader globs `glean-*.jsonl` and merges (a `.partial` from a crashed run is invisible). Single
  writer per file ⇒ plain append is safe.
- **Auditability is intact without making events blobs.** Every event embeds `cleaned_hash`, and
  `get_meta(cleaned_hash).derived_from` walks to the raw blob, thence the datastore — every hop
  content-addressed: **event → byte span in cleaned blob → derived_from → raw blob → datastore**.
  Auditing an event = resolve its span against the immutable cleaned blob.

### Idempotency and crash-safety = a processed ledger + commit-marker ordering

A processed ledger (`state/glean-processed-<run_id>.jsonl`, per-run shards, glob+merge) keyed by
**(chunkset_hash, prompt_version, model)** records every processed chunkset — *including those that
yielded zero events*, so no-signal chunksets are not re-tried forever. A re-run skips a done key with
zero LLM calls; bumping `PROMPT_VERSION` (an improved prompt) or `--model` changes the key and
re-extracts over the **same frozen chunks** — no re-fetch, no re-render (the same split ADR-0001 §3 /
ADR-0003 buy elsewhere). Within a run, **events are written durable first, then the processed marker
(the commit)** — the blobstore's content-first/meta-last pattern. A crash leaves events without a
marker ⇒ reprocessed next run; a reprocessed event re-appears under the same id, so **consumers dedup
by id** (ADR-0001 §6) and the duplicate is harmless. `load_events` is a faithful raw reader and does
*not* dedup — a re-extraction under a new prompt yields the same id with a better summary, which
deduping at the reader would hide. A budget stop (`--max-usd`) is a clean exit: the chunksets done so
far are legitimately committed.

### Cost accounting

The CLI envelope's `total_cost_usd` is the authoritative per-call cost; the processed ledger records
the per-chunkset total (so summing the ledger gives exact run cost, including no-signal calls). An
event's `producer.cost_usd` is the call's cost amortized over its accepted events — provenance, not
the authoritative total.

## Out of scope (next blocks)

The judge / usefulness scoring, the proposal queue, review/apply, and any CLAUDE.md / skill editing.
glean only produces verifiable events.

## Known limits (deferred, accepted)

- **Prompt-cache spend on the CLI binding.** The ~17K-token Claude Code prefix is unavoidable via the
  CLI (replacing it needs `--bare`, which forces API-key auth sulin doesn't use). Mitigated by a
  stable cwd + the cache TTL; the urllib→API binding would avoid it entirely. (ADR-0005 supersedes
  the tool-handling: an empty `--allowedTools` allowlist, not `--disallowed-tools`.)
- **Evidence is single-span.** The schema allows multiple spans, but the prompt asks for one quote
  per learning; a learning whose evidence is split across turns is recorded as its strongest span.
- **No semantic dedup.** Producers dedup only by exact span id; two events quoting overlapping-but-
  not-identical spans of the same learning both persist (the judge dedups semantically later).
- **A quote inside truncated/elided tool output cannot be cited** — the extractor only ever sees, and
  quotes, the rendered cleaned text (ADR-0003).
- **Re-extraction churn.** A bumped prompt/model re-extracts over the same chunks and appends new
  shards; old events are not retracted (status/`supersedes` exist for a future reconciler).
