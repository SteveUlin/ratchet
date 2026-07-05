# ratchet — runbook: processing a backlog

ratchet mines your Claude Code transcripts (later: PRs, Slack, …) into reviewed CLAUDE.md edits. You run it by hand, a bit at a time.

> **Invocation.** In the nix devshell a stage is `python -m ratchet.<stage> <args>`; outside it, `nix run .#<stage> -- <args>` (or `nix run ~/ratchet#<stage>` from anywhere). Below, `ratchet <stage>` is shorthand for either.

**The one idea.** Every stage is a *bounded, prioritized tick*. Each command takes `--limit N` (process the top-N by priority, leave the rest) and `--max-usd C` (stop at a budget), and **re-running never repeats finished work** (content-addressed dedup + per-item markers). So you never drain a stage before moving on — `glean` a thousand chunks at lunch, `resolve` a hundred events tonight. The backlog waits; the most valuable work rises first.

**The pipeline** — two human gates:

```
tap → weave → chunk → glean → resolve → synthesize → review¹ → garden → review² → generate
fetch  clean   window  extract match     prose        ↑concepts reorganize         ↑CLAUDE.md
```

`resolve` + `synthesize` replace `dream` (ADR-0010 → 0028; the module stays for history — don't run it on new events). v2 forced each event to pick from the whole takeaway catalog and over-merged; v3 splits the job: cheap match-or-mint on every event, expensive prose only for what matures.

---

## First, every session: `ratchet status`

```
ratchet status          # read-only census, stage by stage — no LLM, no writes
ratchet status --json
```

Where does the backlog sit? The census tells you which stage below deserves a tick — and counts matured claims still awaiting synthesize (the "why pending" population), so nothing sits invisible. Keep its `--maturity` in step with the bar you review at, so census and queue agree.

## The loop

**1 · Pull** — fetch new transcripts. The cursor dedups, so re-runs grab only what's new.

```
ratchet tap --last 200            # the 200 newest you haven't pulled yet
ratchet tap --since 2026-01-01    # or everything modified since a date
```

**2 · Prep** — cheap, deterministic, no LLM.

```
ratchet weave --all --limit 500   # raw transcript → one clean, speaker-tagged blob
ratchet chunk --all --limit 500   # → windowed chunksets (pointers, not copies)
```

**3 · Glean** — cheap LLM; extract durable events. Novelty-aware: novel/contradicting score high, already-known sinks. Each event is stamped with a subject key + statement signature (deterministic, free) — the raw material resolve matches on.

```
ratchet glean --all --limit 1000 --max-usd 5
ratchet glean --all --source ratchet --limit 500   # focus one project
ratchet glean --all --max-usd 5 --parallel 2       # stepping away? overlap 2-3 calls — the token bucket is SHARED with your interactive session, so this buys latency, not capacity
```

**4 · Resolve** — match or mint, per event; run it **often**. Statement-first entity resolution (ADR-0028): deterministic signals REJECT at $0 — the non-match mass costs nothing — and acceptance is ONE bounded comparative-with-none Haiku call over an event's residue candidates. No match → the event seeds a new claim on the spot (title = its summary, prose deferred). Every merge persists its match key, so review can audit exactly what the model saw.

```
ratchet resolve --limit 100 --max-usd 1
ratchet resolve --source ratchet --limit 50        # focus a project
ratchet resolve --dry-run                          # the priority-ordered working set + pool sizes; no calls
```

The dollars are trivial; **the real cost is wall-clock** — residue calls are serial `claude -p` riding out shared rate limits. `--max-usd` bounds that honestly: past the cap, residue events *defer* (no verdict, no marker; they retry next tick) while $0 events run to completion. Keep the budget small and tick often.

`ratchet resolve --audit-thin` lists live claims whose seed quote fails the noise floor (pre-gate noise seeds like `[assistant]` — bulk-review and retire).

**5 · Synthesize** — prose, **rarely**. Sonnet writes a claim's `why` only after it crosses the maturity bar, so the queue is bounded by the graduation rate, not the event rate. Review never waits for it: a matured, unsynthesized claim still surfaces, provisional title + "why pending" badge.

```
ratchet synthesize --limit 10 --max-usd 1
ratchet synthesize --claim <id>                    # review demand — bypasses the bar and the done-marker
```

**6 · Review¹ — claims → concepts** *(your judgment).* Only **mature** claims surface — corroborated across distinct, *recent* sessions — ordered by importance. The bar is your knob (`--maturity`; `--incubating` shows what sits below, with the reason). Do a sitting's worth.

```
ratchet review --pending                           # a sitting: the top 10 by importance ("top N of M" header; --limit widens, 0 = everything)
/ratchet-review                                    # or guided: Claude faithfulness-checks the evidence, one claim at a time
ratchet review --accept <claim> --assessment "…"
ratchet review --reject <claim> --reason "…"
```

The claim card is an audit surface, not just a summary:

- **Audit card** — for any claim whose maturity rests on an LLM merge (on the shipped cascade, every merged claim): each corroboration's verified quote beside the match key the model saw. The question it answers: *do these quotes teach one lesson?*
- **`--reject-merge <edge-id>`** — the "not the same" verdict, one compound decision: retracts the edge, reopens the event, blocks the pair permanently. Retraction IS the split; nothing latches.
- **Merge suggestions** ride the cards (residue-band pairs, quotes side by side, TTL'd, stored nowhere): confirm `--merge-claims <loser> <winner>`, dismiss `--reject-merge A,B`.
- **`--contested`** — near-bar claims carrying a live contradicts edge, so one wrong LLM CONTRADICTS verdict can't silently suppress a good claim.
- **Kind** — synthesize proposes `behavioral` (shapes conduct → projected into CLAUDE.md) or `reference` (a fact to look up — kept, never projected by default). `--accept` records it (`--kind` overrides); `--set-kind <concept> <kind>` re-kinds a concept accepted earlier (ADR-0029).
- **Scope** — the evidence proposes *where* the lesson applies (no LLM): every quote in one repo → that repo's label; 2+ repos or none → `global`. The card shows `SCOPE: <repo> (derived)` when non-global. `--accept` records it (`--scope` overrides, any repo label); `--set-scope <concept> <repo|global>` re-scopes a concept accepted earlier (ADR-0030).

**7 · Garden — reorganize the concept layer** *(periodic).* Once concepts have accumulated, tidy them: tag, then propose merges / splits / abstractions / retires, and flag stale ones. Low-stakes auto-applies; the rest queues for review². Run occasionally, not every tick.

```
ratchet garden --limit 100                                     # tag concepts (cheap, auto)
ratchet garden propose --limit 20 --max-usd 2 --auto-max-stakes 0.15
ratchet garden propose --stale-only                           # just the "re-confirm or retire?" staleness flags (no LLM)
```

**8 · Review² — structural proposals** *(your judgment).* Accept (applies the change) or reject (sticks — never re-suggested).

```
ratchet review --proposals --limit 20
/ratchet-review                                    # the same skill, tier 2
ratchet review --accept-proposal <id>
ratchet review --reject-proposal <id> --reason "…"
```

**9 · Generate — project concepts → CLAUDE.md.** Refreshed from your reviewed concepts each run; a retired concept's rule *vanishes*. Projects **behavioral ∧ global** concepts only — reference facts are lookup material, not rules (`--kinds behavioral,reference` widens), and a repo-scoped concept belongs in *that repo's* CLAUDE.md, not the global one (`--repo` routes it; the region's header states both filters). The diff is your final gate.

```
ratchet generate --diff   --target ~/.claude/CLAUDE.md   # review the change
ratchet generate --apply  --target ~/.claude/CLAUDE.md   # write it — only the marked region; your own content is untouched
ratchet generate --diff  --repo claude-bus --target ~/claude-bus/CLAUDE.md   # a repo's own region: behavioral ∧ scope=claude-bus
ratchet generate --apply --repo claude-bus --target ~/claude-bus/CLAUDE.md   # (an unknown --repo is refused, with the scopes present)
```

> The default target is a *safe staged file* — you point `--target` at a real CLAUDE.md deliberately. To craft the wording with Claude rather than a mechanical render, use `/ratchet-generate`.

---

## Documents — seed your hand-written rules (ADR-0031)

Your existing `~/.claude/CLAUDE.md` is knowledge ratchet can't see: the novelty digest keeps judging rules you already wrote down as `novel`, and they sit outside decay/contradiction tracking. Ingest it as a **document source** — verbatim; the file's path is its source *and its session*, and the render excludes the `ratchet:generated` region, so re-ingesting your own projection can never let the system corroborate itself:

```
ratchet tap --file ~/.claude/CLAUDE.md      # cursor applies: re-taps of an unchanged file no-op
ratchet weave --all                         # documents ride the normal prep sweep ($0, idempotent)
ratchet chunk --all
ratchet glean --all --source CLAUDE.md --max-usd 1  # document prompt: each rule → one event
ratchet resolve --limit 100
```

The claims sit **incubating at 1 session — by design**: one file is one session no matter how often it's saved, so a document can never self-mature; only your lived sessions (or your accept) mature it. Seed them in one sitting:

```
ratchet review --incubating --source CLAUDE.md --limit 0   # every rule-claim, with its rationale
ratchet review --accept <claim> --assessment "hand-written rule, seeded from CLAUDE.md"
```

Kinds and scopes propose as usual (a document claim derives `global`). From then on the rules live like any concept: a re-learned rule judges `known`, a lived contradiction lands a real contradicts edge, an unlived rule decays toward "re-confirm or retire?". An **edited** rule seeds a fresh claim on the next tap. This is also the pilot for fetched sources (PDFs, webpages) — same mechanism, plus a fetcher.

## Cadence

Documentation, not a typed API (design doc §8): what each stage watches and how often a hand-run is safe. When a scheduler is ever warranted, it reads this table; until then you run stages explicitly.

| Group | Stage | When | Cost |
|---|---|---|---|
| — | `status` | first, every session | $0 |
| INGEST | `tap` / `weave` / `chunk` | on new transcripts | $0 |
| | `glean` (+ sig stamps) | with ingest | Haiku |
| REFINE | **`resolve`** | **often** — any sitting | $0 rejection + capped Haiku residue; trends toward one call per event as the pool grows — wall-clock, not dollars, is the cost; keep `--max-usd` small |
| | **`synthesize`** | rare — matured claims / review demand | Sonnet |
| | `garden` (tag) | slow (~weekly) | Haiku |
| | `garden propose` (+ staleness) | slow (~weekly) | Sonnet / $0 |
| REVIEW | `review` — tier 1 (claims), tier 2 (proposals) | on demand, a sitting's worth | human |
| PRODUCE | `generate` | on demand | $0 |

## One-time reset (2026-07)

v2's router fused unrelated lessons and its in-place support latched them (ADR-0028's motivating failure). The migration is append-only and idempotent — it retired the 22 poisoned v2 takeaways and reopened their consolidated events for v3 to re-resolve:

```
ratchet resolve --reset-v2 --dry-run      # preview
ratchet resolve --reset-v2                # retire v2 takeaways, reopen their events
```

Then drain the reopened backlog with forget disabled, until `status` shows 0 events awaiting resolve:

```
ratchet resolve --no-forget --limit 100 --max-usd 1     # repeat until pending hits 0
```

A freshly reopened backlog is all "stragglers" by cycle count — `--no-forget` keeps the eviction pass from staling it mid-drain. Once drained, drop the flag.

## Knobs (on every LLM stage)

| flag | does |
|---|---|
| `--limit N` | process the top-N by priority; leave the rest for next run |
| `--max-usd C` | stop cleanly at a spend; committed-so-far persists (resolve: paid residue defers, $0 work completes) |
| `--priority aging` | surface old-but-modest work that pure value-ranking would starve — use on a long backlog |
| `--source <substring>` | focus by PROVENANCE — the item's source handle contains the substring: a project name for transcripts, a file path for documents (glean / resolve / review; exact key is `--source-id`) |
| `--dry-run` | show what a stage *would* do, without doing it |

## A note on old data

Run a months-old **backfill** like any other batch. ratchet weights each piece of evidence by **when the conversation happened**, not when you process it — so old data can *add* lessons that are still true, but it can't re-entrench a stale belief or overturn a current one (recent evidence outweighs old). A learning you've stopped living quietly decays toward a "re-confirm or retire?" flag; one you keep living stays. **Nothing is ever auto-deleted** — staleness and contradiction only *surface* a claim for your review.
