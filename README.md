# ratchet

Small, composable tools that mine durable learnings from Claude Code sessions and turn them
into reviewable improvements to your config. Plain files, no database, no daemon, stdlib-only
Python. There is no unifying service: you run each stage by hand as a **bounded, prioritized
tick** — `--limit`/`--max-usd` cap the work, re-running never repeats what's done, and the
backlog waits with the most valuable work rising first.

## Prerequisites

- **Python ≥3.10** — the floor `X | None` union syntax (PEP 604) sets; no `match`/`case`
  anywhere in the tree, so nothing pushes it higher.
- **The `claude` CLI, installed and authenticated.** The pipeline's one LLM seam
  (`completer.py`) shells out to `claude -p`; `glean`, `resolve`'s residue calls, `synthesize`,
  and `garden`'s proposals need it. `tap`, `weave`, `chunk`, `review`, `generate`, and `status`
  run at $0, no CLI required.
- **Transcripts under `~/.claude/projects`** — `tap`'s default source; `--datastore` points it
  at another root.
- **Nix is optional.** Every stage runs either way: `nix run .#<stage> -- <args>` or, from the
  repo root, `python -m ratchet.<stage> <args>`.

## Pipeline

Two human gates:

```
tap → weave → chunk → glean → resolve → synthesize → review¹ → garden → review² → generate
fetch  clean   window  extract match     prose        ↑concepts reorganize          ↑CLAUDE.md
```

- `tap` — copy new/changed transcripts (or a hand-written doc: `--file`, ADR-0031) in as
  immutable **raw** blobs.
- `weave` — reconstruct the active conversation (across rewinds, compacts, parallel tool calls;
  sidechains dropped) into one **cleaned** blob of speaker-tagged text. Deterministic, no LLM.
- `chunk` — window a cleaned blob into a **chunkset** of byte-offset pointers; a chunk resolves
  by slicing the frozen blob, no re-render. Deterministic, no LLM.
- `glean` — extract **events** (Haiku): a quote that must be a real substring of the chunk — a
  hallucinated quote is rejected deterministically — plus an untrusted one-sentence summary,
  novelty-aware markers, and the statement signature + subject key that `resolve` matches on.
- `resolve` — statement-first entity resolution (ADR-0028): deterministic signals REJECT at $0,
  one bounded Haiku call owns acceptance over the residue, and a non-match seeds a new **claim**
  on the spot. Corroboration is an edge, so a wrong merge retracts cleanly — it cannot latch.
- `synthesize` — Sonnet writes a claim's durable `why` only after it matures (corroborated
  across distinct, recent sessions), so prose cost is bounded by the graduation rate, not the
  event rate.
- `review` — the human gate, two tiers: promote mature claims to **concepts** (kind + scope
  recorded, ADR-0029/0030) and judge the gardener's structural proposals. Driven by the
  `/ratchet-review` skill; every verdict is an append-only decision blob.
- `garden` — tend the concept layer: cheap managed tags auto-apply (ADR-0014);
  merge/split/abstract/retire proposals and staleness flags queue for review².
- `generate` — mechanically project valid **behavioral** concepts into a marked CLAUDE.md
  region: repo-scoped concepts route to that repo's file (`--repo`), human content outside the
  region is byte-preserved, and a retired concept's rule unmakes itself on the next `--apply`.
  No LLM (ADR-0020).

`resolve` + `synthesize` replace `dream` (ADR-0010 → 0028): v2 forced each event to pick from
the whole takeaway catalog and over-merged; v3 splits the job into cheap match-or-mint on every
event and expensive prose only for what matures. The `dream` module stays for history — don't
run it on new events.

## Data

Data lives **outside** this repo in `$RATCHET_DATA_DIR` (default
`${XDG_DATA_HOME:-~/.local/share}/ratchet`), local-only; the repo holds only code. **Every
artifact is a blob** (ADR-0007): raw transcripts, cleaned renders, chunksets, events, claims,
edges, concepts, review decisions — content-addressed, immutable, lineage-linked, so every LLM
output stays verifiable forever against the frozen transcript it cites. Nothing is edited or
deleted: a change is a new version, a retraction is a new decision blob, and "what is currently
valid" — the review queue, the concept set, the census — is always a derived fold over the
blobs, never a stored list that could desync.

