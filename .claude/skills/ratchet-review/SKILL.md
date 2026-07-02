---
name: ratchet-review
description: Review ratchet's pending claims (né takeaways) and queued structural-op proposals — the human review gate (two tiers). Invoke when sulin wants to triage what ratchet has learned from his sessions, or accept/reject the gardener's proposed concept restructurings. You are an active faithfulness-checker; sulin makes every call.
---

# ratchet-review — the human gate

ratchet mines sulin's Claude Code sessions into **claims** (a lesson that recurred, with verified evidence and — once matured — a synthesized "why"). This skill is the one hard gate, and it has **two tiers**:

- **Tier 1 — claims → concepts** (most of this skill): a claim sulin **accepts** becomes a **concept** (ratchet's curated knowledge), which feeds back into the system and is later projected into CLAUDE.md/skills.
- **Tier 2 — structural-op proposals** (see the section at the end): the *gardener* proposes restructurings of the concept layer itself — merge two concepts that say the same thing, split one that conflates two lessons, retire a stale one. The safe, low-stakes ones auto-applied; the high-stakes ones are **queued for you**.

Both tiers share one shape: **approving a false belief is costly**, so your job is to make sulin's yes/no *informed*, never to decide for him.

The trust chain guarantees each evidence quote is *copied verbatim from an immutable blob* — glean selects transcript LINES and the system copies their bytes (the model never retypes them, so a quote cannot be hallucinated). The quote is never in question. What **is** untrusted is everything an LLM assembled around the quotes: the `why` prose, a proposal's `rationale`, and — new in the claim era — the *merges* that built the claim. resolve's acceptance layer is one Haiku call deciding "same lesson as an existing claim, or new?" (ADR-0028); every merge it makes persists a **match key**, and the review card renders an **AUDIT CARD**: each corroboration's verified quote beside what the model saw. Checking all of that is your job.

All commands below run from anywhere — `nix run ~/ratchet#review -- …` needs no cwd.

## The loop

Fetch the queue:

```
nix run ~/ratchet#review -- --pending --json
```

Each claim carries its **maturity standing**: `entrenchment` (recency-weighted corroboration score), the `bar` it cleared, `mature`, and a plain `rationale`. The bar is sulin's knob, not a fixed rule — pass `--maturity <N>` to lower it (review more) or raise it (only the most-corroborated). A claim earns the queue by *recurring* across distinct, recent sessions; that is evidence of durability, not a quota.

If the queue is empty, **don't just say "nothing to review."** Run `nix run ~/ratchet#review -- --incubating` to show what's accruing and *why* it's below the bar (each with its score), check `nix run ~/ratchet#review -- --contested` for near-bar claims a contradicts edge is holding down (one wrong LLM CONTRADICTS verdict must not silently suppress a good claim — surface these for sulin's judgment), and offer sulin the choice to lower `--maturity` for this sitting if something looks durable enough already. Otherwise, take the claims **one at a time**, and for each:

### 1. Assess before you present (this decides fast vs deep)

Two checks, in order — the merge, then the prose:

**a. The audit card — does this set of quotes teach ONE lesson?** Any claim whose maturity rests on an LLM merge (on the shipped cascade, every merged claim) carries an `audit` card: per corroboration, the verified quote plus its match key (`stmt_sim`, candidates shown, model) — exactly what the model saw when it accepted the merge, with a ⚠ on disjoint-subject evidence. This is the v2-failure detector: v2 once fused Zig, JAX, and NumPy lessons into one "mature" takeaway, and only a human caught it. Read the quotes side by side. If one doesn't belong, the fix is surgical, not wholesale:

```
nix run ~/ratchet#review -- --reject-merge <edge-id> --reason "…"
```

— the edge id is right on the card. One compound decision: retracts the edge (support recomputes; the claim may un-mature), reopens the event (it re-resolves, likely into its own claim), and pair-blocks permanently (never re-suggested). Don't reject a whole claim because one merged quote is foreign — excise the quote.

