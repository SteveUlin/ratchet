# 0025 — tap does not eat its own tail (self-ingestion exclusion)

- Status: accepted — implemented 2026-06-30 (all offline suites green, incl. the new `test_tap` §9)
- Date: 2026-06-30
- Supersedes: —
- Superseded by: —
- Extends: ADR-0002 (tap the fetcher), ADR-0022 (the operator's fetch-selection knobs).

Code (`tap.encode_project`; `tap.discover(exclude=, skip_self=)`; `TapBlock(exclude=, skip_self=)`;
`tap` CLI `--exclude`/`--include-self`, default `skip_self=config.data_root()`) is the source of truth;
this records the *why*.

## Context

ratchet's LLM stages call the model through the authed `claude` CLI (`completer.py`), run with
`cwd = config.data_root()` so the cached prompt prefix stays byte-stable. But Claude Code logs **every**
`claude -p` invocation as a session transcript under `~/.claude/projects/<encode(cwd)>/`. So each `glean`
and `dream` call writes a transcript into `encode_project(data_root)` — `-home-sulin--local-share-ratchet`
— and the next `tap --all` ingests ratchet's own extract/route PROMPTS as if they were sessions worth
mining. The pipeline reads its own exhaust.

This is not hypothetical. A first end-to-end trial (2026-06-30) tapped the 30 most-recent sessions and found
**24 of 30 were ratchet's own completer calls** — 88% of the resulting glean backlog was self-generated
noise, indistinguishable downstream except by content signature. Left unfixed, every operating tick spends
LLM budget re-reading the previous tick's machinery, and the noise compounds.

A related, smaller annoyance: integration-test fixtures (`-tmp-…ratchet-test-glean`, `-tmp-ratchet-itest-data`)
also live as top-level project dirs and, being freshly written, dominate a `--last N` window.

`discover()` is non-recursive (`proj_dir.glob("*.jsonl")` over immediate children), so nested
subagent/workflow transcripts are already excluded for free. The gap is only these *top-level* dirs.

## Decision

**`tap` skips, by default, the project dir its own completer runs land in** — and exposes a general
substring exclude for everything else.

1. **`encode_project(path)`** reproduces Claude Code's cwd→dir-name convention (`re.sub(r"[/.]", "-", path)`;
   `/home/sulin/.local/share/ratchet` → `-home-sulin--local-share-ratchet`). This couples to a Claude Code
   naming convention — deliberately a read-side *heuristic*, not a contract.

2. **`discover(..., skip_self=data_root)`** drops the dir whose name equals `encode_project(data_root)` and
   any `…-`-prefixed child (a completer run from a nested cwd under the data root). `tap`'s CLI defaults
   `skip_self = config.data_root()`, derived from config so it follows `RATCHET_DATA_DIR` anywhere.

3. **`--include-self`** lifts the skip (`skip_self=None`). **`--exclude SUBSTR`** (repeatable) drops any dir
   whose name contains the substring — e.g. `--exclude -tmp-` for test fixtures.

## Why this shape

- **Prevent, don't detect.** The alternative — letting the runs in, then filtering by content signature
  ("does this look like a glean prompt?") — is fragile (signatures drift with the prompt) and wastes the
  read. Skipping the *source dir* is deterministic and zero-cost (no file read; it's a dir-name check).

- **Default-on with an escape hatch, not a hard wall.** The skip is the right default — a self-ingesting
  loop is never what the operator wants — but it rests on a naming heuristic that could someday be wrong, so
  `--include-self` makes it lift-able and the help text says *why* it exists. (This mirrors ratchet's broader
  turn away from hidden hard rules toward explained, overridable defaults.)

- **Derived from config, not hardcoded.** `skip_self=config.data_root()` means a custom `RATCHET_DATA_DIR`
  is still recognized; there is no literal project name baked into tap.

- **Keep `cwd=data_root`.** Redirecting the completer's transcripts elsewhere (e.g. an isolated
  `CLAUDE_CONFIG_DIR`) would also stop the pollution but risks losing the CLI's auth + the byte-stable
  cached prefix. Fixing it on the *read* side leaves the proven completer path untouched.

## Consequences

- A no-flag `tap` no longer ingests ratchet's own `claude -p` runs; `--last N` is no longer hijacked by
  whichever stage ran most recently. Scope stays controllable with `--project` / `--exclude`.
- The coupling to Claude Code's dir-naming is now load-bearing for the default skip. If that convention
  changes, the skip silently stops matching (fails *open* — back to today's behavior, not a crash), and
  `--exclude` remains as a manual fallback. Documented here so the coupling is not a surprise.
- Does not touch already-ingested blobs; a corpus tapped before this still contains self-runs (prune or
  re-tap a fresh data dir).
