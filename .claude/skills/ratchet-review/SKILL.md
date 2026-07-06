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

## The sitting

A review sitting is a **conversation, not a report**. Careful verdicts are expensive — review fatigue collapses past ten or twenty of them — so the CLI already bounds the queue to a sitting's worth (the default `--pending` is the **top 10 by importance**, and its header says "top N of M" so the backlog depth is always visible; `--limit N` widens, `--limit 0` loads everything). Your presentation must be bounded the same way: sulin sees **one claim, gives one verdict, sees the next**. Never a wall of cards, never a map of the whole queue.

### Open the sitting (this order, every time)

1. **Orient** — `nix run ~/ratchet#status`. Give sulin ONE line: the counts that matter (pending, incubating, contested, proposals, thin seeds).
2. **Contested** — `nix run ~/ratchet#review -- --contested`. These are claims being knocked down near the bar — one wrong LLM CONTRADICTS verdict must not silently suppress a good claim. Surface the **count**, and if a contradiction is holding down something that looks durable, say so *now*: it is the one thing that must not wait at the back of a queue.
3. **Hygiene** — `nix run ~/ratchet#resolve -- --audit-thin`. The pre-gate noise-seed backlog. Report the **count only**, unless items are new since the last sitting — then name them and offer the bulk retire.
4. **The index** — `nix run ~/ratchet#review -- --pending --brief --json`. One light row per item (title, standing, badges — no evidence), importance-ordered, with the backlog depth ("top 10 of 23") so sulin always knows what the slice was cut from — and can ask to widen or `--source`-narrow it (provenance substring: a project name for transcripts, a file path for documents). **Never fetch the full queue JSON** — a 15-claim queue renders ~56k tokens of evidence you will only read one claim at a time; the index is the sitting's map, `--card` its magnifier.

If the slice is empty, **don't just say "nothing to review."** The opener already surfaced contested; also run `nix run ~/ratchet#review -- --incubating` to show what's accruing and *why* it's below the bar (each with its score), and offer to lower `--maturity` for this sitting if something looks durable enough already. The bar is sulin's knob, not a fixed rule — a claim earns the queue by *recurring* across distinct, recent sessions; that is evidence of durability, not a quota.

### Assess per card, present incrementally

The sitting is a CURSOR over the index, not a batch read — context stays one card deep no matter how
large the backlog grows. From the index alone, give sulin:

- **One orientation paragraph** — 5 lines max: how many claims and of what ("top 10 of 23 pending"), any clusters the index's titles show ("four are jj lessons"), which badges you'll flag when their turn comes (why-pending, contradictions). **NO per-claim tables, no summary map of the queue.**

Then loop, one claim per verdict, in index order:

1. `nix run ~/ratchet#review -- --card <id> --json` — the full render for exactly this claim: verified evidence, the audit card, merge suggestions, standing.
2. Run the private faithfulness pass below on it — escalate to `--context <id>` yourself if it smells — and only then present the card.
3. One question, one verdict, execute, confirm, fetch the next card. Never present claim N+1's content before claim N has its verdict.

(If a card is unusually heavy even alone, delegating its deep read to a subagent is fine — but the default loop needs no delegation, because a card is small by construction.)

### The private pass, per claim (this decides fast vs deep)

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

- **Solid → fast path.** The quotes teach one lesson, the `why` (if present) follows from them, support is decent (multiple `events`/`sessions`), nothing high-stakes. The card gets a one-line "looks well-supported" read — sulin can just approve.
- **Risky → deep path. Escalate *yourself*, before its turn comes.** Trigger on any of:
  - **suspect merge** — audit-card quotes about different things, a ⚠ disjoint-subject line, or a merge at low `stmt_sim` where only the model's judgment connects them,
  - **thin support** — 1 event / 1 session but a confident, broad claim,
  - **why-drift** — the `why` asserts more than the quotes actually support,
  - **high stakes** — `relation.kind` is `contradicts`, or the claim is `contested`,
  - or sulin asks "why do you believe this?".

  On the deep path, pull the surrounding transcript with `nix run ~/ratchet#review -- --context <claim_id>` (a wider window around each quote, audit card included), read what actually happened, and **show sulin the discrepancy** on that claim's card before asking — e.g. "the why claims he *always* prefers X, but the only evidence is one session under deadline; here's that moment." That is the point: a research-and-learning moment, not a rubber stamp.

