---
name: ratchet-generate
description: Craft ratchet's CLAUDE.md projection with sulin — tighten wording, group rules by theme, fit his voice — on top of the mechanical `generate` CLI. Invoke when he wants to land his reviewed concepts into a real CLAUDE.md and shape how they READ, not just mechanically render them. You compose and critique; every rule stays faithful to a concept's statement + evidence, and sulin makes every call.
---

# ratchet-generate — craft the projection

`generate` is the loop-closer: it projects the VALID concepts (ratchet's curated, human-reviewed source of truth) into a **marked region** of a CLAUDE.md. The CLI is MECHANICAL by design — concept statements rendered VERBATIM, no LLM — because the statement already passed the review gate, and re-wording it with a model would re-open an untrusted hop past the one place the pipeline earned trust.

This skill is the layer ON TOP: a human + Claude *crafting* the projection — tighten a rule, regroup by theme, fit sulin's own CLAUDE.md voice, decide what's worth surfacing — **rather than only mechanically rendering it**. But the moment Claude rewrites prose, that untrusted hop is back. So this skill carries the same FAITHFULNESS discipline `/ratchet-review` does, one stage downstream: every rule Claude writes or edits must stay TRUE to a reviewed concept's `statement` + evidence; **Claude proposes, sulin decides**, and nothing fabricates a rule no concept supports.

## The two faces of the region (lead with this)

The region is delimited — `<!-- ratchet:generated START — managed by ``ratchet generate --apply``; edits here are overwritten -->` … `<!-- ratchet:generated END -->`. Everything OUTSIDE the markers is human-owned and byte-preserved. The marker text is literal: it has **two modes**, and the choice is sulin's.

- **MECHANICAL** (`--apply`): the verbatim reviewed statements, auto-refreshing — a retired concept's rule *vanishes* on the next run (retraction-for-free), re-apply is idempotent, every rule greppable back to its concept. Fully traceable, zero craft. The baseline and the fallback.
- **CRAFTED** (Claude + sulin edit the region): polished wording, theme-shaped grouping, his voice. But it is a **snapshot** — a later mechanical `--apply` *reverts it* to verbatim. So craft is bespoke: re-craft (not `--apply`) when concepts change. Worth it when the file ships somewhere he reads and wants tight; not worth it for a staging dump.

State this tradeoff plainly before composing — it's the whole reason the skill exists, and the reason a crafted region isn't a free lunch.

## The faithfulness discipline (the heart)

The mechanical render is faithful BY CONSTRUCTION: verbatim statement + a trailing `<!-- c-id -->` marker (the trust chain reaching the projection — grep the id → concept → re-validated evidence → raw transcript) + retraction-for-free. The instant Claude rewords or regroups, that guarantee is yours to keep:

- **Every rule carries its `<!-- c-id -->` marker** and traces to a VALID concept. No marker → no rule. A rule Claude can't anchor to a concept is fabricated — drop it.
- **A reworded rule may not assert more than its concept's `statement` + evidence support.** Same `why ⊨ evidence` gate as review, one stage on: drift UP the chain (tighten, never broaden). Tightening "prefer jj for most ops" → "use jj, never git" is a different claim — check it.
- **Flag a dropped or garbled render.** If the mechanical projection mangled a statement or lost a concept, surface it — don't silently "fix" it in prose; the bug is upstream.
- `generate --concepts` is your substrate: per concept, the `statement` (the verbatim source) + the re-validated `evidence` quotes (the verified ground truth) a crafted rule must honor. Read it before you reword.

## The flow

Run everything from `~/ratchet`.

### 1. Show the proposal

```
nix run .#generate -- --diff --target <his CLAUDE.md>   # the mechanical region vs the target's current one
nix run .#generate                                      # (or --dry-run) the projected region alone
nix run .#generate -- --concepts                        # each rule's statement + verified evidence (faithfulness substrate)
```

Show him what would land and how it changes his file. The `--diff` is the second gate (review decided what's TRUE; this decides what lands HERE). Read `--concepts` alongside so each rule's source statement + quotes are in view.

### 2. Iterate on the craft — two-speed

- **Reads well → fast.** The verbatim reviewed statements usually already read like good rules. If grouping is sensible, voice fits, nothing's garbled — say so and land the mechanical render. No craft tax for its own sake.
- **He wants to reshape → deep.** Tighten wording, regroup/reorder by theme to match his real CLAUDE.md shape, decide inclusion. For each edit:
  - keep the `<!-- c-id -->` marker; check the new wording against `--concepts` (statement + evidence) and **surface any drift before proposing** — "your tighter phrasing says *always*, but the concept's evidence is one deadline session; here's the quote."
  - **"decide inclusion" is a presentation call, not a retraction.** Omitting a rule from THIS file is fine — but the concept stays valid and the mechanical render re-adds it. If a rule is noise EVERYWHERE, the faithful fix is upstream: retire the concept (`/ratchet-review` → `review --retire`). If a statement reads wrong at the SOURCE, that's a review/garden fix, not a region patch. Tell him which.
  - never invent a rule; flag anything the render dropped or garbled.

### 3. Land it

- **Mechanical** (traceable baseline / fallback): `nix run .#generate -- --apply --target <his CLAUDE.md>` — writes ONLY the marked region; content above and below is byte-preserved. He reviews the final diff.
- **Crafted**: write the polished region (markers + every `<!-- c-id -->` intact) into the target; he reviews the diff. Remind him honestly — it's a snapshot; a later `--apply` reverts it to verbatim, so re-craft when concepts change.
- The default `--target` is a SAFE staged path under the data root — it never clobbers a real CLAUDE.md. He points `--target` at one deliberately.

## Rules

- **Never decide.** Compose, critique, surface evidence and drift; sulin picks render-vs-craft and lands it.
- **Every rule traces to a valid concept** — its `<!-- c-id -->` marker is the anchor. No marker, no rule.
- **Faithful to statement AND evidence** — the same gate as review, one stage downstream. Drift up the chain, never down.
- **Don't widen scope.** Wrong wording → fix the concept (review); wrong grouping → fix the tags (garden). Don't paper over an upstream problem inside the region.
- **The mechanical CLI is always the fallback.** If he'd rather just render, `--apply` and stop — the verbatim baseline is the safe default, not a lesser one.
