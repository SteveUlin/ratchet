"""garden — the gardener over the concept layer, in three separable concerns (ADR-0014/0015/0016/0024):

  ops      — the LLM-FREE machinery (3c-i): asserted edges, tag-vocab curation, the deterministic
             structural ops (merge/split/abstract/reparent/retire + merge_tags/retire_tag), the
             `op_stakes` gradient, and the append-only proposal QUEUE.
  tag      — the tagging Block (3b): a cheap-AI grouping signal over concepts (`GardenBlock`).
  propose  — the structural-op proposer (3c-ii) + the deterministic decay/staleness pass (3c-iii).

This package re-exports the FLAT `ratchet.garden.X` surface every caller (the tests, `concepts`,
`review`) reaches for — `import ratchet.garden as garden` exposes the same names the single-module
`garden.py` did before the split. `slugify` (the tag-slug hygiene point) is hosted HERE because >1
submodule needs it and it must resolve before any of them imports it.
"""
from __future__ import annotations

import re

# --- the shared tag-slug hygiene point (ops + tag + propose all slugify) ---------------------------
# Defined BEFORE the submodule re-exports below so each submodule's `from . import slugify` resolves
# against this partially-initialised package (slugify is bound before `from .ops import …` triggers it).
SLUG_MAX = 40   # a tag slug is a short kebab-case identifier, not a sentence


def slugify(s) -> str:
    """Normalize an untrusted proposed tag to a short kebab-case slug — the single hygiene point for the
    controlled vocabulary (mirrors dream's defensive `clean_score`/`_clean_route`). Lowercase, keep only
    `[a-z0-9-]` (everything else → `-`), collapse/trim dashes, cap length. Empty → "" (the caller drops
    it, never minting a blank tag)."""
    s = re.sub(r"[^a-z0-9]+", "-", str(s).strip().lower()).strip("-")
    return s[:SLUG_MAX].strip("-")


# --- re-export the flat pre-split surface ---------------------------------------------------------
from .ops import (                                   # noqa: E402  (must follow slugify's definition)
    ASSERTED_EDGE_KINDS, AUTO_APPLY_MAX_STAKES, PROPOSAL_KIND, RESOLVE_VERBS,
    abstract, assert_edge, asserted_edges, edge_id, merge, merge_tags, mint_proposal_id,
    op_stakes, open_proposals, queue_proposal, reparent, retire, retire_tag, retract_edge,
    split, tag_curation, _concept_blob, _mint_op_concept_id, _proposal_blob,
)
from .tag import (                                   # noqa: E402
    ASSIGN_PREFIX, VOCAB_MAX, GardenBlock, add_tag, all_concept_tags, assign_tags, concept_tags,
    main, run, vocab_fingerprint, vocabulary, _clean_assigned, _clean_new_tags,
)
from .propose import (                               # noqa: E402
    STALENESS_DAYS, GardenProposeBlock, concept_last_corroborated, propose_main, propose_stale,
    run_propose, stale_concepts, _clean_op, _op_stakes_of,
)