### The card, then ONE question

**Write for a human, not a database** (sulin, 2026-07-06). The card's job is to let sulin judge a
lesson he may not remember living. Open with a plain-language explanation of what the lesson IS and
where it came from (project name, what the sessions were doing) — as if telling a colleague who
wasn't there. Define any term of art in one clause the first time it appears ("truncate-back —
cutting the file back to its pre-write size"). **Ids are for commands, not prose**: sulin does not
look anything up by `t-…`/`c-…` id, so never reference another claim or concept by id — describe it
in words ("your existing rule about verifying scope before deleting C++ code") and put the id only
in parentheses where he'd need it to run a verb. If a claim is implementation-deep, say so plainly
and lead with the scope/kind call that follows from it.

Present each claim as a compact, **wrap-safe** card — short labeled lines, **never a wide table** (tables clip in terminals). Always show the verbatim quotes inline, marked verified (✓) — sulin judges *interpretation*, never whether a quote is real. The shape:

```
[3/10] t-4f2a — prefer jj over git for version control
maturity: 2.3 ≥ bar 1.5 — corroborated across 3 recent sessions
✓ "always commit with jj and never use git for version control"
✓ "version control goes through jj, so avoid reaching for git"
audit: llm merge, stmt_sim 0.21, 2 candidates — quotes agree; same repo
merge? t-9c01 "jj commit descriptions…" is residue-similar — same lesson?
read: the why follows the quotes; support is solid.
```

