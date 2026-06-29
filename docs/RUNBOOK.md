# ratchet — runbook: processing a backlog

ratchet mines your Claude Code transcripts (later: PRs, Slack, …) into reviewed CLAUDE.md edits. You run it by hand, a bit at a time.

> **Invocation.** In the nix devshell a stage is `python -m ratchet.<stage> <args>`; outside it, `nix run .#<stage> -- <args>`. Below, `ratchet <stage>` is shorthand for either.

**The one idea.** Every stage is a *bounded, prioritized tick*. Each command takes `--limit N` (process the top-N by priority, leave the rest) and `--max-usd C` (stop at a budget), and **re-running never repeats finished work** (content-addressed dedup + per-item markers). So you never drain a stage before moving on — `glean` a thousand chunks at lunch, `dream` fifty tonight. The backlog waits; the most valuable work rises first.

**The pipeline** — two human gates:

```
tap → weave → chunk → glean → dream → review¹ → garden → review² → generate
fetch  clean   window  extract consolidate ↑concepts  reorganize    ↑CLAUDE.md
```

---

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

**3 · Glean** — cheap LLM; extract durable events. Novelty-aware: novel/contradicting score high, already-known sinks.

```
ratchet glean --all --limit 1000 --max-usd 5
ratchet glean --all --topic ratchet --limit 500    # focus one project
```

**4 · Dream** — the sharper LLM; consolidate the most-salient events into *takeaways*. Run it **k times** — each tick drains the top of the queue.

```
ratchet dream --limit 50 --max-usd 2
ratchet dream --topic ratchet --limit 50           # focus a project
```

**5 · Review¹ — takeaways → concepts** *(your judgment).* Only **mature** takeaways surface — corroborated across distinct, *recent* sessions — ordered by importance. Do a sitting's worth.

```
ratchet review --pending --limit 20                # the top-N most important
/ratchet-review                                    # or guided: Claude faithfulness-checks the evidence
ratchet review --accept <takeaway> --assessment "…"
ratchet review --reject <takeaway> --reason "…"
```

**6 · Garden — reorganize the concept layer** *(periodic).* Once concepts have accumulated, tidy them: tag, then propose merges / splits / abstractions / retires, and flag stale ones. Low-stakes auto-applies; the rest queues for review². Run occasionally, not every tick.

```
ratchet garden --limit 100                                     # tag concepts (cheap, auto)
ratchet garden propose --limit 20 --max-usd 2 --auto-max-stakes 0.15
ratchet garden propose --stale-only                           # just the "re-confirm or retire?" staleness flags (no LLM)
```

**7 · Review² — structural proposals** *(your judgment).* Accept (applies the change) or reject (sticks — never re-suggested).

```
ratchet review --proposals --limit 20
/ratchet-review                                    # the same skill, tier 2
ratchet review --accept-proposal <id>
ratchet review --reject-proposal <id> --reason "…"
```

**8 · Generate — project concepts → CLAUDE.md.** Refreshed from your reviewed concepts each run; a retired concept's rule *vanishes*. The diff is your final gate.

```
ratchet generate --diff   --target ~/.claude/CLAUDE.md   # review the change
ratchet generate --apply  --target ~/.claude/CLAUDE.md   # write it — only the marked region; your own content is untouched
```

> The default target is a *safe staged file* — you point `--target` at a real CLAUDE.md deliberately. To craft the wording with Claude rather than a mechanical render, use `/ratchet-generate`.

---

## Knobs (on every stage)

| flag | does |
|---|---|
| `--limit N` | process the top-N by priority; leave the rest for next run |
| `--max-usd C` | stop cleanly at a spend; committed-so-far persists |
| `--priority aging` | surface old-but-modest work that pure value-ranking would starve — use on a long backlog |
| `--topic <project>` | focus one project (glean / dream / review) |
| `--dry-run` | show what a stage *would* do, without doing it |

## A note on old data

Run a months-old **backfill** like any other batch. ratchet weights each piece of evidence by **when the conversation happened**, not when you process it — so old data can *add* lessons that are still true, but it can't re-entrench a stale belief or overturn a current one (recent evidence outweighs old). A learning you've stopped living quietly decays toward a "re-confirm or retire?" flag; one you keep living stays. **Nothing is ever auto-deleted** — staleness and contradiction only *surface* a concept for your review.
