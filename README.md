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

## Layout

- `ratchet/` — the Python package: `config`, `blobstore`, `tap`, `weave`, `chunk`.
- `docs/decisions/` — dated ADRs. A decision is superseded by a **new** ADR, never edited.

## Data

The blobstore lives **outside** this repo, in `$RATCHET_DATA_DIR`
(default `${XDG_DATA_HOME:-~/.local/share}/ratchet`). It is append-only and local-only; the
repo holds only code.

## Run

```
nix run .#tap -- --dry-run          # show which transcripts would be copied
nix run .#tap                       # copy new transcripts into the blobstore

python -m ratchet.weave --source-id <session>            # inspect a session's cleaned render
python -m ratchet.weave --source-id <session> --render   # print the full cleaned document
python -m ratchet.chunk --source-id <session>            # materialize + list its chunkset
```

## Develop

From the dev shell (picks up uncommitted changes):

```
direnv allow                        # or: nix develop
python -m ratchet.tap --dry-run
python tests/test_storage.py && python tests/test_tap.py && \
  python tests/test_weave.py && python tests/test_chunk.py
```
