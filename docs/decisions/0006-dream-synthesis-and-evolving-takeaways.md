# 0006 — dream: cluster-then-synthesize, and takeaways that evolve by supersession

- Status: accepted
- Date: 2026-06-26
- Supersedes: 0005 §"the insight roadmap" (the deferred `dream` stage is now specified and built)
- Superseded by: —

Code (`ratchet/dream.py`) is the source of truth for the formats; this records the *why*. The design
is grounded in a dedicated prior-art research pass (see References) and hardened by adversarial and
aesthetic reviews.

## Context

glean produces **events**: per-chunk, cheap (Haiku), each a substring-verified verbatim quote
(trusted) + a one-line summary (untrusted) + scored markers. Events are fleeting and noisy — too raw
to review and too many to act on. `dream` is the synthesis stage that turns a growing pile of events
into a few durable, evidence-cited **takeaways** — the reviewable unit (ADR-0005 naming: event →
takeaway → concept).

    … chunk → glean → events → dream → takeaways → [human review] → concepts → generate(skills/CLAUDE.md)

Two hard requirements shaped it, both from sulin: (1) **don't trust Haiku's summary** — the trusted
atom is the quote, so synthesis must reason over quotes, not summaries; (2) **everything evolves** —
cluster names, summaries, and groupings change over time; a takeaway may need to split or merge as
more evidence arrives. Neither can be bolted on later, so both are in the v0 data model.

## Decisions

### Two stages, split by determinism (the rest of ratchet's spine)

Deterministic work is cheap and reproducible; the LLM call is neither. So dream, like the pipeline
upstream, separates them:

1. **cluster** — deterministic, stdlib, no LLM. Group events by lexical similarity over their trusted
   quotes. Cheap enough to recompute every run.
2. **synthesize** — one LLM call per cluster, with a SHARPER model (Sonnet/Opus). Write the cluster's
   "why" + a name, judge it against known concepts, cite the events it used.

This is **canopy clustering** (McCallum 2000): cheap deterministic pre-grouping shrinks the problem,
the expensive model runs only per-group. It is also the field-standard cluster-then-LLM-summarize
shape (QualIT, GraphRAG, RAPTOR).

### Sleep-time: rare, batched, sharper model

glean is the cheap online pass; dream is the rare offline one — "sleep-time compute" (Letta/MemGPT):
spend the sharp model rarely, on accumulated material. Run it on accumulated signal, not a clock
(`--min-events` — a *floor* below which dreaming is pointless, not a since-last-run change detector;
the ledger skip is what makes an unchanged pile cheap). Batching is not just economy:
*streaming consolidation is self-sealing* — scoring each event against the dominant view as it
arrives silently suppresses minority/contradicting evidence; only a batch lets counter-evidence
accumulate (Memory-as-Metabolism).

### The trust chain extends one level — and where it stops

A takeaway must **cite event ids**; each cited event's quote is already substring-verified against an
immutable blob. So the chain is `takeaway → event ids → byte spans → immutable cleaned blob`.
`build_takeaway` keeps only citations naming a real event IN THE CLUSTER (the model can cite nothing
it was not given); a takeaway with no surviving citation has no evidence and is dropped. TopicGPT
independently arrived at this exact anchor ("make the LLM cite a verbatim quote and verify it").

The chain is airtight on quote→blob bytes. It is **not** closed on the LLM-written `why`/`title`:
those are untrusted, and citing real events is necessary but not sufficient — a `why` can cite five
real events yet over-generalize past all of them (FActScore/SAFE). Verifying the `why` is entailed by
its quotes is the single biggest open gap; it is deferred (below), not solved here. dream's job is to
*produce* evidence-cited takeaways; *verifying the synthesis* is a downstream gate feeding review.

### Evolution by supersession — never mutate, append a superseding fact, fold for "now"

