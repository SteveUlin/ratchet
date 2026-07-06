---
name: ratchet-writeup
description: Study a subdirectory, subsystem, or cross-cutting topic of a codebase with sulin, then land the durable statements as repo-scoped reviewed knowledge. Invoke when he says "let's make a write-up on this subdir/topic" — Claude reads the actual code, writes a SHORT dated write-up, and its survivors ingest as a repo-scoped document and seed as concepts through the normal review gate. Extends /ratchet-research's document mechanics; adds the code-verification discipline and per-card scope.
---

# ratchet-writeup — study code, verify, ingest as repo-local rules

A codebase holds invariants, design pressures, and gotchas sulin re-derives every time he reopens a subsystem. This skill turns *studying* part of a codebase into durable, repo-scoped knowledge: Claude reads the actual code, writes a SHORT dated write-up, sulin decides what survives, and the survivors enter ratchet as a **document source** — the same untrusted-until-accepted path `tap --file` uses (ADR-0031). It is `/ratchet-research`'s sibling: identical ingest mechanics, two deltas — the **code-verification discipline** (§2) and per-card **scope** (§5).

The deliverable is NOT a file inventory. It is **rule-shaped durable statements** — invariants, design pressures, gotchas, conventions — each citing the `file:line` that enforces it.

## The flow

### 1. Study — read the actual code

Read the subdirectory / subsystem / topic; **fan out to subagents freely** — their searches and file dumps stay out of the main thread, you keep the conclusions. Synthesize into statements, one durable rule per statement, each carrying the `file:line` it rests on.

### 2. Verify against the code — this skill's defining rule

sulin's own principle: **docs are never a source of truth; a documented property is true only if code enforces it** (the house's cardinal sin — CLAUDE.md Style). So every statement is verified against the code **at write time**: trace the enforcing mechanism, cite it. A statement you cannot anchor to code is speculation — label it as such or drop it.

The irony to name, not hide: a write-up is itself docs. It earns trust through the review gate and then **decays like everything else** — an unlived claim drifts toward "re-confirm or retire", which is exactly right for documentation of a moving codebase. You are authoring a claim with an expiry, not a monument.

### 3. Discuss — he picks what survives

Present statements conversationally, one cluster at a time — he challenges, refines, discards. **Never decide for him** (review's rule — the verdict is his). Only survivors get written down.

### 4. Ingest — write the doc, run the pipeline

Write the survivors as a dated markdown write-up — one rule-shaped statement per paragraph, `file:line` inline — under the same home as research docs:

```
~/.claude/research/<date>-<repo>-<slug>.md   # e.g. 2026-07-06-ratchet-resolver.md
```

Location matters: research docs and write-ups share one home, so the whole document corpus is one `--source`-searchable place; the `<repo>-` prefix keeps them legible. Then ingest and drain — every command runs from anywhere:

```
nix run ~/ratchet#tap -- --file ~/.claude/research/<date>-<repo>-<slug>.md
nix run ~/ratchet#weave -- --all
nix run ~/ratchet#chunk -- --all
nix run ~/ratchet#glean -- --all --source <date>-<repo>-<slug>.md --max-usd 1
nix run ~/ratchet#resolve -- --limit 100
```

`--source` is a provenance substring (a document's source handle is its path), so `<date>-<repo>-<slug>.md` focuses glean/resolve/review on just this write-up. No `--url` line here: a write-up's sources are `file:line`, not pages.

### 5. Same-sitting accepts — WITH SCOPE (the delta that matters)

The claims land **incubating at 1 session — by design** (a document can't self-mature; see honesty). Seed them in the same sitting:

```
nix run ~/ratchet#review -- --incubating --source <date>-<repo>-<slug>.md --limit 0
```

Then run the **one-card-one-verdict discipline from `/ratchet-review`** — its faithfulness pass, card shape, ONE-question loop. Don't duplicate it. The delta: **a write-up's statements belong to THAT repo.** But a document claim carries no repo facet — its evidence is the doc's own prose, so scope **derives `global`** (ADR-0031 §6). Left alone, a repo-local rule silently joins the global CLAUDE.md every project reads. So each accept passes `--scope <repo>` explicitly:

```
nix run ~/ratchet#review -- --accept <claim> --scope <repo> --assessment "wrote up <repo>/<subsystem> with sulin <date>; verified against code; agreed at the sitting"
```

The reviewer's `--scope` is what turns a write-up into repo-local rules; forgetting it pollutes the global projection (ADR-0030). A genuinely repo-transcending lesson stays `global` — **sulin's call, per card**, never a blanket default. Repo-scoped concepts route into that repo's own CLAUDE.md:

```
nix run ~/ratchet#generate -- --repo <repo> --target ~/<repo>/CLAUDE.md --diff   # the loop's last gate
```

## The honesty (read before selling it)

Inherit `/ratchet-research`'s honesty section wholesale — no hidden urgency bump, no immunity from counter-evidence, a document can never self-mature, the prose stays untrusted until accepted. All of it holds; a write-up is a document like any other.

One write-up-specific addition: the **`file:line` citations inside the doc are for the HUMAN's verification** — they let sulin (or a later reader) re-open the code and check. They do NOT extend the trust chain, which still guarantees only quote → the document's immutable bytes. Truth rests on two things the citation is not: the **code-verification discipline** you ran at write time, and **sulin's verdict** at the gate. A cited line that no longer says what the statement claims is caught by re-reading, never by the pipeline.

## Rules

- **Verify at write time.** No enforcing mechanism, no statement — the shape of review's "no quote, no claim." Trace the code; label or drop speculation.
- **Rules, not inventory.** Invariants, pressures, gotchas, conventions — never a tour of files. One paragraph = one durable rule = one clean claim.
- **Scope every accept.** A write-up seeds repo-local knowledge; pass `--scope <repo>` unless a lesson genuinely transcends the repo. Forgetting it is the one failure this skill exists to prevent.
- **Never decide.** Study, verify, surface `file:line` and drift; sulin picks survivors and accepts each concept. Two gates, both his.
- **The doc is the artifact.** Save under `~/.claude/research/`; re-editing it seeds fresh claims on the next tap — revise the doc, never patch claims.
- **Don't widen scope.** This skill studies and ingests; it does not craft CLAUDE.md (that's `/ratchet-generate`) or re-mine sessions.
