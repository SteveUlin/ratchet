# 0010 — dream v2: incremental working-set consolidation (a proposed redesign)

- Status: **proposed** (a grounded plan; not yet built)
- Date: 2026-06-27
- Supersedes (when built): 0006's global-re-cluster dream. Builds on 0007 (blobs + decisions), 0009 (Block).
- Superseded by: —

This is a DESIGN ADR — the v2 model + decisions, grounded in a prior-art pass (see References), to be
implemented behind green tests after the ADR-0009 progress work lands.

## Context

dream v1 clusters the **whole event pile globally** every run, then one LLM call per cluster. Three
failures, all observed:
- **Under-merges badly.** Lexical TF-IDF over short verbatim quotes is too sparse to group related
  events; a real run gave *106 events → 81 clusters* (~1 takeaway per event) for ~$3 of wasted Sonnet.
- **Churns.** Adding events shifts cluster signatures → already-synthesized clusters re-synthesize.
- **Unbounded + not iterative.** It re-clusters everything forever and doesn't "fail in the middle"
  cleanly the way the per-chunk Block does.

sulin's reframe: a bounded **working set** of un-consolidated events; iterate — *incorporate events →
ADD/UPDATE a takeaway → mark events consolidated → forget the stale stragglers → repeat* — and gate
what reaches review on **corroboration over time**, not on a single cluster firing. The prior art has
converged on exactly this shape; the novelty ratchet adds is *forgetting the episode from the working
set while keeping it re-resolvable through the trust chain* — which none of the surveyed systems do
(they either never forget, or forget by deleting/mutating the trace).

## Decisions

### 1. No clustering-first — per-event ADD / UPDATE / NOOP routing (the core move)

Drop global clustering. For each un-consolidated event: retrieve a **bounded top-K** of the most
similar existing takeaways, and let the LLM decide — **ADD** (seed a new takeaway), **UPDATE**
(strengthen an existing one), or **NOOP** (noise). This is the convergent design of Mem0, A-MEM, and
GraphRAG-incremental, and it *sidesteps the similarity-metric problem* that broke v1: the LLM judges
relatedness against a few candidates instead of a brittle global cosine threshold. With no embeddings
(by constraint), the top-K retrieval is a **recall-first lexical pre-filter** (high recall, low
precision is fine — the LLM supplies precision), not the final grouper.

### 2. The maturity gate — corroboration before review (sulin)

A takeaway is **incubating** until its support crosses a bar, then **graduates** to `/ratchet-review`.
**Weight the bar by DISTINCT SESSIONS, not raw event count** — three events in one session is a
possible one-off; three across three sessions is a recurring pattern (independent samples → durable).
This is the load-bearing reframe: in v1 clustering had to be good because every cluster fired a
takeaway; in v2 **corroboration-over-time is the primary filter and clustering is just a hint**, so
the lexical-similarity ceiling stops mattering — recurrence does the filtering clustering failed at.
(Grounded: Generative Agents' accumulated-salience trigger; NELL-style candidate→confirmed promotion;
statistical replication.)

### 3. UPDATE = supersession; DELETE never touches ground truth

