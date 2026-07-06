---
name: ratchet-research
description: Research a topic with sulin, then feed the agreed findings into ratchet. Invoke when he wants to investigate something (web, repos, docs) and land the durable conclusions as reviewed knowledge — not just read a one-off report. Claude researches and synthesizes with sources; sulin decides what survives; the survivors ingest as an incubating document and seed as concepts through the normal review gate.
---

# ratchet-research — research, agree, ingest

The web and other repos hold rules sulin would otherwise re-derive every session. This skill turns a research session INTO durable knowledge: Claude investigates, sulin decides what survives, and the survivors enter ratchet as a **document source** — the same path-as-session, untrusted-until-accepted mechanism `tap --file` uses for his hand-written CLAUDE.md (ADR-0031/0033). Nothing skips the gate; research just moves good findings TO it.

The trust posture is the whole point. Web synthesis is Claude-authored prose — **untrusted**. The chain that ends at a concept runs: quote → the document's immutable bytes → sulin's verdict. Until he accepts, a finding is a proposal, exactly as a claim is at review.

## The flow

### 1. Research — findings with sources

Investigate the topic (web search, reading repos/docs; fan out to subagents freely). Synthesize into **findings**, each a rule-shaped statement with its **source URL(s) inline** — the URL is the finding's provenance, and it rides into the evidence the review card later shows. A finding with no source is an opinion; label it as one or drop it.

### 2. Discuss — he picks what survives

Present findings conversationally, one cluster at a time — he challenges, refines, discards. **Never decide for him** (review's rule, one stage upstream): you surface the finding and its source; he says what's worth keeping. Only survivors get written down. This is the first of two gates; the accept at review is the second.

### 3. Ingest — write the doc, run the pipeline

Write the agreed findings as a dated research document — markdown, **one finding per rule-shaped statement, source URLs inline** — under a stable location:

```
~/.claude/research/<date>-<slug>.md          # e.g. 2026-07-05-batch-api-pricing.md
```

Then ingest and drain it through the pipeline (every command runs from anywhere):

```
nix run ~/ratchet#tap -- --file ~/.claude/research/<date>-<slug>.md
nix run ~/ratchet#tap -- --url <source-url>        # optional: a page he wants ingested WHOLE, verbatim
nix run ~/ratchet#weave -- --all
nix run ~/ratchet#chunk -- --all
nix run ~/ratchet#glean -- --all --source <date>-<slug>.md --max-usd 1
nix run ~/ratchet#resolve -- --limit 100
```

`--source` is a provenance substring (a document's source handle is its path), so `<date>-<slug>.md` focuses glean/resolve/review on just this doc. The `--url` line is optional and separate: it ingests a trusted source page verbatim as its OWN document (url-as-session), surfacing alongside your curated findings — use it when the page itself is worth keeping, not just your distillation of it.

### 4. Same-sitting accepts

The claims land **incubating at 1 session — by design** (a document can't self-mature; see honesty). Seed them in the same sitting:

```
nix run ~/ratchet#review -- --incubating --source <date>-<slug>.md --limit 0
```

Then run the **one-card-one-verdict discipline from `/ratchet-review`** — its faithfulness pass, its card shape, its ONE-question loop. Don't duplicate it here; the deltas that matter for research:

- The accept IS the "add this to the system" — an explicit yes, per finding: `nix run ~/ratchet#review -- --accept <claim> --assessment "researched with sulin <date>, agreed at the sitting"`.
- Faithfulness here checks the finding against its **source**, not a lived session: does the quote say what the finding claims, and does the source back it? A shaky source is a reject, not a snooze.
- Kinds and scopes **propose as usual** — a document finding derives `global`; a fact-to-look-up is `reference`, a rule-of-conduct `behavioral` (sulin's call overrides via `--kind`/`--scope`).

## The honesty (read before selling it)

ratchet's principle: **explained knobs, never hidden rules** (ADR-0027). Research earns no exemption.

- **Urgency is honest, not boosted.** A fresh research doc surfaces because documents date by ARRIVAL (mtime = save time = valid-time) and recent evidence outweighs old — plus the same-sitting accept, which is *sulin's* act, not the system's. There is **no hidden score bump** for research; that's deliberate. It rises because it's recent and because he chose to seed it — both visible.
- **No immunity — counter-evidence wins, by design.** An accepted research concept is a peer, not scripture. A lived session that contradicts it lands a real `contradicts` edge; WEAKEN nets its support down; a contested concept un-graduates; `retire` is always available. This is the promise: **living the opposite overrides an approved rule**, no matter that a webpage once said it.
- **A document can never self-mature.** One file (or URL) is ONE session however often it's re-saved or re-fetched — re-ingesting corroborates at ZERO added maturity. Maturity comes only from sulin's *lived* sessions or his *accept*. Re-running research manufactures no agreement.
- **The prose stays untrusted until accepted.** glean's quote is copied verbatim from the document's bytes (never retyped, so never hallucinated) — but that guarantees only the *quote* is real, not that the *finding* is true. Truth is sulin's verdict at the gate. Research moves findings to the gate; it never smuggles them past it.

## Rules

- **Never decide.** Research, synthesize, surface sources and drift; sulin picks what's kept and accepts each concept. Two gates, both his.
- **Every finding carries a source.** No URL, no rule — the shape of review's "no quote, no claim" and generate's "no marker, no rule."
- **Rule-shaped, one per statement.** The document glean prompt restates each rule verbatim; write findings so one paragraph = one durable rule = one clean claim.
- **The doc is the artifact.** Save under `~/.claude/research/`; re-editing it seeds fresh claims on the next tap (the edited-file rule) — so revise the doc, never patch claims.
- **Don't widen scope.** This skill researches and ingests; it does not craft CLAUDE.md (that's `/ratchet-generate`) or re-mine sessions.