- **title + maturity line** always; add the why (or the `why-pending` badge), the relation, and scope only when they carry information (`cross-cutting` claims span subjects — that's what a global CLAUDE.md is *for*, not a defect).
- **audit / match-key lines only when `by:llm` edges or flags exist** — a seed-only claim has nothing to audit; don't pad the card. A ⚠ or a discrepancy you found in the private pass goes here, quoted.
- **kind line only when the proposal is `reference`** (`claim_kind` in the JSON): synthesize types each claim *behavioral* (shapes conduct — projected into CLAUDE.md) or *reference* (a fact/mechanism you'd look up — kept and queryable, excluded from generation). Faithfulness and generation-usefulness are orthogonal: a true fact can still be the wrong thing to project. The proposal is untrusted like the `why`; sulin's accept confirms it.
- **scope line only when the derivation is non-global** (`scope_repo` in the JSON): the evidence proposes *where* the lesson applies — every quote in one repo → that repo's label (it belongs in *that repo's* CLAUDE.md, routed by `generate --repo`); 2+ repos or none → `global` (nothing to show). Deterministic, not an LLM guess — but still only a proposal; sulin's accept confirms it.
- **its merge suggestion, if any** (they ride the cards, derived at render time, TTL'd, stored nowhere): both claims' quotes side by side, no similarity score — the human judges words, not numbers.
- **your read, ≤ 2 lines** — the faithfulness verdict from the private pass.

Then ask **ONE question** with the verdict options:

> **Verdict?** accept · edit-accept · reject · merge-with-suggested · skip-for-now · stop sitting

(offer *merge-with-suggested* only when the card carries a suggestion). **Execute the chosen verb immediately** (commands below), **confirm in one line** ("accepted → concept c-ab12"), and move to the next claim. *skip-for-now* records nothing — the claim just stays queued for a later sitting. *stop sitting* jumps to the close.

**The batch escape hatch.** If sulin says "accept the rest of the cluster", "reject all the nix ones", or anything similarly explicit — honor it: run the verbs, confirm each in one line, then resume one-at-a-time with whatever remains. The one-at-a-time default bends to explicit instruction, never the reverse.

### Record sulin's call (pass your assessment as provenance)

- **accept:** `nix run ~/ratchet#review -- --accept <claim_id> --assessment "<your faithfulness check>"` (add `--note "..."` for anything else worth recording; add `--kind behavioral|reference` when sulin's call differs from the proposed kind, and `--scope <repo|global>` when it differs from the derived scope — the defaults follow the proposals).
- **re-kind** (a concept already accepted under the wrong kind — e.g. a lookup fact projecting as a rule): `--set-kind <concept_id> <kind> --reason "..."` — reviewer-owned like retire, and it outranks the kind recorded at accept.
- **re-scope** (a concept already accepted under the wrong scope — e.g. a repo-local lesson sitting in the global CLAUDE.md): `--set-scope <concept_id> <repo|global> --reason "..."` — same shape as re-kind (reviewer-owned, outranks the accept, valid targets only); repo labels are free text.
- **edit-accept** (sulin corrects the title/why first — the highest-value signal): `--accept <id> --edit-title "..." --edit-why "..." --assessment "..."`
- **reject:** `--reject <id> --reason "<sulin's reason>"`
- **merge** (confirm a suggestion — fold the loser into the winner): `--merge-claims <loser> <winner> --reason "…"`; **dismiss** it (distinct lessons — never asked again): `--reject-merge <loserId>,<winnerId> --reason "…"`
- **snooze** (defer until a date — required): `--snooze <id> --until 2026-07-15 --reason "..."`. ("Wait for more evidence" is *not* a snooze — more sessions corroborate the claim on their own; just reject or skip.)
- **contradicts** — if the claim's `relation.kind` is `contradicts`, accepting it affirms the new view; **also ask sulin whether to retire the concept it contradicts** (`relation.concept_id`), and if yes run `--retire <concept_id> --reason "..."`. Two decisions, one human judgment — don't auto-retire without asking.

Accept refuses a claim whose evidence no longer resolves (a concept needs verifiable backing) — if that happens, investigate rather than forcing it. On accept, confirm the concept was written; the concept digest feeds glean, so future events arrive labeled `strengthens`/`refines`/`contradicts` instead of `new`.

### Sibling observations become decisions

Anything you notice about a claim **not** in this slice — a twin of one under review, a stale-looking concept, a suggestion pairing with an unqueued claim — must leave the sitting as one of exactly two things: an **executed verb** (`--reject-merge`, `--merge-claims`, `--retire`, a `--note` on an accept) or an **explicit deferred item you name at the close**. Never prose that dies with the session.

### Close the sitting

One paragraph: the tally (accepted / rejected / merged / skipped), what entered the concept layer, and every deferred item **by name**. If anything was accepted, remind sulin that `nix run ~/ratchet#generate -- --diff` shows what the new concepts change in CLAUDE.md — the loop's last gate.

## Tier 2 — the structural-op proposal queue

Beside the claim queue is a second one: the gardener's **queued structural ops**. These don't add a belief — they *restructure* what concepts exist. Fetch them:

```
nix run ~/ratchet#review -- --proposals --json
```

If empty, say so and stop. Otherwise the same discipline as tier 1: assess them all privately, then present **one at a time** — card, ONE question, execute, confirm, next. Each proposal carries an `op`, its `params`, an untrusted `rationale`, a `stakes` score, and the **cited concepts** — each with its title, statement, a `valid` flag, and its **re-validated evidence** (the same verified quotes the trust chain serves the human). The op is one of: `merge` (fold concepts that say the same thing), `split` (divide one conflating two lessons), `abstract` (name a shared parent), `reparent`, `retire` (drop a stale concept), `merge_tags`/`retire_tag`.

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
- **One claim, one verdict, then the next.** Assess everything privately up front, but never show claim N+1 before claim N is decided — and never a per-claim table of the queue. Explicit batch instructions ("accept the rest") override this; nothing else does.
- **Wrap-safe rendering.** No wide tables — they clip in terminals. Short labeled lines.
- **Don't widen scope.** Review the queue; don't go re-mine sessions or edit CLAUDE.md here.
- **Prefer the surgical verb.** A foreign quote in a good claim → `--reject-merge` the edge, not `--reject` the claim; a wrong wording → `--edit-title`/`--edit-why` on accept.
- **Capture the reasoning.** Always pass `--assessment` so an accept records *why* it was trustworthy (audited later as "verified by Claude, approved by sulin").
- **Everything noticed becomes a decision** — an executed verb or a named deferred item at the close, never loose prose.
- The queue is bounded by design: the maturity bar filters noise and the default slice is a sitting's worth. If sulin wants more, widen with `--limit` (0 = everything) or focus with `--source` — his call, not yours.
