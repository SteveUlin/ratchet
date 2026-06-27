---
name: ratchet-review
description: Review ratchet's pending takeaways and promote the good ones to concepts — the human review gate. Invoke when sulin wants to triage what ratchet has learned from his sessions. You are an active faithfulness-checker; sulin makes every call.
---

# ratchet-review — the human gate

ratchet mines sulin's Claude Code sessions into **takeaways** (a synthesized "why" + verified evidence). This skill is the one hard gate: a takeaway sulin **accepts** becomes a **concept** (ratchet's curated knowledge), which feeds back into the system and is later projected into CLAUDE.md/skills. So **approving a false takeaway is costly** — your job is to make sulin's yes/no *informed*, never to decide for him.

The trust chain guarantees each evidence quote is a *real substring of an immutable blob* — so the quote is never in question. What is **untrusted** is the LLM-written `title`/`why`: it cites real events but can over-generalize past them. Checking that is your job.

## The loop

Run everything from the ratchet repo (`~/ratchet`). Fetch the queue:

```
nix run .#review -- --pending --json
```

If empty, tell sulin there's nothing to review and stop. Otherwise, take the takeaways **one at a time**, and for each:

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

Always show the verbatim quotes inline, marked verified (✓) — sulin judges *interpretation*, never whether a quote is real. Show the title, the why, support (events/sessions), the relation, and your own one-line faithfulness read.

### 3. Record sulin's call (pass your assessment as provenance)

- **accept:** `nix run .#review -- --accept <takeaway_id> --assessment "<your faithfulness check>"` (add `--note "..."` for anything else worth recording).
- **edit** (sulin corrects the title/why first — the highest-value signal): `--accept <id> --edit-title "..." --edit-why "..." --assessment "..."`
- **reject:** `--reject <id> --reason "<sulin's reason>"`
- **snooze** (defer until a date — required): `--snooze <id> --until 2026-07-15 --reason "..."`. ("Wait for more evidence" is *not* a snooze — more sessions grow the cluster into a fresh takeaway on their own; just reject or skip.)
- **contradicts** — if the takeaway's `relation.kind` is `contradicts`, accepting it affirms the new view; **also ask sulin whether to retire the concept it contradicts** (`relation.concept_id`), and if yes run `--retire <concept_id> --reason "..."`. Two decisions, one human judgment — don't auto-retire without asking.

Accept refuses a takeaway whose evidence no longer resolves (a concept needs verifiable backing) — if that happens, investigate rather than forcing it. On accept, confirm the concept was written; on its next run dream will see this concept and can label related takeaways `strengthens`/`refines`/`contradicts` instead of `new`. Then move to the next takeaway.

## Rules

- **Never decide.** Recommend, surface evidence and discrepancies, but the verdict is sulin's.
- **Don't widen scope.** Review the queue; don't go re-mine sessions or edit CLAUDE.md here.
- **Capture the reasoning.** Always pass `--assessment` so an accept records *why* it was trustworthy (audited later as "verified by Claude, approved by sulin").
- Keep the queue clearable in one sitting — it's small by design (dream's clustering is the noise filter). If it's large, surface the highest-stakes first.
