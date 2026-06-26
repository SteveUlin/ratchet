# ratchet

Small, composable tools that mine durable learnings from Claude Code sessions and turn them
into reviewable improvements to your config. Plain files, no database, no daemon.

The unifying `ratchet` service does not exist yet — the project is built as composable blocks.
The first block is the **blobstore** and the tool that fills it:

- `tap` — locate new/changed Claude Code transcripts and copy them into the blobstore.

## Layout

- `ratchet/` — the Python package: `config`, `blobstore`, `tap`.
- `docs/decisions/` — dated ADRs. A decision is superseded by a **new** ADR, never edited.

## Data

The blobstore lives **outside** this repo, in `$RATCHET_DATA_DIR`
(default `${XDG_DATA_HOME:-~/.local/share}/ratchet`). It is append-only and local-only; the
repo holds only code.

## Run

```
nix run .#tap -- --dry-run          # show which transcripts would be copied
nix run .#tap                       # copy new transcripts into the blobstore
```

## Develop

From the dev shell (picks up uncommitted changes):

```
direnv allow                        # or: nix develop
python -m ratchet.tap --dry-run
python tests/test_storage.py && python tests/test_tap.py
```
