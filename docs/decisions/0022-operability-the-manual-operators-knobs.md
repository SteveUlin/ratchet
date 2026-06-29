# 0022 — Operability: the manual operator's knobs (fetch-selection, processing focus, review ordering)

- Status: accepted — implemented 2026-06-28 (all 19 offline suites green, incl. the new `test_operability`)
- Date: 2026-06-28
- Supersedes: —
- Superseded by: —
- Extends: ADR-0009 (the uniform Block + per-stage CLI surface), ADR-0011/0021 (the priority POLICY seam).

Code (`tap.TapBlock` `--last`/`--since` + `_parse_since`; `dream.filter_by_topic` + `DreamBlock(topic=)`;
`glean.GleanBlock(topic=)`; `review.importance` + `pending`/`pending_proposals` ordering/`--limit`/`--topic`;
`blobstore.project_of`, the single `cleaned_hash` → project hop) is the source of truth; this records the *why*.

## Context

ratchet has **no `tick` orchestrator** — by design (ADR-0009/0021). A human drives a months-long backlog by
hand: `tap`, then `glean`, then `dream`, then `review`, each run explicitly. The pipeline already has the
*throughput* levers — `--limit` (cap items EXAMINED), `--max-usd` (budget stop), `--priority` (ordering
POLICY, ADR-0011/0021). What it lacks are the operator's *steering* levers: "pull only the recent ones",
"work just this project today", "show me the highest-leverage reviews first". Those are the knobs a person
needs to spend a finite session well against an effectively-infinite backlog. This ADR adds three.

The unifying principle is **active learning under a manual budget**: attention (the human's, and the LLM
dollars) is the scarce resource, so every knob narrows *what* the next run touches or *what order* the human
sees it — never changing the artifacts, only the slice. All three default to off, so a no-knob run is
byte-identical to before.

## Decisions

### 1. Fetch selection on the FETCHER: `tap --last N` / `--since <date>`

Selection of *which transcripts exist to ingest* is **per-source**, so it belongs to the fetcher, not the
generic Block driver. `--last N` / `--since <date>` are `TapBlock.__init__` args (like `datastore`/`project`),
threaded from `tap`'s argparse — **not** `block.run` knobs.

They act **inside `TapBlock.items()`, after the cheap (size, mtime) cursor skip** — so they select among the
files tap has *not already pulled*: `--last N` keeps the N most-recently-MODIFIED survivors (stable sort by
mtime descending, take the first N — "the last 200 I haven't already pulled"); `--since` keeps survivors with
`mtime >= cutoff`. They **compose** (`--since` narrows, then `--last` takes the newest N of those).

This is deliberately **distinct from `--limit`**, and both are useful: `--limit` caps items the driver
EXAMINES this tick (the throughput dial); `--last`/`--since` SELECT which candidates exist at all (the
steering dial). `tap --last 500` then a smaller per-tick `--limit` is a normal pairing. The default (None)
streams every un-pulled file in discover order — today's behavior, byte-identical, including an unstatable
file's inline position (an unstatable file is yielded regardless of the selectors: it has no mtime to select
on, and error-isolation must not be silently suppressed). `--since` parses ISO date *or* datetime, naive-as-
UTC (mtimes compare in UTC); a bad string fails fast at the CLI rather than silently selecting nothing.

### 2. Processing FOCUS: `dream --topic <str>` / `glean --topic <str>`

Filter the stage's `items()` to items whose **source PROJECT** contains `<str>` (case-insensitive substring),
so `dream --topic ratchet` consolidates only `ratchet`-session events and `glean --topic ratchet` extracts
only those chunks. The project is reached by the lineage hop the stages **already walk** for age/facets:
`cleaned_hash` → `derived_from` (raw) → raw `origin_ref.project` (what `tap.read_origin` stamped). That hop is
single-sourced as **`blobstore.project_of`** — a sibling of `session_of`, the one `concepts._cleaned_facets`
uses for its `repo` facet — so dream/glean/review share one spelling. It is **cheap**: one cached meta read
per item, no content slice, no LLM. An item whose project can't be resolved does **not** match (focus
NARROWS — an unknowable origin is excluded, never waved through). Default None → no filter.

