# ratchet

Small, composable tools that mine durable learnings from Claude Code sessions and turn them
into reviewable improvements to your config. Plain files, no database, no daemon.

The unifying `ratchet` service does not exist yet — the project is built as composable blocks
over a content-addressed **blobstore**, each step a materialized, lineage-linked artifact:

```
tap → raw → weave → cleaned → chunk → chunkset → glean → events → dream → takeaways → review → concepts
  (fetch)      (render)            (window)        (extract, LLM)    (synthesize, LLM)   (human gate)
```

- `tap` — locate new/changed Claude Code transcripts and copy each in as an immutable **raw** blob.
- `weave` — reconstruct a transcript's active conversation (across rewinds, compacts, parallel
  tool calls; sidechains dropped) and render it to one **cleaned** blob of speaker-tagged text.
  Deterministic, no LLM.
- `chunk` — window a cleaned blob into a **chunkset**: bounded, provenance-tagged byte-offset
  pointers an extractor consumes. A chunk resolves by slicing the immutable cleaned blob — no
  re-render. Deterministic, no LLM.
- `glean` — filter chunks for durable signal and extract **events**: thin pointers into the cleaned
  blob (a byte span whose verbatim quote is *trusted*, plus an *untrusted* one-sentence summary and
  scored *markers* — surprise / insight / research — that classify why the learning matters). A quote
  is accepted only if it is a real substring of the chunk, so a hallucinated quote is rejected
  deterministically. Events are an append-only log, not blobstore blobs.
- `dream` — cluster events and synthesize each cluster into a durable, evidence-cited **takeaway**
  (a "why" + name). Deterministic stdlib clustering first, then one LLM call per cluster with a
  *sharper* model (sleep-time: rare, batched). A takeaway cites its events, extending the trust chain
  to the immutable blob. Takeaways **evolve by supersession** — a re-run re-clusters and a new takeaway
  supersedes those it re-covers (grow/split/merge are one mechanism); "current" is a derived fold.
- `review` — the **human gate**: promote takeaways to **concepts** (ratchet's curated knowledge).
  `review.py` is the pure backend; the interaction is the `/ratchet-review` skill, where Claude is an
  active faithfulness-checker (the `why` is untrusted — Claude checks it against the verified evidence
  and escalates to investigate the doubtful cases) and you make every call. accept/reject/snooze/edit
  are append-only decision blobs; an accept mints a concept, closing the loop (dream reads concepts to
  judge belief-change). The queue and the valid concept set are derived queries, never stored lists.
- `concepts` — the **concept-graph view**: derived edges + clusters between concepts from PROVENANCE
  FACETS (shared repo/file/tool, temporal proximity — recomputed on read from each cited session's raw
  transcript, never stored), NO LLM and NO text similarity (ADR-0013). A rebuildable read-side view; the substrate
  the gardener (managed tags, structural ops) is built on.
- `garden` — the **gardener, phase 1**: a cheap model tags each concept from a gardener-managed,
  controlled **vocabulary** (both a derived fold over append-only blobs — concepts stay immutable), and a
  shared **tag** becomes a `shares-tag` edge that sharpens the `concepts` graph. LOW-STAKES, auto-applied
  (no review); a `block.Block` reusing the whole driver (ADR-0014). Structural ops + vocab curation = 3c.

## Layout

- `ratchet/` — the Python package: `config`, `blobstore` (every artifact is a content-addressed,
  versioned blob — ADR-0007), `block` (the one driver every stage runs on — ADR-0009), `tap`, `weave`,
  `chunk`, `glean`, `dream`, `review`, `concepts` (the rebuildable concept-graph view — ADR-0013),
  `garden` (the gardener's managed-tags pass over concepts — ADR-0014), and `completer` (the injected
  LLM seam).
- `.claude/skills/ratchet-review/` — the `/ratchet-review` skill (the human gate's interaction).
- Every batch stage (tap/weave/chunk/glean/dream) is a **Block**: same `--all`/`--source-id`/`--max-usd`/
  `--limit`/`--dry-run`/`--quiet` CLI, same streaming per-item progress, same per-item commit + resume.
- `docs/decisions/` — dated ADRs. A decision is superseded by a **new** ADR, never edited.

## Data

The data lives **outside** this repo, in `$RATCHET_DATA_DIR`
(default `${XDG_DATA_HOME:-~/.local/share}/ratchet`), append-only and local-only; the repo holds
only code. The **blobstore** (`blobs/`) holds deterministic, content-addressed artifacts (raw +
cleaned + chunkset). The **stream store** (`events/glean-*.jsonl`, `events/dream-*.jsonl`) is a
separate append-only log of non-deterministic LLM output that *points into* the blobstore — each
event/takeaway verifiable forever against its frozen cleaned blob. `concepts/` is the curated-
knowledge layer the human-review gate will write and `dream` reads.

## Run

```
nix run .#tap -- --dry-run          # show which transcripts would be copied
nix run .#tap                       # copy new transcripts into the blobstore

python -m ratchet.weave --source-id <session>            # inspect a session's cleaned render
python -m ratchet.weave --source-id <session> --render   # print the full cleaned document
python -m ratchet.chunk --source-id <session>            # materialize + list its chunkset

nix run .#glean -- --source-id <session> --dry-run       # show which chunksets would be gleaned
nix run .#glean -- --source-id <session>                 # extract events (default model: haiku)
nix run .#glean -- --all --max-usd 2.00                  # glean every chunkset, capped at $2

nix run .#dream -- --dry-run                             # cluster events, print groupings (no LLM)
nix run .#dream -- --show --max-usd 2.00                 # synthesize takeaways (default model: sonnet)

nix run .#review -- --pending                            # the review queue (takeaways + verified evidence)
nix run .#review -- --accept <takeaway> --assessment ".."  # promote a takeaway → concept
```

The review gate is meant to be driven by the **`/ratchet-review` skill** (Claude presents each
takeaway with its verified evidence, checks the untrusted `why`, and records your verdict); the CLI
above is the same backend it calls.

`glean` and `dream` are the LLM stages — by default they shell out to your authed `claude` CLI.
Both re-run idempotently (a processed ledger skips done work); a bumped prompt or `--model` re-does
it over the same frozen inputs. `dream --dry-run` shows the deterministic clustering for free, so you
can eyeball the groupings before spending on synthesis.

## Develop

From the dev shell (picks up uncommitted changes):

```
direnv allow                        # or: nix develop
python -m ratchet.tap --dry-run
python tests/test_storage.py && python tests/test_tap.py && python tests/test_block.py && \
  python tests/test_weave.py && python tests/test_chunk.py && \
  python tests/test_glean.py && python tests/test_dream.py && python tests/test_review.py
```

`test_glean.py` and `test_dream.py` run offline with fake completers; set `RATCHET_LIVE_TEST=1` to
also smoke-test the real `claude` CLI against one transcript.
