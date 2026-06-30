---
name: ratchet-review
description: Review ratchet's pending takeaways and queued structural-op proposals — the human review gate (two tiers). Invoke when sulin wants to triage what ratchet has learned from his sessions, or accept/reject the gardener's proposed concept restructurings. You are an active faithfulness-checker; sulin makes every call.
---

# ratchet-review — the human gate

ratchet mines sulin's Claude Code sessions into **takeaways** (a synthesized "why" + verified evidence). This skill is the one hard gate, and it has **two tiers**:

- **Tier 1 — takeaways → concepts** (most of this skill): a takeaway sulin **accepts** becomes a **concept** (ratchet's curated knowledge), which feeds back into the system and is later projected into CLAUDE.md/skills.
- **Tier 2 — structural-op proposals** (see the section at the end): the *gardener* proposes restructurings of the concept layer itself — merge two concepts that say the same thing, split one that conflates two lessons, retire a stale one. The safe, low-stakes ones auto-applied; the high-stakes ones are **queued for you**.

Both tiers share one shape: **approving a false belief is costly**, so your job is to make sulin's yes/no *informed*, never to decide for him.

The trust chain guarantees each evidence quote is *copied verbatim from an immutable blob* — glean selects transcript LINES and the system copies their bytes (the model never retypes them, so a quote cannot be hallucinated). The quote is never in question. What is **untrusted** is the LLM-written justification — a takeaway's `title`/`why`, or a proposal's `rationale`: it cites real events but can over-generalize past them. Checking that is your job.

## The loop

Run everything from the ratchet repo (`~/ratchet`). Fetch the queue:

```
nix run .#review -- --pending --json
```

Each takeaway carries its **maturity standing**: `entrenchment` (recency-weighted corroboration score), the `bar` it cleared, `mature`, and a plain `rationale`. The bar is sulin's knob, not a fixed rule — pass `--maturity <N>` to lower it (review more) or raise it (only the most-corroborated). A takeaway earns the queue by *recurring* across distinct, recent sessions; that is evidence of durability, not a quota.

If the queue is empty, **don't just say "nothing to review."** Run `nix run .#review -- --incubating` to show what's accruing and *why* it's below the bar (each with its score), and offer sulin the choice to lower `--maturity` for this sitting if something looks durable enough already. Otherwise, take the takeaways **one at a time**, and for each:

### 1. Assess before you present (this decides fast vs deep)

Read the `why` against the resolved `evidence` quotes and the metadata. Decide which way to go:

- **Solid → fast path.** The `why` follows from the quotes, support is decent (multiple `events`/`sessions`), nothing high-stakes. Present it compactly with the verified quotes and say it looks well-supported — sulin can just approve.
- **Risky → deep path. Escalate *yourself*, before asking.** Trigger on any of:
  - **thin support** — 1 event / 1 session but a confident, broad claim,
  - **why-drift** — the `why` asserts more than the quotes actually support,
  - **high stakes** — `relation.kind` is `contradicts`, or the takeaway is `surprise`-heavy,
  - or sulin asks "why do you believe this?".

  On the deep path, pull the surrounding transcript with `nix run .#review -- --context <takeaway_id>` (a wider window around each quote), read what actually happened, and **show sulin the discrepancy** before asking — e.g. "the why claims he *always* prefers X, but the only evidence is one session under deadline; here's that moment." That is the point: a research-and-learning moment, not a rubber stamp.

### 2. Present with zero-click evidence

Always show the verbatim quotes inline, marked verified (✓) — sulin judges *interpretation*, never whether a quote is real. Show the title, the why, support (events/sessions), the relation, the maturity `rationale` (score vs bar), and your own one-line faithfulness read.

### 3. Record sulin's call (pass your assessment as provenance)

- **accept:** `nix run .#review -- --accept <takeaway_id> --assessment "<your faithfulness check>"` (add `--note "..."` for anything else worth recording).
- **edit** (sulin corrects the title/why first — the highest-value signal): `--accept <id> --edit-title "..." --edit-why "..." --assessment "..."`
- **reject:** `--reject <id> --reason "<sulin's reason>"`
- **snooze** (defer until a date — required): `--snooze <id> --until 2026-07-15 --reason "..."`. ("Wait for more evidence" is *not* a snooze — more sessions grow the cluster into a fresh takeaway on their own; just reject or skip.)
- **contradicts** — if the takeaway's `relation.kind` is `contradicts`, accepting it affirms the new view; **also ask sulin whether to retire the concept it contradicts** (`relation.concept_id`), and if yes run `--retire <concept_id> --reason "..."`. Two decisions, one human judgment — don't auto-retire without asking.

Accept refuses a takeaway whose evidence no longer resolves (a concept needs verifiable backing) — if that happens, investigate rather than forcing it. On accept, confirm the concept was written; on its next run dream will see this concept and can label related takeaways `strengthens`/`refines`/`contradicts` instead of `new`. Then move to the next takeaway.

## Tier 2 — the structural-op proposal queue

Beside the takeaway queue is a second one: the gardener's **queued structural ops**. These don't add a belief — they *restructure* what concepts exist. Fetch them:

```
nix run .#review -- --proposals --json
```

If empty, say so and stop. Otherwise take them **one at a time**. Each proposal carries an `op`, its `params`, an untrusted `rationale`, a `stakes` score, and the **cited concepts** — each with its title, statement, a `valid` flag, and its **re-validated evidence** (the same verified quotes the trust chain serves the human). The op is one of: `merge` (fold concepts that say the same thing), `split` (divide one conflating two lessons), `abstract` (name a shared parent), `reparent`, `retire` (drop a stale concept), `merge_tags`/`retire_tag`.

### The check is the same `why ⊨ evidence` gate, one level up

The `rationale` is **untrusted** — exactly like a takeaway's `why`. Your job: **does the rationale FOLLOW from the cited concepts' evidence?** Read the verified quotes of each cited concept and decide whether the op is warranted. Flag drift and over-claim; **never auto-revise** — recommend, surface the discrepancy, let sulin call it.

Two-speed, same as tier 1:

- **Well-grounded + low-impact → fast.** The cited concepts plainly say the same thing (a `merge`), or the parent idea is real (`abstract`). Present compactly with the verified quotes and say it looks sound.
- **Thin / over-claimed → escalate yourself, before asking.** Trigger especially on:
  - a **`merge` that conflates DISTINCT lessons** — the two concepts' quotes are about different things; merging would erase a real distinction,
  - a **`retire` of a still-supported concept** — the `valid` flag is true and the evidence still backs it; the rationale calls it stale but the quotes don't agree,
  - a **`split`** whose proposed parts don't cleanly partition the evidence,
  - any rationale asserting more than the cited quotes support.

  Show sulin the discrepancy — "the rationale says c-x and c-y are the same lesson, but c-x's quote is about jj and c-y's is about nix; here are both." That's the point: protect the concept layer from a silent bad restructuring.

### Record sulin's call

- **accept** (APPLIES the op — the concept graph reorganizes): `nix run .#review -- --accept-proposal <proposal_id> --assessment "<your faithfulness read>"`. A `merge` invalidates the losers and unions their evidence into the winner; a `retire` drops the concept; etc. — all append-only, invalidate-don't-delete.
- **reject** (does NOT apply; and the gardener will **not** re-suggest it): `--reject-proposal <proposal_id> --reason "<sulin's reason>"`. Rejection is *remembered* — a re-gardened cluster won't re-surface the same dismissed op, so reject freely when an op is wrong.
- A `split` accept needs the per-part **evidence partition** (which quotes go to which new concept) — that's sulin's judgment, passed as `--split-parts '<json>'`; if he's unsure, reject and let the gardener re-propose, or escalate.

## Rules

- **Never decide.** Recommend, surface evidence and discrepancies, but the verdict is sulin's.
- **Don't widen scope.** Review the queue; don't go re-mine sessions or edit CLAUDE.md here.
- **Capture the reasoning.** Always pass `--assessment` so an accept records *why* it was trustworthy (audited later as "verified by Claude, approved by sulin").
- Keep the queue clearable in one sitting — it's small by design (dream's clustering is the noise filter). If it's large, surface the highest-stakes first.