An UPDATE writes a **new takeaway blob** (refined "why" + the added cited event) that **supersedes**
the prior via an append-only decision — never an in-place edit (ratchet's immutable model already, and
the antidote to A-MEM/SSGM's documented *cumulative semantic drift* from in-place rewrites). Re-verify
the cited verbatim spans on **every** update so a takeaway's "why" never floats free of its evidence.

### 4. Additive sufficient-statistics on each takeaway (BIRCH)

Each takeaway carries running summary stats — `support_count`, `distinct_sessions`, `last_seen`,
aggregated markers — maintained as **closed operations over the summary**, so UPDATE/MERGE/the maturity
gate work *without the raw events*. This is what makes "drop consolidated events but keep updating"
possible (BIRCH's cluster-feature trick).

### 5. Consolidate then mark — events leave the working set

An event incorporated (ADD/UPDATE) gets a **`consolidated` decision** → it leaves the working set (the
takeaway captured it). The working set = un-consolidated, non-stale events — derived, bounded.

### 6. Forget stale — conservatively (topic-locality caveat)

An event resident τ incorporation-cycles without ever consolidating gets a **`stale` decision** → it
leaves the working set (Denning working-set timeout; MemoryBank reinforcement-not-just-age). **But
forget conservatively:** a Claude-Code stream has locality by *topic*, not arrival time, so a
sparse-but-recurring lesson must survive — gate forgetting on (τ cycles passed) ∧ (low corroboration),
never on age alone (DenStream/Denning cautions). Stale is a decision, not a deletion — reversible.

### 7. Trigger on pressure/salience, not a fixed N

Run an incorporate cycle when the working set crosses a **watermark** (MemGPT) or a **high-salience**
event arrives (Generative Agents importance; DenStream density), not "every N events." Batch the cycle
to bound LLM calls and to let counter-evidence accumulate (Memory-as-Metabolism's anti-self-sealing).

### 8. Process by salience — the working set is a PRIORITY QUEUE (sulin)

Order matters twice over: (a) under `--max-usd` you want the highest-*value* work done first, and (b)
incremental routing/clustering is **order-dependent** — Leader clustering seeds better and more stably
when fed the most distinctive items first. So process by **salience, not arrival**:
- **dream v2** — the working set is a priority queue keyed on event salience (the glean markers
  surprise/insight/research × confidence, plus recurrence). The highest-salience un-consolidated event
  is incorporated next: it seeds takeaways and corroborates the maturity gate on the *strongest*
  evidence first; low-salience stragglers wait and are the first to be forgotten. [Generative Agents
  importance-first; EM-LLM surprise-first]
- **glean too** — the same lever one stage up: prioritize chunks by likely durable yield (the cheap
  structural cues — a failure/redirect/insight marker present — plus density), so a budget-capped pass
  gleans the *best* chunks first instead of burning the cap on trivia.

Mechanically this is one optional Block knob, `priority(item) -> float`: the driver sorts its eager
enumeration by it (default: stable order), so it composes with idempotency (a re-run re-prioritizes the
not-done remainder) and `--max-usd` (highest-priority-first under the ceiling).

### 9. Compaction is log-compaction with a checkable GC predicate

dream v2 is **log compaction with an LLM as the merge function** (takeaway = key, event = superseded
value). GC a consolidated event's blob + the *rebuildable* derived blobs (cleaned/chunkset) only when
an **explicit, checkable predicate** holds (CRDT causal-stability discipline, Kafka grace-window):

> `(consolidated ∨ stale)  ∧  (every citing takeaway's span RE-RESOLVES, verified)  ∧  (grace window passed)`

Never GC in the same step that consolidates (grace window). **Verify re-resolution before dropping** —
don't trust it (Bazel BwoB ships "success" while the bytes are silently gone). Raw transcripts are
**never** GC'd.

### 10. Soft revision, not a hard freeze

A settled takeaway is left alone by default (avoid LLM write-amplification — the LSM caution), but
**can** be revised when new evidence is strong enough (a `contradicts` → the belief-change path), a
soft history penalty rather than an immutable freeze (Evolutionary Clustering: a hard freeze
over-smooths and hides real drift).

## Trust-chain reconciliation (the load-bearing invariant)

"Drop the middle, re-prove on demand" is safe here *only because the pipeline is deterministic up to
glean* and the trust chain re-resolves:

| artifact | kept? | why |
|---|---|---|
| raw transcript | **forever** | ground truth; the ultimate fallback (re-glean is possible, non-deterministic) |
| takeaway / concept / decision | **forever** | the distilled knowledge + the audit log |
| cleaned / chunkset | **GC-eligible** | deterministically rebuildable from raw (weave/chunk) |
| **event** | GC-eligible **once consolidated** | non-deterministic (can't regenerate) — but the takeaway captured it + carries the span |

**Robust anchoring (W3C Web Annotation):** a takeaway's evidence stores **both** the byte span *and*
the verbatim quote + a little context. Re-resolution tries the offset against the (possibly
regenerated) cleaned blob; on a mismatch it falls back to substring-matching the stored quote. Verify
the rebuild content-addresses to what was cited (Nix CA-derivations) before any GC. Keep **retraction**
(supersede; history preserved) distinct from **excision** (irrevocable secret-removal; not a
correctness tool) — Datomic. The standing risk the whole field flags: **consolidation error is
cumulative and irreversible once evidence is dropped** — so the span+quote anchoring, verify-before-GC,
and never-drop-raw are non-negotiable, not nice-to-haves.

## Keep from v1

The trust chain (extended: span **+** verbatim quote); takeaways as immutable blobs with supersession
and a derived "current" view; decision blobs for all state; the human review gate (now fed by the
maturity gate, not by clustering).

## Open questions / risks

- **Top-K recall without embeddings** — is a lexical recall pre-filter good enough to surface the right
  candidates for the LLM to route against? (The one place the no-embeddings constraint still bites.)
- **Threshold tuning** — the maturity bar, the consolidation watermark, the forget-τ are magic numbers
  the prior art warns don't transfer; need a small gold set / observed-locality sizing.
- **Write-amplification** — re-synthesizing a takeaway per touching event is the LLM analog of LSM
  write-amp; the batch + soft-revision gate bounds it, but watch "compaction debt."
- **Verify-before-GC cost** and the silent-mis-anchor risk (fuzzy re-anchoring can match a *different*
  identical-looking quote — W3C/RARR caution).

## References

Prior-art pass (2026-06-27, workflow `wf_48b8891d-86f`, 6 lenses; synthesis hand-finished after the
agent hit a session limit). Strongest sources: Generative Agents (2304.03442); Mem0 (2504.19413); A-MEM
(2502.12110) + SSGM drift critique; Zep/Graphiti (2501.13956); EM-LLM (2407.09450); MemoryBank
(2305.10250); Letta sleep-time (2504.13171); Sequential Leader (Hartigan), BIRCH, CluStream, DenStream,
Evolutionary Clustering; Kafka log compaction; LSM/RocksDB compaction; event-sourcing snapshots;
CRDT causal stability; Kleppmann DDIA (derived-data); Bazel Build-without-the-Bytes; Nix CA-derivations;
W3C Web Annotation robust anchoring; Datomic retraction-vs-excision; RARR (2210.08726).
