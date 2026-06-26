# ratchet

Small, composable tools that mine durable learnings from Claude Code sessions and turn
them into reviewable improvements to your config. Plain files, no database, no daemon.

The unifying `ratchet` service does not exist yet. Today there are individual,
use-case-named tools:

- `glean` — extract durable learnings from a Claude Code transcript into the event store.

## Layout

- `ratchet/` — the Python package: config, event model, store, sources, tools.
- `docs/decisions/` — dated ADRs. A decision is superseded by a **new** ADR, never edited.

## Data

Events live **outside** this repo, in `$RATCHET_DATA_DIR`
(default `${XDG_DATA_HOME:-~/.local/share}/ratchet`). They are append-only and local-only;
the repo holds only code.

## Develop

```
nix develop
python -m ratchet.glean path/to/session.jsonl        # extract learnings
python -m ratchet.glean path/to/session.jsonl --dry-run   # show chunks, no API calls
```
