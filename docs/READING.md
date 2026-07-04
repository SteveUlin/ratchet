# Reading list — the literatures ratchet sits between

"Knowledge-graph generation" is the wrong shelf: that literature extracts entities and relations;
ratchet's nodes are lessons, and no LLM builds structure here. The right frame: **a knowledge-base
construction pipeline whose core problem is entity resolution under uncertainty, with human-gated
trust and incremental maintenance** — NELL's promotion pipeline on 1960s record-linkage machinery
over an immutable log, with Wikidata's editorial model, at a scale of one person. No published
system occupies that last clause; the 2026 memory-governance surveys name human-gated write paths
the field's open gap.

Start with the first three; each entry says which part of ratchet it explains.

## 1 · Similarity without semantics → `sig.py`

- **Mining of Massive Datasets**, Leskovec/Rajaraman/Ullman — **ch. 3 "Finding Similar Items"**
  (free: mmds.org). Shingling, Jaccard, MinHash, LSH banding. Literally the math in `sig.py`;
  after this chapter every threshold in the band report is obvious. *The single highest-value read.*

## 2 · Entity resolution / record linkage → `resolve`

- **Splink topic guides** (moj-analytical-services.github.io/splink) — working practice: blocking
  keys, comparison vectors, the clerical-review band, cluster-quality metrics. resolve's cascade
  is this, with an LLM in the clerical seat.
- **Fellegi & Sunter, "A Theory for Record Linkage" (JASA 1969)** — the three-verdict model
  (match / non-match / review band) proven minimal. The residue band's ancestor.
- **Peter Christen, *Data Matching*** (Springer) — the textbook, when the docs aren't enough.
- **ComEM (arXiv 2405.16884)** — measured: binary pairwise yes/no is the most over-merge-prone
  LLM matching framing; select-from-candidates-with-none beats it. Why the residue prompt is
  shaped the way it is.

## 3 · Knowledge-base construction & trust → maturity, corroboration, the gate

- **NELL** — Carlson et al., "Toward an Architecture for Never-Ending Language Learning"
  (AAAI 2010). Candidate-vs-confirmed beliefs, corroboration-gated promotion — and the canonical
  drift failure ("cookies are baked goods"), which is dream v2's disease at planetary scale.
  Their missing demotion path is ratchet's WEAKEN (ADR-0012).
- **Knowledge Vault** — Dong et al. (KDD 2014). Provenance-weighted, Platt-calibrated confidence;
  sqrt-diminishing corroboration returns (our backlog item).
- **Truth discovery survey** (arXiv 1505.02463) — source reliability and independence estimated
  jointly; the formal justification for counting *distinct sessions*, not mentions.

## 4 · Agent memory, the current wave → the design's siblings and cautionary tales

- **Generative Agents** — Park et al. (2023). Importance-triggered reflection with
  citations-as-provenance; the consolidation loop everyone copies.
- **MemGPT** — Packer et al. (2023) — memory tiers; plus Letta's **sleep-time compute**
  (arXiv 2504.13171) — offline consolidation economics, and its prompt-level-guards-only failure.
- **Zep/Graphiti** (arXiv 2501.13956, code in `research/repos/graphiti`) — the closest production
  cousin: bi-temporal edges, invalidate-don't-delete, deterministic-first dedupe cascade with an
  abstention-default LLM residue.
- **"Useful Memories Become Faulty…"** (arXiv 2605.12978) and **ACE's context collapse**
  (arXiv 2510.04618) — over-consolidation as a measured failure mode: memory utility falling
  below no-memory baseline under continuous LLM rewriting. Why consolidation is gated, delta-only,
  and never a free rewrite. Mem0 v2 deleting its own ADD/UPDATE/DELETE pipeline (2026-04 release
  notes) is the industry-retreat datapoint.

## 5 · The substrate: immutable log, derived views, retraction → blobstore, recompute-on-read

- **Kleppmann, *Designing Data-Intensive Applications*, ch. 11** — or the talk
  **"Turning the Database Inside Out"** — events as truth, state as a derived view. The
  blobstore's worldview.
- **DBSP** — Budiu et al. (VLDB 2023; gentler intros on the Feldera blog; code in
  `research/repos/`) — retraction-correct incremental computation: the theory behind
  "support recomputes from live edges" and why `reject-merge` is mathematically clean.
  (FSRS was rejected for breaking exactly this property — see the design doc §4.)

## 6 · The human gate → review, two tiers, fatigue

- **Wikipedia: pending changes + ORES** — trust-tiered auto-accept, ML-prioritized human patrol,
  deliberately shallow review. The editorial model the two-tier gate borrows.
- **Wikidata Primary Sources tool** — per-statement approve/reject of machine-proposed facts; its
  never-drained millions-scale queue is the cautionary tale for queue noise. P1889 "different
  from" (the persisted negative verdict) is `reject-merge`'s ancestor.
- **SmartBear/Cisco code-review study** — detection collapses past ~60–90 min or ~400 units per
  sitting; **Buçinca et al. (CSCW 2021)** — people prefer and drift toward accept-all designs.
  Why "a sitting's worth" is a design constraint, not a suggestion.

## 7 · Memory strength over time → recency weighting, decay

- **ACT-R base-level activation** (Anderson) — strength from distinct rehearsals with power-law
  decay; the cognitive-science twin of recency-weighted distinct-session entrenchment.
- **FSRS** (open-spaced-repetition wiki) — instructive as the model ratchet *rejected*: its
  path-dependent state breaks retraction; its saturation property remains worth stealing someday.

## In-repo

- `research/PRIOR-ART.md` — the annotated bibliography the design workflow wrote, keyed to
  ratchet's specific decisions. Read alongside `docs/dream-v3-design-2026-07-01.md` and ADR-0028.
- `research/repos/` — graphiti, mem0, letta, cognee, HippoRAG, differential-dataflow, feldera,
  genagents and friends, cloned for spelunking.