`--topic` is a **project/source** substring, the cheap version. Semantic-topic focus — "everything tagged
`version-control`", by the gardener's concept TAGS (ADR-0014) rather than the source dir — is the deferred
richer version; it wants a tag→item index this first cut doesn't build.

### 3. Review a PRIORITIZED SUBSET: `review` IMPORTANCE ordering + `--limit N` + `--topic`

`pending()`/`pending_proposals()` surfaced the maturity-gated / queued items in **no particular order** —
which begs the operator's question "what do I look at first?". The answer is the active-learning one: **spend
your attention where it matters most.** So:

- `pending()` orders takeaways by **`importance` = `dream.net_sessions` × confidence**, descending. Net
  entrenchment (distinct supporting sessions minus contradicting ones — the *same* signal the maturity gate
  graduates on, ADR-0012) scaled by the takeaway's own durability `confidence`. Both already live ON the
  takeaway, so ordering reads no extra blob. A belief corroborated across more net sessions, held with more
  confidence, is the most consequential to review now; a thin or contested-but-still-mature one sinks.
- `pending_proposals()` orders by **`stakes`** descending (the 3c-ii fuzzy gradient — how much an op changes
  what concepts EXIST/ASSERT, `garden.op_stakes`). The cluster `tension` that *drove* a proposal is a
  propose-time signal not carried on the proposal blob, so `stakes` is the on-proposal leverage stand-in.
- `--limit N` returns the top-N (a one-sitting review); `--topic <str>` filters to a project (substring over
  the cited evidence — a takeaway/concept matches if ANY cited span comes from that project).

Filter + sort run on the **raw** items first, so the expensive evidence resolution (`_present`) is paid only
for the survivors the human will actually see. Ordering is **deterministic and order-stable** — equal-
importance items keep their derivation order. The richer signal (a roll-up of the seed events' salience,
ADR-0010 §8) is deferred; net×confidence is the signal already on the blob.

## Consequences

- **Good:** a human can now steer a months-long backlog by hand — pull the recent slice (`tap --last`), work
  one project end-to-end (`--topic` across glean→dream→review), and review highest-leverage-first
  (`importance` + `--limit`). Each knob is additive and defaults off, so every prior suite and the no-knob
  path are unchanged. The project hop is single-sourced (`project_of`), so the three stages can't drift on
  what "this project" means.
- **Costs / known limits:** `--topic` is a project SUBSTRING, not a semantic topic — focusing by meaning
  (concept tags) is deferred. `importance` (net×confidence) and proposal `stakes` are reasonable, untuned
  signals — like every weight in dream, they await a gold set; they are deterministic, not learned. `--last`
  ranks by file **mtime**, a slight proxy for "most recent session" (a touched-but-unchanged file is cursor-
  skipped before it ever reaches the selector, so this only bites a genuine late edit). These are the manual
  operator's knobs precisely because there is no `tick` orchestrator to apply them automatically (ADR-0009).

## References

ADR-0009 (the uniform Block + per-stage CLI surface these knobs extend; the deliberate no-orchestrator
stance). ADR-0011/0021 (the priority POLICY — the *ordering across a backlog* seam; this adds the
*selection*/*focus*/*review-order* complements the policy seam doesn't cover). ADR-0012 (net entrenchment —
the `importance` signal). ADR-0010 §8 (the salience roll-up deferred as the richer review signal). ADR-0014
(the concept tags a future semantic `--topic` would key on). ADR-0007 (`origin_ref.project` / `derived_from`,
the lineage `project_of` walks). The acceptance test `tests/test_operability.py` (the four knobs, deterministic).