## Layout

- `ratchet/` — one module per stage (`tap`, `weave`, `chunk`, `glean`, `resolve`, `synthesize`,
  `review`, `garden/`, `generate`, `status`) over a shared substrate: `config`, `blobstore`
  (ADR-0007), `block` (the one driver every batch stage runs on — the same bounded-tick CLI,
  streaming per-item progress, per-item commit + resume; ADR-0009), `completer` (the injected
  LLM seam), `sig` + `subject` (the resolver's deterministic identity math: statement
  signatures, repo+files scope keys), `concepts` (the rebuildable concept-graph view —
  ADR-0013), and `dream` (superseded — kept for history).
- `.claude/skills/` — `/ratchet-review` (the human gate's interaction), `/ratchet-generate`
  (craft the projection's wording with Claude; every rule stays faithful to its concept), and
  `/ratchet-research` (turn a research session into an incubating document source, through the
  same gate as any other claim).
- `docs/RUNBOOK.md` — the operator loop: order, cadence, budgets, knobs.
- `docs/decisions/` — dated ADRs. A decision is superseded by a **new** ADR, never edited.

The three skills are the author's own personal sittings, not a fixed API: copy them into your
own `~/.claude/skills/`, then edit the reviewer's name and the `nix run ~/ratchet#…` paths to
your clone's location. Every stage above is fully operable from the CLI without them — the
skills are the ergonomic layer, not a requirement.

## Run

Every stage is `nix run .#<stage> -- <args>` (in the dev shell: `python -m ratchet.<stage>`):

```
nix run .#status                                     # first, every session: where does the backlog sit?
nix run .#tap -- --last 200                          # pull new transcripts
nix run .#glean -- --all --limit 1000 --max-usd 5    # extract events (Haiku)
nix run .#resolve -- --limit 100 --max-usd 1         # match or mint claims — run often
nix run .#review -- --pending                        # a sitting's worth — or guided: /ratchet-review
nix run .#generate -- --diff --target ~/.claude/CLAUDE.md
```

**`docs/RUNBOOK.md` is the operator manual** — the full loop, the cadence table, seeding
hand-written rules, every knob. Two read-side instruments ride alongside the pipeline:
`concepts` renders the derived concept graph and digest (recomputed from blobs, no LLM), and
`sig` is the measuring CLI that earned the resolver's thresholds (`--band-report`,
`--sample-pairs`, `--score-gold`; never writes).

## First hour

Day one is quiet by design — expect it, don't read it as broken:

```
nix run .#tap -- --last 10               # your 10 most recent sessions
nix run .#weave -- --all                 # $0
nix run .#chunk -- --all                 # $0
nix run .#glean -- --all --max-usd 2     # extract events (Haiku)
nix run .#resolve -- --limit 200         # match or mint claims
nix run .#status
nix run .#review -- --pending
```

On the author's corpus, gleaning runs ≈$0.06/chunk and yields ≈3 events/chunk. But a claim
matures only once it's corroborated across 2+ distinct, recent sessions, so a fresh store —
tapped shallow — shows everything still **incubating**: `--pending` comes back empty, and that
is the design working, not a stall. `nix run .#review -- --incubating` shows exactly what's
accruing and why it hasn't crossed the bar yet. The first real review sitting typically arrives
after a few days of ordinary sessions, or a wider `tap --last` — not the first hour.

`docs/RUNBOOK.md` has the full operating loop: cadence, budgets, every knob.

## Develop

From the dev shell (picks up uncommitted changes):

```
direnv allow                                             # or: nix develop
for t in tests/test_*.py; do python "$t" || break; done  # bash
for t in tests/test_*.py; python $t; or break; end       # fish
```

Every test is a plain stdlib script — no framework, a throwaway `RATCHET_DATA_DIR`, fake
completers for the LLM stages, golden files under `tests/golden/` pinning exact derived output.
`RATCHET_LIVE_TEST=1` additionally smoke-tests the real `claude` CLI.