**b. The `why` against the quotes** — when prose exists. A **why-pending** claim (badge: `why_pending`, title = its seed event's summary) is reviewable as-is — the title plus verified quotes is often enough for a clear call. If sulin wants the synthesized prose first:

```
nix run ~/ratchet#synthesize -- --claim <id>
```

then re-fetch. Never treat a missing `why` as a defect — prose is deferred by design (review never waits on Sonnet's cadence). A `why-stale` flag means the evidence diverged since the prose was written — re-synthesize or judge on the quotes.

Then decide which way to go:

- **Solid → fast path.** The quotes teach one lesson, the `why` (if present) follows from them, support is decent (multiple `events`/`sessions`), nothing high-stakes. Present it compactly with the verified quotes and say it looks well-supported — sulin can just approve.
- **Risky → deep path. Escalate *yourself*, before asking.** Trigger on any of:
  - **suspect merge** — audit-card quotes about different things, a ⚠ disjoint-subject line, or a merge at low `stmt_sim` where only the model's judgment connects them,
  - **thin support** — 1 event / 1 session but a confident, broad claim,
  - **why-drift** — the `why` asserts more than the quotes actually support,
  - **high stakes** — `relation.kind` is `contradicts`, or the claim is `contested`,
  - or sulin asks "why do you believe this?".

  On the deep path, pull the surrounding transcript with `nix run ~/ratchet#review -- --context <claim_id>` (a wider window around each quote, audit card included), read what actually happened, and **show sulin the discrepancy** before asking — e.g. "the why claims he *always* prefers X, but the only evidence is one session under deadline; here's that moment." That is the point: a research-and-learning moment, not a rubber stamp.

### 2. Present with zero-click evidence

Always show the verbatim quotes inline, marked verified (✓) — sulin judges *interpretation*, never whether a quote is real. Show the title, the why (or the why-pending badge), support (events/sessions), the corroboration story (when and where the lesson recurred), the relation, the scope (`cross-cutting` claims span subjects — that's what a global CLAUDE.md is *for*, not a defect), the maturity `rationale` (score vs bar), the audit card when present, and your own one-line faithfulness read.

**Merge suggestions** ride the cards (derived at render time, stored nowhere, TTL'd): two claims' quotes side by side, no similarity score — the human judges words, not numbers. Present them as a question, and record sulin's answer:

- **confirm** (same lesson — fold the loser into the winner): `nix run ~/ratchet#review -- --merge-claims <loser> <winner> --reason "…"`
- **dismiss** (distinct lessons — never asked again): `nix run ~/ratchet#review -- --reject-merge <loserId>,<winnerId> --reason "…"`

### 3. Record sulin's call (pass your assessment as provenance)

- **accept:** `nix run ~/ratchet#review -- --accept <claim_id> --assessment "<your faithfulness check>"` (add `--note "..."` for anything else worth recording).
- **edit** (sulin corrects the title/why first — the highest-value signal): `--accept <id> --edit-title "..." --edit-why "..." --assessment "..."`
- **reject:** `--reject <id> --reason "<sulin's reason>"`
- **snooze** (defer until a date — required): `--snooze <id> --until 2026-07-15 --reason "..."`. ("Wait for more evidence" is *not* a snooze — more sessions corroborate the claim on their own; just reject or skip.)
- **contradicts** — if the claim's `relation.kind` is `contradicts`, accepting it affirms the new view; **also ask sulin whether to retire the concept it contradicts** (`relation.concept_id`), and if yes run `--retire <concept_id> --reason "..."`. Two decisions, one human judgment — don't auto-retire without asking.

Accept refuses a claim whose evidence no longer resolves (a concept needs verifiable backing) — if that happens, investigate rather than forcing it. On accept, confirm the concept was written; the concept digest feeds glean, so future events arrive labeled `strengthens`/`refines`/`contradicts` instead of `new`. Then move to the next claim.

## Tier 2 — the structural-op proposal queue

Beside the claim queue is a second one: the gardener's **queued structural ops**. These don't add a belief — they *restructure* what concepts exist. Fetch them:

```
nix run ~/ratchet#review -- --proposals --json
```

If empty, say so and stop. Otherwise take them **one at a time**. Each proposal carries an `op`, its `params`, an untrusted `rationale`, a `stakes` score, and the **cited concepts** — each with its title, statement, a `valid` flag, and its **re-validated evidence** (the same verified quotes the trust chain serves the human). The op is one of: `merge` (fold concepts that say the same thing), `split` (divide one conflating two lessons), `abstract` (name a shared parent), `reparent`, `retire` (drop a stale concept), `merge_tags`/`retire_tag`.

### The check is the same `why ⊨ evidence` gate, one level up

The `rationale` is **untrusted** — exactly like a claim's `why`. Your job: **does the rationale FOLLOW from the cited concepts' evidence?** Read the verified quotes of each cited concept and decide whether the op is warranted. Flag drift and over-claim; **never auto-revise** — recommend, surface the discrepancy, let sulin call it.

Two-speed, same as tier 1:

- **Well-grounded + low-impact → fast.** The cited concepts plainly say the same thing (a `merge`), or the parent idea is real (`abstract`). Present compactly with the verified quotes and say it looks sound.
- **Thin / over-claimed → escalate yourself, before asking.** Trigger especially on:
  - a **`merge` that conflates DISTINCT lessons** — the two concepts' quotes are about different things; merging would erase a real distinction,
  - a **`retire` of a still-supported concept** — the `valid` flag is true and the evidence still backs it; the rationale calls it stale but the quotes don't agree,
  - a **`split`** whose proposed parts don't cleanly partition the evidence,
  - any rationale asserting more than the cited quotes support.

  Show sulin the discrepancy — "the rationale says c-x and c-y are the same lesson, but c-x's quote is about jj and c-y's is about nix; here are both." That's the point: protect the concept layer from a silent bad restructuring.

### Record sulin's call

- **accept** (APPLIES the op — the concept graph reorganizes): `nix run ~/ratchet#review -- --accept-proposal <proposal_id> --assessment "<your faithfulness read>"`. A `merge` invalidates the losers and unions their evidence into the winner; a `retire` drops the concept; etc. — all append-only, invalidate-don't-delete.
- **reject** (does NOT apply; and the gardener will **not** re-suggest it): `--reject-proposal <proposal_id> --reason "<sulin's reason>"`. Rejection is *remembered* — a re-gardened cluster won't re-surface the same dismissed op, so reject freely when an op is wrong.
- A `split` accept needs the per-part **evidence partition** (which quotes go to which new concept) — that's sulin's judgment, passed as `--split-parts '<json>'`; if he's unsure, reject and let the gardener re-propose, or escalate.

## Rules

- **Never decide.** Recommend, surface evidence and discrepancies, but the verdict is sulin's.
- **Don't widen scope.** Review the queue; don't go re-mine sessions or edit CLAUDE.md here.
- **Prefer the surgical verb.** A foreign quote in a good claim → `--reject-merge` the edge, not `--reject` the claim; a wrong wording → `--edit-title`/`--edit-why` on accept.
- **Capture the reasoning.** Always pass `--assessment` so an accept records *why* it was trustworthy (audited later as "verified by Claude, approved by sulin").
- Keep the queue clearable in one sitting — it's small by design (the maturity bar is the noise filter). If it's large, surface the highest-stakes first.