Takeaways are immutable and append-only. A re-run re-clusters globally; a new takeaway **supersedes**
the current takeaways it replaces (`supersedes` is a list). Grow, split (one→many), merge (many→one),
rename, and re-summarize are then **one mechanism** — there is no separate "split op".
`current_takeaways` is a **projection**: fold the log, keep the latest record per id (recency keyed on
`producer.run_id`, which sorts by creation time — *not* the shard glob order, which a same-second
counter could invert), drop any takeaway some survivor supersedes. A superseded takeaway is
*tombstoned*, not deleted (CRDT / Zep's "invalidate, don't delete").

Supersession is **coverage-conditioned**, and this is load-bearing: a prior takeaway is folded out
only when *all* its events are re-covered by takeaways **committed in the same run**, and a new
takeaway supersedes *every* eligible prior it shares an event with. The naive "supersede every prior
sharing an event" is wrong — a split where one child is **dropped** as noise (an expected adjudication
that still marks the cluster done) would fold the parent out while the dropped child's events are
covered by nothing, *permanently* (adversarial review found this). Because synthesis must complete
before the run's coverage is known, all clusters are synthesized *before* any commit; a crash mid-run
re-does the run (idempotent), trading crash-cost for coverage-correctness. Editing in place was
rejected: it would orphan the evidence spans and erase history (A-MEM / MemGPT do this and suffer
unrecoverable semantic drift). This append-only-supersede-fold model is not over-engineering — the
field is actively converging on it (Zep/Graphiti, SSGM, Memory-as-Metabolism, even Mem0's own pivot to
add-only), and it is the classic event-sourcing / Datomic / XTDB / CRDT pattern.

A takeaway's `id` is its cluster signature, so a bumped `prompt_version`/model **replaces** the
grouping's current takeaway (same id, newer run wins the fold) rather than coexisting — a model swap is
a re-derivation of the same grouping, not a second opinion to keep. `model` in the ledger key only
prevents the *skip* so the re-synthesis runs.

A contradicted takeaway is **never auto-deleted** — it routes to human review (Mem0's silent LLM
DELETE is catastrophic and unauditable).

### Persistence: the takeaway log, and the clustering recorded in the ledger

dream persists at the determinism boundary, the same rule as glean:

- **takeaways → an append-only log** (`events/dream-*.jsonl`, via `runlog`). LLM output is
  non-deterministic, so — like glean events — it is never content-addressed.
- **the clustering is NOT a stored blob.** The blobstore models *single-parent* lineage (one cleaned
  blob → one chunkset); a clustering is a *fan-in* over many events spanning many blobs, and it is
  cheap to recompute. Forcing it through `derived_from` would distort the store. The partition is
  recorded where it is actually useful: each cluster's member ids land in the **processed ledger**
  marker (`event_ids`), and each takeaway records its `member_events` + `cites`. Fully auditable and
  re-derivable without a new artifact.

### Idempotency keys on a cluster signature

The processed ledger is keyed by `(cluster_signature, prompt_version, model)`, where the signature is
the hash of a cluster's sorted member ids. An unchanged cluster is **skipped** (zero LLM calls); a
changed cluster (new signature) re-synthesizes. This is GraphRAG's "re-summarize only the communities
whose membership changed" — the cheap win that keeps a global re-run from re-paying for stable
takeaways. Bump `PROMPT_VERSION`/model to re-synthesize the same groupings with a sharper prompt.

### Clustering hardenings (leader clustering is order-dependent and chains)

Leader clustering (Hartigan) is the chosen core — O(n), no `k`, the threshold is the under-merge
knob. Two documented failure modes are guarded:

- **order dependence** → events are clustered in a fixed order, **heaviest TF-IDF mass first**, so
  the most distinctive events seed clusters (id breaks ties → reproducible signatures).
- **centroid-drift chaining** → an event joins only if similar to BOTH the drifting centroid *and*
  the cluster's fixed **seed** (a diameter cap from the origin; IR-book complete-link, Swarm).

### The concept seam and the belief-change label

dream reads the curated `concepts/` layer (empty until review exists) and labels each takeaway
`new`/`strengthens`/`refines`/`contradicts` it. This relocates the belief-change judgment off glean
(cheap, per-chunk, context-blind — the wrong tier) and onto dream (rare, sharper, with the clustered
evidence and concept context in hand). It is an **entailment-based check against the concept layer**,
*not* "Bayesian surprise" — real Bayesian surprise needs a calibrated likelihood dream does not have
(EM-LLM); the earlier framing overstated the rigor. An unknown `concept_id` coerces to `new`, so with
no concepts every takeaway is `new` and the seam is inert-but-wired until review fills it.

## Consequences

