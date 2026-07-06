# ratchet

A personal knowledge pipeline: it mines Claude Code transcripts — and documents and webpages —
into human-reviewed concepts, projected into CLAUDE.md. Single-user by design: the human gate IS
the trust model. README.md is the entry point, docs/RUNBOOK.md the operating manual,
docs/decisions/ the why (ADRs 0001+).

# Invariants

Violating any of these is a bug, not a style choice.

- **Every artifact is a content-addressed, immutable blob** (ADR-0007). There is no update or
  delete — the blobstore enforces append-only by API absence. Never touch `blobs/` around it.
- **State is a fold, never a field.** Lifecycle = the latest append-only decision blob referencing
  a target; retraction, reopen, and reject-merge are new decisions that derived folds read. If a
  feature seems to need a status flag on a blob, the design is wrong.
- **The human gate is the only trust source** (ADR-0008). LLM output — a `why`, a rationale, a
  merge — stays untrusted until a human verdict. Evidence quotes are bytes copied from immutable
  blobs: code points at lines, it never retypes them (ADR-0026), so a quote cannot be hallucinated.
- **Knobs are explained, never hidden** (ADR-0027). A threshold is a named module constant wearing
  its rationale — and its measurement, when one exists — surfaced as a CLI flag with an escape
  hatch. A flag that binds nothing is a lie: wire it or drop it. Refuse loudly; never silently
  reinterpret.
- **Pure stdlib.** No dependencies, no AI embeddings — deterministic feature math (`sig.py`) over
  model magic. The one LLM seam is `completer.py` (`claude -p`); never call a model around it.
- **Every stage is a Block on the one driver** (ADR-0009): per-item commit with the marker written
  last, resumable, budget-capped. A new stage joins the driver; it does not grow its own loop.

# Style

- A comment states the design pressure — the constraint the code cannot show — never what the next
  line does. A docstring claims only what code enforces; a documented property with no enforcing
  mechanism is the house's cardinal sin.
- Present tense, always: code describes what IS. History lives in jj and the ADRs — a comment
  saying "now" or "the old X" is rot the next reader must excavate.

# Working here

- Version control is **jj**, never git. Descriptions are imperative one-liners optimized for
  `jj log`; no conventional-commit prefixes.
- A design decision gets an ADR in docs/decisions/ — read two neighbors first for the voice
  (Context → Decision → Why this shape → Consequences). Operating changes land in the RUNBOOK.
- Tests are plain scripts, no framework: `tests/test_<name>.py` with a throwaway
  `RATCHET_DATA_DIR`, fake completers at the `claude -p` seam, and golden files pinning rendered
  bytes. The whole suite is the gate:

      for t in tests/test_*.py; do python "$t" || break; done      # bash
      for t in tests/test_*.py; python $t; or break; end           # fish

- Never point a test or an experiment at the live store (`~/.local/share/ratchet`); tap skips
  ratchet's own data-dir project so the pipeline cannot eat its tail (ADR-0025).
- The skills under `.claude/skills/` are the human-facing surface. Their shared discipline: one
  card, one verdict; cards speak plain language — ids are for commands, not prose.
