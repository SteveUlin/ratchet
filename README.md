# ratchet

Small, composable tools that mine durable learnings from Claude Code sessions and turn them
into reviewable improvements to your config. Plain files, no database, no daemon.

The unifying `ratchet` service does not exist yet — the project is built as composable blocks
over a content-addressed **blobstore**, each step a materialized, lineage-linked artifact:

```
tap → raw blob → weave → cleaned blob → chunk → chunkset → glean → events
   (fetch)            (render)              (window)           (extract, LLM, future)
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
  scored *markers* — surprise / insight / research — that classify why the learning matters). The
  only LLM stage. A quote is accepted only if it is a real substring of the chunk, so a hallucinated
  quote is rejected deterministically. Events are an append-only log, not blobstore blobs.

## Layout

- `ratchet/` — the Python package: `config`, `blobstore`, `tap`, `weave`, `chunk`, `glean`, and
  `completer` (the injected LLM seam — a `Completion` + the default `claude` CLI binding).
- `docs/decisions/` — dated ADRs. A decision is superseded by a **new** ADR, never edited.

## Data

The data lives **outside** this repo, in `$RATCHET_DATA_DIR`
(default `${XDG_DATA_HOME:-~/.local/share}/ratchet`), append-only and local-only; the repo holds
only code. The **blobstore** (`blobs/`) holds deterministic, content-addressed artifacts (raw +
cleaned + chunkset). The **event store** (`events/glean-*.jsonl`) is a separate append-only log of
non-deterministic LLM output that *points into* the blobstore — each event verifiable forever
against its frozen cleaned blob.

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
```

`glean` is the only step that calls an LLM — by default it shells out to your authed `claude` CLI.
It re-runs idempotently (a processed ledger skips done chunksets); a bumped prompt or `--model`
re-extracts over the same frozen chunks.

## Develop

From the dev shell (picks up uncommitted changes):

```
direnv allow                        # or: nix develop
python -m ratchet.tap --dry-run
python tests/test_storage.py && python tests/test_tap.py && \
  python tests/test_weave.py && python tests/test_chunk.py && python tests/test_glean.py
```

`test_glean.py` runs offline with a fake extractor; set `RATCHET_LIVE_TEST=1` to also smoke-test the
real `claude` CLI against one transcript.