- **Good:** the reviewable unit is now evidence-cited and durable; the trust chain reaches it (and is
  re-anchored at dream's *read* boundary — a recorded span is re-validated as real in-bounds bytes
  before use, not trusted); the evolution model is coverage-correct and matches emerging best practice;
  re-runs are cheap on the unchanged majority; the whole stage is offline-testable behind the
  `Completer` seam and reuses `runlog` + `completer`. It introduces one new persistence shape — the
  `concepts/` curated layer (loose, mutable, human-owned JSON, **read-only to dream**) — deliberately
  *outside* the immutable-blob / append-only-log discipline because it is human-curated and low-volume;
  its format and mutation model belong to the future review-stage ADR.
- **Costs / known limits:** clustering is lexical (no embeddings, by constraint) — paraphrased
  cross-session insights won't cluster, and two lexically-disjoint takeaways about the *same* insight
  (sharing no event) coexist with no consolidation path; the global re-fold + global re-cluster is the
  un-optimized read model (fine at current scale); the fold is not a *pure* function of the log (the
  `why` is non-deterministic), so it lacks event-sourcing's replay determinism past the cluster
  boundary; tombstoned takeaways accumulate with no compaction yet.

### Deferred, with prior-art grounding (the research's `must_change`, triaged out of v0)

1. **Faithfulness gate on the `why`** — atomize it, check each clause is entailed by a cited quote
   (FActScore/SAFE/SummaC); FLAG to review, never auto-revise (RARR's edit-in-place changes meaning),
   never auto-gate supersession. The top gap; belongs to a downstream verify stage + the review UI
   (Traceable Text: provenance links lift reviewer error-catching 12.5%→70%).
2. **Canopy soft multi-citation** — an event near a second centroid gets cited by both takeaways,
   giving lexically-disjoint duplicates a shared-event merge path. Interacts with the partition
   invariant supersession relies on, so it needs care.
3. **Swarm chain-breaking split-check** — split a cluster at a weak internal cut, using salience
   markers as the "abundance valley"; also lets a minority, lexically-isolated-but-surprising event
   split off its own takeaway (fixes intra-run self-sealing).
4. **Term-set overlap (Jaccard/containment over rare shared tokens** — paths, error codes, fn names)
   as the leader distance, cheaper and more faithful than cosine on short verbatim quotes (FTC).
5. **Snapshot/materialize the current view + incremental clustering** so "current" isn't a global
   re-fold from zero; make global re-cluster periodic, not every-run (event-sourcing snapshots;
   Datomic index; GraphRAG incremental append).
6. **Identity is a DAG under split/merge**, not a linear chain — commit to per-event vs per-concept
   lineage for the as-of/review view (Memento needs a stable identity dream lacks).
7. **Bitemporality** — events have a valid-time (session) distinct from transaction-time (run);
   specify whether supersession orders by run or by session recency so a late-ingested old transcript
   can't mis-supersede (XTDB/Zep).
8. **Compaction / causal-stability GC** for tombstoned takeaways, and true excision of a leaked-secret
   quote (CRDT tombstone accumulation; Datomic excision is expensive).
9. **`contradicts` gates visibility** — quarantine a contradicting/poisoned takeaway out of the
   current fold until reviewed, not merely annotate it (SSGM Write Gate). Inert until concepts exist.
10. **Decontextualization** — a verbatim quote ("it", "that file") may not be self-interpretable out
    of its session; carry surrounding context, or treat the summary as a verification target (AIS).

## References

Prior-art research pass (2026-06-26, workflow `wf_6880ff03-0d1`), 6 lenses + synthesis. Strongest
sources: Generative Agents (2304.03442); Letta/MemGPT sleep-time (2504.13171); Zep/Graphiti
(2501.13956); Mem0 (2504.19413); A-MEM (2502.12110); QualIT (2409.15626); GraphRAG (2404.16130);
RAPTOR (2401.18059); TopicGPT (NAACL 2024); BERTopic (2203.05794); FActScore (2305.14251); SAFE
(2403.18802); Traceable Text (2409.13099); SSGM (2603.11768); Memory-as-Metabolism (2604.12034);
Hartigan leader clustering (1975); Canopy (McCallum, KDD 2000); Swarm (Mahé, PeerJ 2014); FTC (Beil/
Ester/Xu, KDD 2002); Datomic; XTDB; event sourcing; CRDTs; Memento RFC 7089.
