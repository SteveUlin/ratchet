"""garden — the gardener Block, phase 1: MANAGED TAGS, a cheap-AI grouping signal over concepts (ADR-0014).

    … concepts (3a facet substrate) → [GARDEN: a cheap tagger over a managed vocab] → a sharper graph

3a (ADR-0013) gave the concept layer PROVENANCE facets — two concepts that wrote the same file/repo/tool
are related — but nothing groups concepts that share a *meaning* without sharing a file (two different
files, one lesson about version control). This stage adds the missing semantic axis as a LOW-STAKES,
AUTO-applied signal: a cheap model assigns each concept tags from a CONTROLLED VOCABULARY the gardener
curates, and a shared tag becomes a `shares-tag` edge that sharpens 3a's clustering. It is the same
LLM-Block shape as dream's `route` (one cheap call per item over an in-prompt catalog — no embeddings),
reusing the whole `block.run` driver (budget/limit/resume/priority).

Everything is the BLOB model (ADR-0007), state DERIVED by folding append-only artifacts — never a stored
mutable set:

  VOCABULARY — a derived fold over append-only `tag` blobs (source_id = the slug). `vocabulary(root)`
    folds `latest_by_kind('tag')` to {slug: gloss} — the current controlled tag set. Starts EMPTY; grows
    as the tagger proposes new tags (auto-added, low-stakes). Tag merge/retire — the vocab's own curation
    — is deferred to 3c; here the vocab only ever grows.

  ASSIGNMENTS — append-only per-concept `tag_assignment` blobs (source_id = `ct-`+concept_id), LATEST-WINS.
    `concept_tags(concept_id, root)` folds to that concept's CURRENT tags. The concept BLOB stays IMMUTABLE
    — tags never re-version a concept (a concept is freely re-taggable as the vocab grows, independently of
    what it asserts). The assignment CONTENT is run-invariant ({concept_id, tags, vocab_fingerprint}); the
    producer/cost ride in `origin_ref`, so a crash-retry re-ingests byte-identically (no churn), exactly
    like dream's `takeaway_content`.

  THE BLOCK — `GardenBlock` is a `block.Block` (ADR-0009): `items()` = the valid concepts; the driver's
    done-skip + a VOCAB FINGERPRINT in `params` give idempotency — a concept whose latest tagging ran
    against the CURRENT vocab is skipped, and any vocab change (the fingerprint flips) re-tags everything
    (like glean's `prompt_version`). `process()` is ONE cheap tagger call over {title + statement + the 3a
    provenance facets as context + the FROZEN vocabulary} → assign tags + optionally propose new ones;
    it commits the assignment and auto-adds proposed tags. The vocab is read ONCE frozen at run start
    (new tags this run apply NEXT run — keeps per-item commit + resume clean). `priority()` is
    untagged-first then facet-rich-first (ADR-0011's modular signal).

This is purely additive over 3a: an untagged concept yields no tags → no `shares-tag` edge → the 3a graph
is byte-identical (the golden stands). The structural ops (merge/split/abstract/retire of concepts AND of
tags) are the gardener's phase 2 (3c) — they ACT ON this signal; tagging here only PRODUCES it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from itertools import combinations
from pathlib import Path

from . import blobstore, block, completer, concepts, config, dream, review
from .completer import Completer
from .concepts import concept_facets

PROMPT_VERSION = "garden/1"             # bump to re-tag every concept with a sharper prompt (idempotency key)
TAG_MODEL = "haiku"                     # tagging is cheap + per-concept → the small model (dream's router seat)
OUT_NOUN = "assignments"               # the per-item output noun the Progress bar/line shows

SLUG_MAX = 40                          # a tag slug is a short kebab-case identifier, not a sentence
GLOSS_MAX = 120                        # the one-line meaning of a tag
TAGS_PER_CONCEPT_MAX = 6              # a concept is grouped by a FEW tags; more is noise, not signal
NEW_TAGS_PER_CALL_MAX = 3            # propose SPARINGLY — a controlled vocabulary works only while small
VOCAB_MAX = 256                       # HARD cap on the controlled vocabulary. The no-embeddings design
                                       # renders the WHOLE vocab into EVERY prompt, so "small, in-prompt" is
                                       # a load-bearing INVARIANT, not a nicety — once the frozen vocab hits
                                       # this size, new-tag proposals are DROPPED (reuse-only from then on).
                                       # Generous (sized never to fire on the happy path); the backstop that
                                       # bounds BOTH the re-tag amplification worst case (a vocab change
                                       # re-tags every concept) and vocab bloat until 3c's real curation
                                       # (tag merge/retire) lands.

ASSIGN_PREFIX = "ct-"                  # tag_assignment source_id = ASSIGN_PREFIX + concept_id — a DISTINCT
                                       # source_id namespace from the concept blob (source_id = concept_id),
                                       # so the kind-agnostic `latest_version` fold never entangles the two.

TAG_KIND = "tag"
ASSIGN_KIND = "tag_assignment"

TAG_SYSTEM = (
    "You are the TAGGER for a developer's long-term memory. Each CONCEPT is a durable, human-reviewed "
    "lesson. Assign it tags from a CONTROLLED VOCABULARY so concepts about the same THEME group together "
    "— even when they touch different files.\n"
    "  - Assign the EXISTING vocabulary tags that genuinely apply. PREFER reusing the vocabulary: reuse "
    "is what makes a tag group things; a near-duplicate new tag fragments the grouping.\n"
    "  - Only if NO existing tag captures an important theme of the concept, PROPOSE a new tag: a short "
    "lowercase kebab-case slug (e.g. \"version-control\") plus a one-line gloss. Propose SPARINGLY.\n"
    "Assign only tags that truly fit — a concept may carry several tags or, occasionally, none. The "
    "concept's provenance (repos/files/tools) is CONTEXT for what it is about, not the lesson itself.\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{"tags": ["slug", ...], "new_tags": [{"slug": "...", "gloss": "..."}]}\n'
    'where "tags" lists EVERY tag you assign (existing ones plus any you propose), and "new_tags" '
    "declares each proposed slug with its gloss."
)


# --- the vocabulary: a derived fold over append-only `tag` blobs ----------------------------------

def slugify(s) -> str:
    """Normalize an untrusted proposed tag to a short kebab-case slug — the single hygiene point for the
    controlled vocabulary (mirrors dream's defensive `clean_score`/`_clean_route`). Lowercase, keep only
    `[a-z0-9-]` (everything else → `-`), collapse/trim dashes, cap length. Empty → "" (the caller drops
    it, never minting a blank tag)."""
    s = re.sub(r"[^a-z0-9]+", "-", str(s).strip().lower()).strip("-")
    return s[:SLUG_MAX].strip("-")


def vocab_fingerprint(vocab: dict[str, str]) -> str:
    """A short STABLE hash of the frozen vocabulary — the idempotency param (like glean's `prompt_version`).
    A concept tagged against this exact vocab is done-skipped; ANY change to the slug set or a gloss flips
    the fingerprint, so the done-key changes and every concept re-tags next run (the vocab grew → re-judge
    everything against it). Keyed on the sorted (slug, gloss) pairs via `canonical_json` so it is order- and
    bytes-stable; an empty vocab hashes to a fixed sentinel, so the first-ever run has a well-defined key."""
    body = blobstore.canonical_json(sorted(vocab.items()))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def vocabulary(root: Path | None = None) -> dict[str, str]:
    """The current controlled tag set — {slug: gloss} — folded from `latest_by_kind('tag')` (latest
    version per slug, ADR-0007). Never a stored set: the vocab IS this fold over the append-only `tag`
    blobs, exactly like `catalog`/`load_concepts`. Empty until the tagger proposes its first tag. A
    malformed/absent tag blob is skipped, never fatal.

    Tag CURATION (3c/ADR-0015) folds the vocab DOWN: a `merge_tags`/`retire_tag` loser is dropped from
    the set here (the redirect is its own append-only `tag_curation` blob — no tag blob is rewritten), so
    a near-duplicate or dead slug leaves the controlled vocabulary without a deletion."""
    root = root or config.data_root()
    curated = tag_curation(root)            # loser_slug -> winner|None; a curated-away loser leaves the set
    out: dict[str, str] = {}
    for slug, h in blobstore.latest_by_kind(TAG_KIND, root).items():
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("slug") and obj["slug"] not in curated:
            out[obj["slug"]] = str(obj.get("gloss", ""))
    return out


def add_tag(slug: str, gloss: str, root: Path | None = None, *, run_id: str, model: str,
            cost: float = 0.0) -> tuple[str, bool]:
    """Append one tag to the controlled vocabulary — a `tag` blob keyed on the slug (ADR-0007). The
    CONTENT is run-invariant ({slug, gloss}), so re-proposing the same slug+gloss is a byte-identical
    no-op (the vocab never churns); the producer/cost ride in `origin_ref`. Returns (hash, written) —
    `written` is False for an already-present identical tag, which is how `process` counts only genuine
    growth. Low-stakes + AUTO (no review): the vocab is gardener-curated LATER (merge/retire = 3c)."""
    body = blobstore.canonical_json({"slug": slug, "gloss": str(gloss)[:GLOSS_MAX]})
    return blobstore.ingest(body, source_kind=TAG_KIND, source_id=slug,
                            origin_ref={"stage": "garden", "model": model, "prompt_version": PROMPT_VERSION,
                                        "run_id": run_id, "cost_usd": round(cost, 8)}, root=root)


# --- assignments: append-only per-concept tags, latest-wins ---------------------------------------

def assign_tags(concept_id: str, tags: list[str], fingerprint: str, root: Path | None = None, *,
                run_id: str, model: str, cost: float = 0.0) -> tuple[str, bool]:
    """Commit ONE concept's tag assignment as a `tag_assignment` blob VERSION keyed on `ct-`+concept_id
    (latest-wins; the concept blob itself is NEVER touched). CONTENT = run-invariant {concept_id, tags
    (sorted, unique), vocab_fingerprint} so a crash-retry re-ingests byte-identically (no churn), like
    dream's `takeaway_content`; the `vocab_fingerprint` records WHICH vocab this tagging judged against,
    so a later read can tell a current assignment from a stale one. Producer/cost live in `origin_ref`."""
    body = blobstore.canonical_json({"concept_id": concept_id, "tags": sorted(set(tags)),
                                     "vocab_fingerprint": fingerprint})
    return blobstore.ingest(body, source_kind=ASSIGN_KIND, source_id=ASSIGN_PREFIX + concept_id,
                            origin_ref={"stage": "garden", "model": model, "prompt_version": PROMPT_VERSION,
                                        "run_id": run_id, "cost_usd": round(cost, 8)}, root=root)


def concept_tags(concept_id: str, root: Path | None = None) -> list[str]:
    """The CURRENT tags of one concept — the latest-wins fold of its `tag_assignment` versions (sorted).
    Single-concept read (the `ct-` source_id is a distinct namespace, so `latest_version` is unambiguous).
    Untagged (no assignment yet) → [] — which threads into 3a facets as no `tags` and so no `shares-tag`
    edge, leaving the 3a graph unchanged. Tag curation (3c/ADR-0015) is applied at READ: a merged slug
    resolves to its winner, a retired one drops — so a concept re-groups under a curated vocab WITHOUT the
    assignment blob being rewritten."""
    root = root or config.data_root()
    h = blobstore.latest_version(ASSIGN_PREFIX + concept_id, root)
    if not h:
        return []
    try:
        obj = json.loads(blobstore.get(h, root))
    except (OSError, json.JSONDecodeError):
        return []
    tags = obj.get("tags") if isinstance(obj, dict) else None
    return _resolve_tags([str(t) for t in tags], tag_curation(root)) if isinstance(tags, list) else []


def all_concept_tags(root: Path | None = None) -> dict[str, list[str]]:
    """concept_id -> its current tags, folded over EVERY `tag_assignment` in one scan — the batch sibling
    of `concept_tags` (the concept graph + `GardenBlock.items` resolve every concept at once). Keyed on the
    body's own `concept_id` (robust against the `ct-` prefix), latest version per source via
    `latest_by_kind`. A concept with no assignment simply has no entry → [] downstream. Tag curation
    (3c/ADR-0015) is folded ONCE and applied to every concept's tags at read (merged slug → winner,
    retired slug → dropped) — the concept graph re-groups under a curated vocab with no assignment rewrite."""
    root = root or config.data_root()
    curated = tag_curation(root)            # fold the redirects ONCE for the whole batch
    out: dict[str, list[str]] = {}
    for h in blobstore.latest_by_kind(ASSIGN_KIND, root).values():
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        cid = obj.get("concept_id")
        tags = obj.get("tags")
        if isinstance(cid, str) and cid and isinstance(tags, list):
            out[cid] = _resolve_tags([str(t) for t in tags], curated)
    return out


# --- the tagger call: one cheap completion → coerced assignment + proposed new tags ---------------

def render_vocabulary(vocab: dict[str, str]) -> str:
    """The in-prompt vocabulary — a slug: gloss list (the WHOLE vocab, no top-K; it stays small by
    design). An empty vocab renders a sentinel so the model is invited to seed the first tags rather than
    asked to choose from nothing."""
    if not vocab:
        return "(empty — propose the first tags)"
    return "\n".join(f"- {slug}: {gloss}" if gloss else f"- {slug}"
                     for slug, gloss in sorted(vocab.items()))


def _tag_user(concept: dict, facets: dict, vocab_rendered: str) -> str:
    # `vocab_rendered` is the FROZEN vocab pre-rendered ONCE per run (a run constant — the vocab never grows
    # mid-run), unlike dream's `render_catalog` which re-renders because its catalog mutates per event.
    f = facets or {}
    ctx = (f"repos={f.get('repos', [])} files={f.get('files', [])} tools={f.get('tools', [])}")
    return (f"CONCEPT\ntitle: {str(concept.get('title', '')).strip()!r}\n"
            f"statement: {str(concept.get('statement', '')).strip()!r}\n"
            f"provenance (CONTEXT, not the lesson): {ctx}\n\n"
            f"VOCABULARY\n{vocab_rendered}")


def _clean_new_tags(raw, vocab: dict[str, str]) -> list[tuple[str, str]]:
    """Coerce the untrusted `new_tags` into [(slug, gloss)] — slugified, non-empty, NOVEL (not already in
    the frozen vocab), deduped, capped at `NEW_TAGS_PER_CALL_MAX`. Defensive like `_clean_relation`: a
    malformed entry is dropped, never minting a blank or duplicate tag.

    The VOCAB_MAX INVARIANT lives here: once the frozen vocab is full, admit NO new tags — reuse-only. The
    whole no-embeddings design renders the WHOLE vocab into every prompt, so a small vocab is load-bearing;
    this is the backstop that holds it until 3c's tag merge/retire curates the vocabulary down."""
    if len(vocab) >= VOCAB_MAX:                           # the frozen vocab is full → reuse-only (drop all)
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        slug = slugify(item.get("slug"))
        if not slug or slug in vocab or slug in seen:
            continue
        seen.add(slug)
        out.append((slug, str(item.get("gloss", "")).strip()[:GLOSS_MAX]))
        if len(out) >= NEW_TAGS_PER_CALL_MAX:
            break
    return out


def _clean_assigned(raw, allowed: set[str]) -> list[str]:
    """Coerce the untrusted `tags` into the assigned slug list — slugified, kept ONLY if in `allowed`
    (the frozen vocab UNION this call's proposed-new slugs), deduped, capped at `TAGS_PER_CONCEPT_MAX`.
    A tag the model names but neither knows nor proposes is a hallucination → dropped (the `_clean_route`
    rule: never act on an id/slug the model invented out of nothing)."""
    out: list[str] = []
    for t in raw if isinstance(raw, list) else []:
        slug = slugify(t)
        if slug and slug in allowed and slug not in out:
            out.append(slug)
        if len(out) >= TAGS_PER_CONCEPT_MAX:
            break
    return out


def tag_concept(concept: dict, vocab: dict[str, str], vocab_rendered: str, fingerprint: str,
                tagger: Completer, *, facets: dict, model: str, run_id: str, root: Path) -> tuple[int, float, dict]:
    """ONE cheap tagger call over {concept + its 3a provenance facets + the FROZEN vocabulary} → coerce →
    commit. Auto-adds the proposed NEW tags to the vocabulary FIRST (low-stakes, so they exist for next
    run), then commits the concept's assignment (the slugs it assigned, including any it proposed — a
    concept that fits nothing both proposes AND gets a fresh tag). Returns (n_out, cost, info) where n_out
    is the ASSIGNMENT count (always 1 — one assignment per concept, so the driver's `outputs` tally matches
    `OUT_NOUN="assignments"`), and `info` is {"assigned": the committed slugs, "n_new": count of
    newly-WRITTEN vocab tags} — the only two fields a reader consumes (n_new feeds the separate `n_new_tags`
    summary; a re-proposed tag is a no-op, uncounted). A raised tagger propagates — the driver isolates the
    concept as errored (no marker → retried next run), exactly like dream's route."""
    comp = tagger(TAG_SYSTEM, _tag_user(concept, facets, vocab_rendered))
    cost = completer.cost_of(comp)
    parsed = completer.parse_json_object(comp.text) or {}
    new_tags = _clean_new_tags(parsed.get("new_tags"), vocab)
    allowed = set(vocab) | {slug for slug, _ in new_tags}
    assigned = _clean_assigned(parsed.get("tags"), allowed)

    tag_writes = 0
    for slug, gloss in new_tags:                          # grow the vocab FIRST (auto, low-stakes)
        _, written = add_tag(slug, gloss, root, run_id=run_id, model=model, cost=cost)
        tag_writes += int(written)
    assign_tags(concept["id"], assigned, fingerprint, root, run_id=run_id, model=model, cost=cost)
    return 1, cost, {"assigned": assigned, "n_new": tag_writes}


# --- the Block: a cheap tagger over the valid concepts, per-concept commit -------------------------

class GardenBlock:
    """The gardener's tagging phase as a `block.Block` (ADR-0009): a cheap tagger over the valid concepts,
    committing PER CONCEPT. `items()` = the valid concepts (`dream.load_concepts`); the driver's done-skip
    plus the VOCAB FINGERPRINT in `params` make it idempotent — a concept already tagged against the
    CURRENT vocab is skipped, and a vocab change (the fingerprint flips) re-tags every concept. `process()`
    runs the one tagger call and commits the assignment + any new tags; the driver writes the per-concept
    `processed` marker LAST (resumable, fail-in-the-middle). `priority()` is untagged-first then
    facet-rich-first.

    The vocabulary is FROZEN once at construction (run start): new tags proposed this run are committed
    immediately (so NEXT run sees them) but do NOT change the fingerprint this run, so the done-key and the
    assignments stay consistent within a run regardless of order. The frozen vocab is only an
    intra-run constant — always re-folded from the store at the next run's start, never a source of truth."""

    name = "garden"
    commits_per_item = True
    finalize = block.no_finalize
    marker_extra = block.no_marker_extra

    def __init__(self, tagger: Completer, *, model: str = TAG_MODEL, root: Path | None = None) -> None:
        self.tagger = tagger
        self.model = model
        self.root = root or config.data_root()
        self.vocab = vocabulary(self.root)             # FROZEN once — the run's constant vocabulary
        self.fingerprint = vocab_fingerprint(self.vocab)
        self._vocab_rendered = render_vocabulary(self.vocab)   # render ONCE — the frozen vocab is a run
                                                       # constant (no per-concept re-render; dream's
                                                       # `render_catalog` must re-render, its catalog mutates)
        # the done-key suffix: a concept is done for (concept_id, PROMPT_VERSION, vocab_fingerprint).
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROMPT_VERSION), ("vocab_fp", self.fingerprint))
        self._current: dict[str, list[str]] = {}       # concept_id -> current tags (for priority)
        self._facet_cache: dict = {}                    # shared cleaned-blob facet cache (priority + process)
        # run-total tallies (instance-scoped; the uniform Report stays stage-agnostic)
        self.n_concepts = 0
        self.n_tagged = 0                               # concepts that got >=1 tag this run
        self.n_new_tags = 0                             # vocabulary entries minted this run
        self.assignments: list[dict] = []               # [{concept_id, tags}] for --show / tests

    def items(self, root: Path, *, source_id: str | None = None):
        """The valid concepts (`dream.load_concepts`, sorted by id) — the driver done-skips those already
        tagged against the current vocab. `source_id` is ignored (garden is a global pass). The current
        tags are folded ONCE here for `priority`."""
        self._current = all_concept_tags(root)
        concepts = sorted(dream.load_concepts(root), key=lambda c: c["id"])
        self.n_concepts = len(concepts)
        return concepts

    def key(self, concept: dict) -> str:
        return concept["id"]

    def priority(self, concept: dict) -> float:
        """UNTAGGED-FIRST, then facet-rich-first (ADR-0011's modular signal). An untagged concept is the
        most valuable to tag (it has no grouping at all); among equals, a facet-rich concept carries more
        provenance context, so the tagger judges it with more to go on. The shared facet cache keeps this
        from re-parsing a cited blob the eventual `process` re-parses."""
        untagged = 1.0 if not self._current.get(concept["id"]) else 0.0
        f = concept_facets(concept, self.root, cache=self._facet_cache)
        # CLAMP the richness tie-breaker below 1.0 so UNTAGGED strictly dominates: an untagged concept
        # (1.0 + 0) always outranks any tagged one (0.0 + <1.0), even one with >100 facet items — the
        # untagged-first TIER must never invert. Ties within a tier stay stable (Greedy is a stable sort).
        richness = min(0.99, 0.01 * (len(f["files"]) + len(f["repos"]) + len(f["tools"])))
        return untagged + richness

    def process(self, concept: dict, *, root: Path, run_id: str) -> tuple[int, float]:
        """One tagger call → commit the concept's assignment + any proposed new tags. Returns
        (n_outputs, cost) for the driver's budget gate; the driver writes the `processed` marker LAST."""
        facets = concept_facets(concept, root, cache=self._facet_cache)
        n_out, cost, info = tag_concept(concept, self.vocab, self._vocab_rendered, self.fingerprint,
                                        self.tagger, facets=facets, model=self.model, run_id=run_id, root=root)
        if info["assigned"]:
            self.n_tagged += 1
        self.n_new_tags += info["n_new"]                # newly-minted vocab tags (reported separately from
                                                        # n_out, which is the per-concept ASSIGNMENT count)
        self.assignments.append({"concept_id": concept["id"], "tags": info["assigned"]})
        return n_out, cost


# --- run: a thin compat shim over the block driver (mirrors glean/dream) ---------------------------

class _ShimReport:
    """The shape `garden.run` returns — a thin WRAPPER over the uniform `block.Report` the driver populated
    plus the GardenBlock instance, exposing every field by reading THROUGH them (no copy → no desync, like
    glean's `_ShimReport`). The uniform fields proxy the Report; the genuinely-extra tallies proxy the block."""
    def __init__(self, report: block.Report, blk: GardenBlock) -> None:
        self._report = report
        self._blk = blk

    @property
    def n_concepts(self) -> int:
        return self._blk.n_concepts
    @property
    def n_tagged(self) -> int:
        return self._blk.n_tagged
    @property
    def n_new_tags(self) -> int:
        return self._blk.n_new_tags
    @property
    def assignments(self) -> list[dict]:
        return self._blk.assignments
    @property
    def fingerprint(self) -> str:
        return self._blk.fingerprint

    @property
    def run_id(self) -> str:
        return self._report.run_id
    @property
    def examined(self) -> int:
        return self._report.examined
    @property
    def processed(self) -> int:
        return self._report.processed
    @property
    def skipped(self) -> int:
        return self._report.skipped
    @property
    def errored(self) -> int:
        return self._report.errored
    @property
    def outputs(self) -> int:
        return self._report.outputs
    @property
    def cost_usd(self) -> float:
        return self._report.cost_usd
    @property
    def stopped_on_budget(self) -> bool:
        return self._report.stopped_on_budget


def run(tagger: Completer, *, model: str = TAG_MODEL, max_usd: float | None = None,
        limit: int | None = None, priority: block.PriorityStrategy | None = None,
        progress: block.Progress | None = None, root: Path | None = None) -> _ShimReport:
    """Tag the valid concepts — a thin shim over `block.run(GardenBlock(...))` (mirrors glean/dream). The
    root is resolved ONCE and handed to BOTH the block (which freezes the vocab + fingerprint off it) and
    the driver, so the params the driver done-skips on match the vocab the block tagged against. `progress`
    defaults to None (silent) so a setup helper doesn't spew per-concept lines."""
    root = config.ensure_layout(root)
    blk = GardenBlock(tagger, model=model, root=root)
    report = block.run(blk, max_usd=max_usd, limit=limit, root=root, priority=priority, progress=progress)
    return _ShimReport(report, blk)


# ==================================================================================================
# STRUCTURAL OPS (3c-i, ADR-0015): the deterministic gardener machinery — NO LLM
#
# 3b PRODUCED a grouping signal (managed tags). 3c ACTS on it: the gardener restructures the concept
# layer. This is the TRUST-CRITICAL append-only foundation the 3c-ii LLM Block will DRIVE and the 3d
# human gate will ACCEPT — so it is purely deterministic here, with the trust chain re-proven on every
# write. Two pieces, both append-only folds (ADR-0007):
#
#   ASSERTED EDGES — a `concept_edge` blob is the gardener's DELIBERATE claim that two concepts relate
#     (generalizes/supersedes/relates-to — ONE canonical hierarchy direction, `generalizes`; its inverse
#     is read by reversing the edge), keyed on the edge identity `src|kind|dst`,
#     latest-wins, retract = a new version with active:false. Distinct from 3a's DERIVED edges, which
#     recompute from provenance on every read and are never stored (ADR-0013 §B realised). `generalizes`
#     defines the hierarchy spine; `concepts.concept_graph` folds the active edges in alongside derived.
#
#   THE OPS — merge/split/abstract/reparent/retire of concepts + merge_tags/retire_tag of the vocab.
#     Every op INGESTS new blobs/decisions ONLY; nothing is ever deleted. A concept leaves the valid set
#     by a `supersede`/`split`/`retire` DECISION (`load_concepts` folds it out; the blob + history stay) —
#     invalidate-don't-delete (Zep/AGM; dream's merge/WEAKEN, ADR-0012). Evidence is UNIONED/subset and
#     RE-VALIDATED on write (review.accept's `_verified_pointers` discipline), so the trust chain — cited
#     evidence → validated span → immutable blob — reaches every minted/versioned concept.
# ==================================================================================================

# The asserted-edge kinds, ONE canonical direction each — `generalizes` is the hierarchy spine (its
# inverse `specializes` is NOT stored: read it by reversing a `generalizes` edge, so the two directions
# can never disagree), `supersedes` is lineage, `relates-to` is association. `assert_edge` REJECTS any
# other kind: in this append-only trust-critical store an unknown kind is a producer bug (a future 3c-ii
# typo), not data to silently fold.
ASSERTED_EDGE_KINDS = ("generalizes", "supersedes", "relates-to")
EDGE_KIND = "concept_edge"             # an asserted inter-concept edge blob (vs 3a's derived edges)
EDGE_SEP = "|"                         # edge identity = src|kind|dst (concept ids + kinds carry no `|`)
TAG_CURATION_KIND = "tag_curation"     # a vocab-curation redirect blob (merge_tags / retire_tag)
NOTE_MAX = 160                         # a short human note on an op/edge (matches dream's NOTE_MAX)


# --- asserted edges: append-only `concept_edge` blobs, latest-wins, retract = active:false --------

def edge_id(src: str, kind: str, dst: str) -> str:
    return f"{src}{EDGE_SEP}{kind}{EDGE_SEP}{dst}"


def assert_edge(src: str, kind: str, dst: str, *, note: str = "", active: bool = True,
                root: Path | None = None, run_id: str, op: str = "assert") -> tuple[str, bool]:
    """Append an asserted edge as a `concept_edge` blob VERSION keyed on its identity `src|kind|dst`
    (ADR-0015). An ASSERTED edge is a first-class append-only artifact — the gardener's DELIBERATE claim
    that two concepts relate — distinct from 3a's DERIVED edges (recomputed from provenance every read,
    ADR-0013 §B). CONTENT is run-invariant ({src,kind,dst,note,active}), so re-asserting an identical edge
    is a byte-identical no-op (idempotent); the producer/op ride in origin_ref. RETRACT = a new version
    with active:false (invalidate-don't-delete; never a deletion). Returns (hash, written).

    REJECTS any `kind` not in `ASSERTED_EDGE_KINDS` (raise) as a fail-safe — in this append-only,
    trust-critical store an unknown edge kind is a PRODUCER bug (a future 3c-ii typo), not data to fold in
    silently and have to invalidate later."""
    if kind not in ASSERTED_EDGE_KINDS:
        raise ValueError(f"assert_edge: unknown kind {kind!r} (allowed: {ASSERTED_EDGE_KINDS})")
    body = blobstore.canonical_json({"src": src, "kind": kind, "dst": dst,
                                     "note": str(note)[:NOTE_MAX], "active": bool(active)})
    return blobstore.ingest(body, source_kind=EDGE_KIND, source_id=edge_id(src, kind, dst),
                            origin_ref={"stage": "garden", "op": op, "run_id": run_id}, root=root)


def retract_edge(src: str, kind: str, dst: str, *, root: Path | None = None, run_id: str,
                 op: str = "retract") -> tuple[str, bool]:
    """Retract an asserted edge — a new version with active:false (invalidate-don't-delete). The blob +
    history stay; the latest-wins fold simply stops surfacing it. Re-retracting is byte-identical → no-op."""
    return assert_edge(src, kind, dst, active=False, root=root, run_id=run_id, op=op)


def asserted_edges(root: Path | None = None, *, active_only: bool = True) -> list[dict]:
    """The current asserted edges — latest version per `src|kind|dst` identity, folded from
    `latest_by_kind('concept_edge')` (ADR-0007), active-only by default. Never a stored set: the live edge
    set IS this fold, exactly like `vocabulary`/`load_concepts`. Sorted for stable bytes; a malformed/absent
    edge blob is skipped, never fatal. `concepts.concept_graph` folds these in alongside the derived edges;
    the active `generalizes` edges define the hierarchy spine."""
    root = root or config.data_root()
    out: list[dict] = []
    for h in blobstore.latest_by_kind(EDGE_KIND, root).values():
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if not (isinstance(obj, dict) and obj.get("src") and obj.get("dst") and obj.get("kind")):
            continue
        if active_only and not obj.get("active"):
            continue
        out.append({"src": obj["src"], "kind": obj["kind"], "dst": obj["dst"],
                    "note": str(obj.get("note", "")), "active": bool(obj.get("active"))})
    out.sort(key=lambda e: (e["src"], e["kind"], e["dst"]))
    return out


# --- tag-vocabulary curation: append-only redirects the vocab/assignment folds honor at READ ------

def tag_curation(root: Path | None = None) -> dict[str, str | None]:
    """loser_slug -> winner_slug (or None = retired) — the latest-wins fold over append-only `tag_curation`
    blobs (ADR-0015), the gardener's vocab-DOWN curation deferred from 3b (ADR-0014 §1). A `merge_tags`
    redirects a near-duplicate slug to its canonical winner; a `retire_tag` drops a dead slug (winner None).
    Both are honored at READ — by `vocabulary` (the loser leaves the set) and the assignment folds
    (`_resolve_tags`) — so NO concept or tag blob is ever rewritten; the redirect is its own append-only,
    reversible (active:false) artifact. A malformed/inactive entry is skipped."""
    root = root or config.data_root()
    out: dict[str, str | None] = {}
    for h in blobstore.latest_by_kind(TAG_CURATION_KIND, root).values():
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("loser") and obj.get("active"):
            out[obj["loser"]] = obj.get("winner")      # a winner slug, or None for a retire
    return out


def _resolve_tag(slug: str, curated: dict[str, str | None]) -> str | None:
    """Follow merge redirects to the terminal winner; a retired slug (winner None) resolves to None
    (dropped). Cycle-safe via a visited guard, so a curation cycle (a→b→a) terminates instead of looping."""
    seen: set[str] = set()
    while slug in curated and slug not in seen:
        seen.add(slug)
        nxt = curated[slug]
        if nxt is None:
            return None
        slug = nxt
    return slug


def _resolve_tags(tags: list[str], curated: dict[str, str | None]) -> list[str]:
    """A concept's stored tags, projected THROUGH the curation redirects (merged → winner, retired →
    dropped), deduped + sorted. Empty curation → the bytes are identical to the raw stored tags (the 3b
    behaviour, so the golden stands)."""
    if not curated:                                    # the common case — no redirect to apply
        return sorted({str(t) for t in tags})
    return sorted({r for t in tags if (r := _resolve_tag(str(t), curated))})


def _write_tag_curation(loser: str, winner: str | None, *, root: Path | None, run_id: str,
                        note: str) -> tuple[str, bool]:
    body = blobstore.canonical_json({"loser": loser, "winner": winner,
                                     "note": str(note)[:NOTE_MAX], "active": True})
    return blobstore.ingest(body, source_kind=TAG_CURATION_KIND, source_id=loser,
                            origin_ref={"stage": "garden", "op": "tag_curation", "run_id": run_id}, root=root)


def merge_tags(loser_slug: str, winner_slug: str, *, note: str = "", root: Path | None = None,
               run_id: str | None = None) -> tuple[str, bool]:
    """MERGE a near-duplicate tag INTO a canonical one — append a `tag_curation` redirect (loser→winner)
    the `vocabulary` fold drops the loser from and the assignment folds (`_resolve_tags`) redirect through.
    NO concept or tag blob is rewritten (append-only); the redirect is its own reversible artifact. This
    FOLDS the vocab down — the symmetric twin of 3b's auto-grow (ADR-0014 §1 → ADR-0015). Returns
    (hash, written). Both slugs are `slugify`d; a self-merge or blank slug is refused."""
    loser, winner = slugify(loser_slug), slugify(winner_slug)
    if not loser or not winner or loser == winner:
        raise ValueError(f"merge_tags needs distinct non-empty slugs: {loser_slug!r} → {winner_slug!r}")
    return _write_tag_curation(loser, winner, root=root, run_id=run_id or config.run_id(), note=note)


def retire_tag(slug: str, *, note: str = "", root: Path | None = None,
               run_id: str | None = None) -> tuple[str, bool]:
    """RETIRE a tag from the controlled vocabulary — a `tag_curation` redirect with winner None: the slug
    leaves the vocab and every assignment of it resolves to nothing (dropped). Reversible (active:false).
    Returns (hash, written)."""
    loser = slugify(slug)
    if not loser:
        raise ValueError(f"retire_tag needs a non-empty slug: {slug!r}")
    return _write_tag_curation(loser, None, root=root, run_id=run_id or config.run_id(), note=note)


# --- concept helpers: load a version, the valid set, RE-VALIDATE evidence (the trust chain) -------

def _concept_blob(concept_id: str, root: Path) -> dict | None:
    """The latest VERSION of a concept source — its raw blob, valid OR not (an op reads a loser's blob to
    union its evidence even as it invalidates it). A gone/malformed source → None, never fatal."""
    h = blobstore.latest_version(concept_id, root)
    if not h:
        return None
    try:
        obj = json.loads(blobstore.get(h, root))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) and obj.get("id") else None


def _revalidate_evidence(evidence: list[dict], root: Path) -> list[dict]:
    """Re-validate a pool of concept evidence pointers — keep ONLY the spans that re-anchor NOW
    (review.resolve_evidence runs each through blobstore.validate_span), deduped, sorted for order-invariant
    bytes (so a merge is idempotent regardless of loser order). This is review.accept's `_verified_pointers`
    discipline applied to a structural op: every minted/versioned concept carries exactly the evidence that
    RE-PROVES against its immutable blobs — the trust chain reaches the gardener, not just the human gate."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in review.resolve_evidence({"evidence": evidence}, root):
        key = (e["event_id"], e["cleaned_hash"], e["byte_start"], e["byte_end"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"event_id": e["event_id"], "cleaned_hash": e["cleaned_hash"],
                    "byte_start": e["byte_start"], "byte_end": e["byte_end"]})
    out.sort(key=lambda p: (p["cleaned_hash"], p["byte_start"], p["byte_end"], str(p["event_id"])))
    return out


def _require_evidence(evidence: list[dict], *, allow_no_evidence: bool, what: str) -> None:
    """The ZERO-EVIDENCE FLOOR — mirror review.accept's empty-evidence refusal (review.py) for a structural
    op. A re-validated pool that came back EMPTY must NOT silently become a curated belief: cleaned spans are
    TTL-eligible, so an unbacked re-validation would feed a concept with no verifiable anchor into
    dream/generate — the exact failure accept guards the human gate against. Raise unless the caller
    deliberately overrides (`allow_no_evidence=True`), the same escape hatch accept exposes."""
    if not evidence and not allow_no_evidence:
        raise ValueError(f"{what} re-validates to no evidence — refusing to mint/version a concept with no "
                         f"verifiable backing (override with allow_no_evidence=True)")


def _mint_op_concept_id(material: str) -> str:
    """A fresh, DETERMINISTIC concept id for an op-minted concept (a split part / an abstract parent) — the
    op-side mirror of review._mint_concept_id, sharing its `c-` prefix so the id space is uniform.
    Deterministic on the op inputs → a crash-retry re-mints the SAME id, absorbed as a byte-identical
    version (latest wins), never an orphan duplicate."""
    return review.CONCEPT_ID_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _ingest_concept(concept_id: str, title: str, statement: str, evidence: list[dict],
                    source_takeaway, root: Path, *, op: str, run_id: str,
                    extra_origin: dict | None = None) -> str:
    """Ingest a concept blob VERSION (ADR-0007) in the SAME shape review.accept mints
    ({id,title,statement,evidence,source_takeaway}), so the trust chain + every downstream reader are
    unchanged. CONTENT is run-invariant — the op lineage rides in origin_ref, not the body — so a
    deterministic-id re-run re-ingests byte-identically and no-ops (idempotent). Returns the version hash."""
    concept = {"id": concept_id, "title": title, "statement": statement,
               "evidence": evidence, "source_takeaway": source_takeaway}
    origin = {"stage": "garden", "op": op, "run_id": run_id}
    if extra_origin:
        origin.update(extra_origin)
    ch, _ = blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept",
                             source_id=concept_id, origin_ref=origin, root=root)
    return ch


def _write_concept_decision(verb: str, target: str, root: Path, *, run_id: str, **fields) -> None:
    """Append a lifecycle decision over a concept — written exactly like review._record / dream._write_decision
    (source_id == blob_hash(body), prev=None, fetched_at == body['at'], so the audited + folded timelines
    agree). `dream.load_concepts` folds `supersede`/`split` (with `retire`) out of the valid set — the
    invalidate-don't-delete fold: the concept blob + history stay, only the latest decision moves it."""
    at = config.now()
    body = {"verb": verb, "target": target, "at": at, "run_id": run_id,
            "producer": {"stage": "garden", "run_id": run_id, "at": at}, **fields}
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s),
                     origin_ref={"stage": "garden", "verb": verb, "target": target},
                     fetched_at=at, prev=None, root=root)


def _carry_edges(old_id: str, new_id: str, root: Path, *, run_id: str, op: str) -> None:
    """Re-point a concept's relational edges onto another (merge uses it: a loser's hierarchy + association
    edges MOVE to the winner, so a merge never silently drops them — carry-don't-drop, the edge analogue of
    union-the-evidence). For each active edge touching `old_id`, assert the equivalent with old→new
    substituted and RETRACT the original. Skips `supersedes` (pure lineage — re-pointing it would chase the
    merge's own loser→winner edge) and any substitution that would self-loop."""
    for e in asserted_edges(root):
        if e["kind"] == "supersedes" or old_id not in (e["src"], e["dst"]):
            continue
        nsrc = new_id if e["src"] == old_id else e["src"]
        ndst = new_id if e["dst"] == old_id else e["dst"]
        retract_edge(e["src"], e["kind"], e["dst"], root=root, run_id=run_id, op=op)
        if nsrc != ndst:
            assert_edge(nsrc, e["kind"], ndst, note=e["note"], root=root, run_id=run_id, op=op)


# --- the structural ops: append-only, invalidate-don't-delete, trust-chain preserving -------------

def merge(loser_ids: list[str], winner_id: str, *, title: str | None = None,
          statement: str | None = None, root: Path | None = None, run_id: str | None = None,
          allow_no_evidence: bool = False) -> str:
    """MERGE losers INTO a winner — a new WINNER concept VERSION unioning the losers' evidence (re-validated,
    deduped) + their relational edges, each loser INVALIDATED via a `supersede` decision (dropped from
    `valid_concepts`; blob + history retained) + a `supersedes` asserted edge loser→winner recording the
    lineage (the edge points from the retired concept to its replacement). The winner's evidence is
    RE-VALIDATED on write and REFUSED if empty (`allow_no_evidence` overrides), so the trust chain reaches
    the merged concept. Both winner AND each loser must be VALID — an already-invalidated loser is skipped
    (merging it would re-introduce evidence a prior invalidation deliberately moved away). Title/statement
    default to the winner's (3c-ii's LLM supplies a synthesized one); the losers' `source_takeaway`s ride in
    origin_ref (the concept schema keeps ONE field). Append-only, invalidate-don't-delete (ADR-0015).
    Returns winner_id."""
    root = root or config.data_root()
    run_id = run_id or config.run_id()
    valid = dream.valid_concept_ids(root)               # the membership gate — winner AND losers
    winner = _concept_blob(winner_id, root)
    if winner is None or winner_id not in valid:
        raise ValueError(f"merge winner {winner_id!r} is not a valid concept")
    pooled = list(winner.get("evidence") or [])
    source_takeaways: list = []                         # ONLY the LOSERS' takeaways merged in — the winner's
                                                        # own already rides in the body's `source_takeaway`
    losers: list[str] = []
    for lid in loser_ids:
        if lid == winner_id:
            continue                                    # a concept never merges into itself
        if lid not in valid:
            continue                                    # an already-invalidated loser re-introduces
                                                        # moved-away evidence — skip (mirror the winner guard)
        lc = _concept_blob(lid, root)
        if lc is None:
            continue                                    # a gone loser is a no-op, never fatal
        pooled.extend(lc.get("evidence") or [])
        source_takeaways.append(lc.get("source_takeaway"))
        losers.append(lid)
    evidence = _revalidate_evidence(pooled, root)
    _require_evidence(evidence, allow_no_evidence=allow_no_evidence, what=f"merge winner {winner_id!r}")
    _ingest_concept(winner_id, winner.get("title", "") if title is None else title,
                    winner.get("statement", "") if statement is None else statement,
                    evidence, winner.get("source_takeaway"), root, op="merge", run_id=run_id,
                    extra_origin={"losers": losers,
                                  "source_takeaways": [s for s in source_takeaways if s]})
    for lid in losers:                                  # carry edges, then record lineage + invalidate
        _carry_edges(lid, winner_id, root, run_id=run_id, op="merge")
        assert_edge(lid, "supersedes", winner_id, root=root, run_id=run_id, op="merge")
        _write_concept_decision(dream.VERB_SUPERSEDE, lid, root, run_id=run_id, into=winner_id, op="merge")
    return winner_id


def split(concept_id: str, parts: list[dict], *, root: Path | None = None,
          run_id: str | None = None, allow_no_evidence: bool = False) -> list[str]:
    """SPLIT one concept into several — mint a NEW concept per `part` ({title, statement, evidence}), each
    carrying a re-validated SUBSET of the original's evidence (the caller's chosen partition — 3c-ii's LLM
    picks it), INVALIDATE the original via a `split` decision (dropped from `valid_concepts`; blob + history
    retained), and assert a `supersedes` edge original→part for each (lineage). Each part's evidence is
    re-validated on write and REFUSED if empty (`allow_no_evidence` overrides) — a part is the narrow
    counterpart of `abstract`'s broad parent, so it must carry a real subset, not float free of backing.
    Append-only, invalidate-don't-delete (ADR-0015). Returns the new ids."""
    root = root or config.data_root()
    run_id = run_id or config.run_id()
    orig = _concept_blob(concept_id, root)
    if orig is None or concept_id not in dream.valid_concept_ids(root):
        raise ValueError(f"split target {concept_id!r} is not a valid concept")
    new_ids: list[str] = []
    for i, p in enumerate(parts):
        ev = _revalidate_evidence(p.get("evidence") or [], root)
        _require_evidence(ev, allow_no_evidence=allow_no_evidence,
                          what=f"split part #{i} of {concept_id!r}")
        # the part INDEX `i` is folded into the mint material so two parts sharing a (or empty) title mint
        # DISTINCT ids — without it the second collides onto the first and silently overwrites it, losing a
        # part. Stable across re-runs (a fixed partition → fixed indices), so resumability holds.
        pid = _mint_op_concept_id(f"{concept_id}{EDGE_SEP}split{EDGE_SEP}{i}{EDGE_SEP}{p.get('title', '')}")
        _ingest_concept(pid, str(p.get("title", "")), str(p.get("statement", "")), ev,
                        orig.get("source_takeaway"), root, op="split", run_id=run_id,
                        extra_origin={"split_from": concept_id})
        assert_edge(concept_id, "supersedes", pid, root=root, run_id=run_id, op="split")
        new_ids.append(pid)
    _write_concept_decision(dream.VERB_SPLIT, concept_id, root, run_id=run_id, parts=new_ids, op="split")
    return new_ids


def abstract(child_ids: list[str], title: str, statement: str, *, evidence: list[dict] | None = None,
             root: Path | None = None, run_id: str | None = None, allow_no_evidence: bool = False) -> str:
    """ABSTRACT children under a NEW parent generalization — mint a new concept (evidence = the UNION of the
    children's, or a curated subset the caller supplies) and assert a `generalizes` edge parent→child for
    each. The children STAY VALID (a generalization ADDS a belief, it does not remove the specifics); only
    the parent is minted, so nothing is invalidated. The parent's evidence is re-validated on write and
    REFUSED if empty (`allow_no_evidence` overrides), so the trust chain reaches it. The active `generalizes`
    edges form the hierarchy spine (ADR-0015). Returns the parent id."""
    root = root or config.data_root()
    run_id = run_id or config.run_id()
    valid = dream.valid_concept_ids(root)
    children = [c for c in child_ids if c in valid and _concept_blob(c, root) is not None]
    if not children:
        raise ValueError("abstract needs at least one valid child concept")
    if evidence is None:                                # default: UNION the children's evidence
        pooled: list[dict] = []
        for c in children:
            pooled.extend((_concept_blob(c, root) or {}).get("evidence") or [])
    else:
        pooled = list(evidence)                         # a curated subset the caller chose
    ev = _revalidate_evidence(pooled, root)
    _require_evidence(ev, allow_no_evidence=allow_no_evidence,
                      what=f"abstract parent over {sorted(children)}")
    pid = _mint_op_concept_id(f"abstract{EDGE_SEP}{EDGE_SEP.join(sorted(children))}{EDGE_SEP}{title}")
    _ingest_concept(pid, str(title), str(statement), ev, None, root, op="abstract", run_id=run_id,
                    extra_origin={"children": sorted(children)})
    for c in sorted(children):
        assert_edge(pid, "generalizes", c, root=root, run_id=run_id, op="abstract")
    return pid


def reparent(concept_id: str, new_parent_id: str, *, root: Path | None = None,
             run_id: str | None = None) -> None:
    """REPARENT a concept under a new generalization — RETRACT every active `generalizes` edge INTO it
    (active:false) and assert the new parent→child edge. Edge-only: no concept is versioned or invalidated,
    so the trust chain is untouched and the stakes are low. Idempotent — re-running asserts the same active
    edge (byte-identical no-op) and finds nothing left to retract (ADR-0015)."""
    root = root or config.data_root()
    run_id = run_id or config.run_id()
    valid = dream.valid_concept_ids(root)
    if concept_id not in valid or new_parent_id not in valid:
        raise ValueError("reparent needs a valid concept and a valid new parent")
    for e in asserted_edges(root):                      # retract the OLD parent edge(s)
        if e["kind"] == "generalizes" and e["dst"] == concept_id and e["src"] != new_parent_id:
            retract_edge(e["src"], "generalizes", concept_id, root=root, run_id=run_id, op="reparent")
    assert_edge(new_parent_id, "generalizes", concept_id, root=root, run_id=run_id, op="reparent")


def retire(concept_id: str, *, reason: str = "", reviewer: str = "sulin",
           root: Path | None = None) -> None:
    """RETIRE a concept — reuse review.retire (the human-gate verb `load_concepts` already folds out).
    Exposed on the gardener's op surface so all the structural ops share one entry point; the underlying
    decision + invalidate-don't-delete semantics are review's (ADR-0008/0015)."""
    review.retire(concept_id, root, reason=reason, reviewer=reviewer)


# --- stakes: the fuzzy gradient 3c-ii routes on (high→human review, low→auto) ----------------------

OP_STAKES = {
    # HIGH — changes what concepts EXIST or what they ASSERT → 3c-ii routes to the human gate (3d)
    "merge": 0.85, "split": 0.85, "retire": 0.80, "abstract": 0.65,
    # LOW — edge-only / vocab curation: relations + grouping, never a concept's assertion → auto-apply
    "reparent": 0.25, "retire_tag": 0.20, "merge_tags": 0.15,
    "assert_edge": 0.10, "retract_edge": 0.10,
}


def op_stakes(op) -> float:
    """The FUZZY stakes gradient — higher for ops that change what concepts EXIST or ASSERT (merge/split/
    retire/abstract), lower for edge-only / tag-vocab curation (reparent/merge_tags/...). Defined HERE, in
    ONE place, so 3c-ii routes high→human review (3d) / low→auto on a SINGLE gradient (ADR-0015) — a
    gradient, not hard lines, so the routing threshold is a tunable knob, not a brittle if/else. `op` is the
    op name or a descriptor dict ({"op": name, ...}); breadth (more concepts touched) nudges the score up a
    little, clamped to [0,1] — a 5-way merge is a touch higher-stakes than a 2-way. An unknown op lands
    mid-gradient (so a new, unclassified op routes to review by default — fail safe)."""
    name = op if isinstance(op, str) else str((op or {}).get("op", ""))
    base = OP_STAKES.get(name, 0.5)
    breadth = 0
    if isinstance(op, dict):
        for k in ("loser_ids", "child_ids", "parts"):
            v = op.get(k)
            if isinstance(v, (list, tuple)):
                breadth = max(breadth, len(v))
    return max(0.0, min(1.0, base + min(0.10, 0.02 * max(0, breadth - 1))))


# ==================================================================================================
# THE OPS-PROPOSER BLOCK (3c-ii, ADR-0016): a SHARP model proposes structural ops, the gradient routes
#
# 3c-i (above) is the deterministic op machinery. 3c-ii is the gardener's PHASE 2: a sharper model
# (Sonnet) reads each high-tension concept CLUSTER (the 3a facet + 3b tag substrate) and PROPOSES
# structural ops — merge / split / abstract / reparent / retire (+ tag merge/retire). This is the cascade
# ONE LEVEL UP from dream's route/synth: the cheap pre-gate (3a/3b clustering) narrows the field to the
# clusters worth a sharp look, then ONE sharp call per cluster proposes the edits — never a sharp call per
# concept pair, never an embedding index.
#
# The untrusted proposal is DEFENSIVELY COERCED (mirror dream's `_clean_route`): an op of an unknown kind,
# citing a concept NOT in THIS cluster / not in the valid set, or malformed in shape, is DROPPED — never
# acted on. The surviving ops route on `op_stakes` (the 3c-i fuzzy gradient): LOW-stakes edge/tag/reparent
# ops AUTO-APPLY (call the 3c-i fn directly), HIGH-stakes (and the fuzzy MIDDLE — recall-first) ops are
# QUEUED as append-only `garden_proposal` blobs for the 3d human gate. The op's RATIONALE is UNTRUSTED
# (like dream's `why`): it rides into the proposal as the human-facing justification, never as a fact the
# machine trusts — the faithfulness check belongs to 3d, not the proposer.
#
# A `garden_proposal` is the same blob/fold shape as every other ratchet artifact (ADR-0007): keyed on a
# DETERMINISTIC proposal id (the op's identity), latest-wins — a QUEUED artifact with NO lifecycle field.
# `open_proposals(root)` folds the queue and drops any proposal a 3d resolve DECISION (accept/reject) closed.
# The 3d accept/reject surface is the NEXT section — here a high-stakes op only gets QUEUED.
# ==================================================================================================

PROPOSE_PROMPT_VERSION = "garden-ops/1"   # bump to re-propose over every cluster with a sharper prompt
PROPOSE_MODEL = "sonnet"                  # the proposer is SHARP + RARE (one call per high-tension cluster)
PROPOSE_OUT_NOUN = "ops"                  # the per-cluster output noun the Progress bar/line shows

AUTO_APPLY_MAX_STAKES = 0.35   # the auto/queue cut on the `op_stakes` gradient. RECALL-FIRST: it sits LOW —
                               # just above the edge/tag/reparent band (assert_edge .10 … reparent .25) and
                               # well below the concept-altering band (abstract .65 … merge/split .85), so the
                               # empty FUZZY MIDDLE (.35–.65) routes to the human gate, never auto. The human's
                               # attention is the conservative default: when near the line, QUEUE. A tunable
                               # knob, not a hard line (op_stakes IS a gradient) — 3d can raise it as trust grows.
                               # DEAD-BAND today: with the current OP_STAKES no op falls in (0.25, 0.65), so 0.35
                               # is behaviourally IDENTICAL to any cut in that range — "queue the fuzzy middle"
                               # is FORWARD-LOOKING (for an op that later lands mid-gradient), not active now.

PROPOSAL_KIND = "garden_proposal"   # an append-only QUEUED structural-op proposal (vs an op already applied)
PROPOSAL_PREFIX = "gp-"             # garden_proposal source_id = PREFIX + sha256(op identity)[:12]
RESOLVE_VERBS = frozenset({"accept_proposal", "reject_proposal"})   # the 3d gate's verdict verbs — review
                                   # records one against a `gp-` proposal id. A proposal is RESOLVED iff its
                                   # `blobstore.latest_decision` carries a verb in here; OPEN otherwise. The
                                   # queue derives OPEN from this — no stored status (ADR-0017). garden owns
                                   # the proposal-verb vocabulary; review references it.
RATIONALE_MAX = 240                 # the proposer's one-line justification (UNTRUSTED — surfaced to 3d)
QUOTES_PER_CONCEPT = 2              # a FEW verified evidence quotes per concept ground the proposer's call
QUOTE_MAX = 160
OPS_PER_CLUSTER_MAX = 6             # a sharp call proposes a HANDFUL of ops per cluster; more is noise, capped
OP_TITLE_MAX = 80
OP_STATEMENT_MAX = 400

# tension weights — UNTUNED named constants (like the facet/tag weights, pending a gold set). Concept-level
# CONTRADICTION signals live on TAKEAWAYS (ADR-0012), not concepts, so the cluster cannot read them directly;
# tension proxies on the cheap structural signals the cluster DOES carry — DENSITY (size + how strongly the
# members relate) and the AMOUNT of related evidence.
W_TENSION_SIZE = 1.0       # density: more concepts in one cluster = more candidate merges/splits/abstractions
W_TENSION_COHESION = 0.25  # how strongly the members already relate (the facet-overlap mass that grouped them)
W_TENSION_EVIDENCE = 0.05  # the AMOUNT of related evidence backing the cluster (material the gardener acts on)

# the op vocabulary the proposer may emit → its `op_stakes` key (only `relate` differs: a relates-to edge is
# `assert_edge` on the gradient; every other op name IS its stakes key). An op outside this set is dropped.
PROPOSE_OPS = ("merge", "split", "abstract", "reparent", "retire", "relate", "merge_tags", "retire_tag")
_STAKES_KEY = {"relate": "assert_edge"}

PROPOSE_SYSTEM = (
    "You are the GARDENER for a developer's long-term memory. Each CONCEPT is a durable, human-reviewed "
    "lesson. You are shown ONE CLUSTER of concepts the cheap layer grouped as related (they share files / "
    "repos / tools, or a theme tag), each with its title, statement, provenance, tags, a few VERIFIED "
    "evidence quotes, and any relations already asserted among them. Propose STRUCTURAL EDITS that make the "
    "concept layer cleaner:\n"
    "  - merge: two+ concepts state the SAME lesson — fold the losers into one winner.\n"
    "  - split: one concept conflates TWO distinct lessons — divide it into parts.\n"
    "  - abstract: several concepts share a more GENERAL parent idea — name it.\n"
    "  - reparent: a concept belongs under a different generalization parent.\n"
    "  - retire: a concept is stale, wrong, or fully subsumed — take it out of the valid set.\n"
    "  - relate: two concepts are associated (a relates-to link), short of a merge.\n"
    "  - merge_tags / retire_tag: two theme tags duplicate, or one is dead — fold/drop it.\n"
    "Propose ONLY edits you are confident about, each with a ONE-LINE rationale and the concept ids it "
    "touches. Cite ONLY ids shown in THIS cluster. A clean cluster may need NO edits — return an empty list.\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{"ops": [\n'
    '  {"op": "merge", "winner_id": "c-…", "loser_ids": ["c-…"], "rationale": "…"},\n'
    '  {"op": "split", "concept_id": "c-…", "parts": [{"title": "…", "statement": "…"}, …], "rationale": "…"},\n'
    '  {"op": "abstract", "child_ids": ["c-…", "c-…"], "title": "…", "statement": "…", "rationale": "…"},\n'
    '  {"op": "reparent", "concept_id": "c-…", "parent_id": "c-…", "rationale": "…"},\n'
    '  {"op": "retire", "concept_id": "c-…", "rationale": "…"},\n'
    '  {"op": "relate", "src": "c-…", "dst": "c-…", "rationale": "…"},\n'
    '  {"op": "merge_tags", "loser_slug": "…", "winner_slug": "…", "rationale": "…"},\n'
    '  {"op": "retire_tag", "slug": "…", "rationale": "…"}\n'
    "]}"
)


# --- the cluster's tension: the priority SIGNAL (highest-tension first, ADR-0011) ------------------

def cluster_tension(cluster: dict, blob_by_id: dict, facets_by_id: dict) -> float:
    """The priority SIGNAL — how much a sharp pass over this cluster buys us (ADR-0011's modular knob;
    `GardenOpsBlock.priority` delegates here, the driver's Greedy policy sorts highest-first). A DENSE
    cluster of well-related concepts backed by lots of evidence is the most worth gardening: it holds the
    most candidate merges/splits/abstractions. Pure + deterministic — size, the members' pairwise
    facet-overlap mass (the same `facet_score` that grouped them), and the total cited-evidence count.
    UNTUNED, like the facet/tag weights."""
    members = [m for m in cluster["members"] if m in blob_by_id]
    size = len(members)
    cohesion = sum(concepts.facet_score(facets_by_id.get(a, {}), facets_by_id.get(b, {}))
                   for a, b in combinations(members, 2))
    evidence_mass = sum(len(blob_by_id[m].get("evidence") or []) for m in members)
    return W_TENSION_SIZE * size + W_TENSION_COHESION * cohesion + W_TENSION_EVIDENCE * evidence_mass


# --- the sharp call: render the cluster → propose ops → DEFENSIVELY coerce -------------------------

def _facet_ctx(f: dict | None) -> str:
    f = f or {}
    return (f"repos={f.get('repos', [])} files={f.get('files', [])} "
            f"tools={f.get('tools', [])} tags={f.get('tags', [])}")


def _verified_quotes(blob: dict, root: Path, *, limit: int = QUOTES_PER_CONCEPT,
                     maxlen: int = QUOTE_MAX) -> list[str]:
    """A FEW VERIFIED evidence quotes for a concept — `review.resolve_evidence` re-validates each span at the
    read boundary, so the proposer reads the trusted verbatim bytes (not the takeaway's claimed quote). The
    proposer's grounding, the same trust anchor review serves the human."""
    out: list[str] = []
    for e in review.resolve_evidence({"evidence": blob.get("evidence") or []}, root):
        q = " ".join(str(e.get("quote", "")).split())[:maxlen]
        if q:
            out.append(q)
        if len(out) >= limit:
            break
    return out


def _propose_user(members: list[str], blob_by_id: dict, facets_by_id: dict, quotes: dict,
                  among: list[dict], cluster_tags: set[str]) -> str:
    lines = ["CLUSTER (related concepts):"]
    for cid in members:
        b = blob_by_id[cid]
        lines.append(f"- id {cid}: {str(b.get('title', '')).strip()!r}")
        lines.append(f"    statement: {str(b.get('statement', '')).strip()[:OP_STATEMENT_MAX]!r}")
        lines.append(f"    provenance: {_facet_ctx(facets_by_id.get(cid))}")
        qs = quotes.get(cid) or []
        if qs:
            lines.append("    evidence: " + " | ".join(f"{q!r}" for q in qs))
    if among:
        lines.append("\nASSERTED RELATIONS among these concepts:")
        for e in among:
            lines.append(f"- {e['src']} —{e['kind']}→ {e['dst']}"
                         + (f"  ({e['note']})" if e.get("note") else ""))
    if cluster_tags:
        lines.append(f"\nTHEME TAGS in this cluster: {sorted(cluster_tags)}")
    return "\n".join(lines)


def _clean_op(raw, *, member_ids: set[str], valid_ids: set[str], cluster_tags: set[str]) -> dict | None:
    """Coerce ONE untrusted proposed op (mirror `_clean_route`/`_clean_relation`): DROP it unless its kind is
    known, its shape is well-formed, and EVERY concept id it cites is BOTH in this cluster AND valid — never
    act on an id the model invented out of nothing, nor one outside the cluster it was actually shown. Tag
    ops validate their slugs against THIS cluster's tag set. The rationale is coerced to a short string,
    UNTRUSTED — carried to 3d as provenance, never trusted here. Returns {op, params, concept_ids, rationale}
    or None (dropped)."""
    if not isinstance(raw, dict):
        return None
    op = raw.get("op")
    if op not in PROPOSE_OPS:
        return None
    rationale = str(raw.get("rationale", "")).strip()[:RATIONALE_MAX]

    def cid(x):                              # a cited concept id is kept ONLY if in-cluster AND valid
        return x if isinstance(x, str) and x in member_ids and x in valid_ids else None

    if op == "merge":
        winner = cid(raw.get("winner_id"))
        losers: list[str] = []
        for c in raw.get("loser_ids") or []:
            c = cid(c)
            if c and c != winner and c not in losers:
                losers.append(c)
        if not winner or not losers:
            return None
        params = {"winner_id": winner, "loser_ids": sorted(losers)}
        concept_ids = sorted({winner, *losers})
    elif op == "split":
        target = cid(raw.get("concept_id"))
        parts: list[dict] = []
        for p in raw.get("parts") or []:
            if isinstance(p, dict) and str(p.get("title", "")).strip():
                parts.append({"title": str(p.get("title", "")).strip()[:OP_TITLE_MAX],
                              "statement": str(p.get("statement", "")).strip()[:OP_STATEMENT_MAX]})
        if not target or len(parts) < 2:     # a split needs a target + at least two parts (else it's a no-op)
            return None
        params = {"concept_id": target, "parts": parts}
        concept_ids = [target]
    elif op == "abstract":
        children: list[str] = []
        for c in raw.get("child_ids") or []:
            c = cid(c)
            if c and c not in children:
                children.append(c)
        title = str(raw.get("title", "")).strip()
        if len(children) < 2 or not title:   # a generalization spans at least two children + names itself
            return None
        params = {"child_ids": sorted(children), "title": title[:OP_TITLE_MAX],
                  "statement": str(raw.get("statement", "")).strip()[:OP_STATEMENT_MAX]}
        concept_ids = sorted(children)
    elif op == "reparent":
        child = cid(raw.get("concept_id"))
        parent = cid(raw.get("parent_id"))
        if not child or not parent or child == parent:
            return None
        params = {"concept_id": child, "parent_id": parent}
        concept_ids = sorted({child, parent})
    elif op == "retire":
        target = cid(raw.get("concept_id"))
        if not target:
            return None
        params = {"concept_id": target}
        concept_ids = [target]
    elif op == "relate":
        a, b = cid(raw.get("src")), cid(raw.get("dst"))
        if not a or not b or a == b:
            return None
        params = {"src": a, "dst": b}
        concept_ids = sorted({a, b})
    elif op == "merge_tags":
        loser, winner = slugify(raw.get("loser_slug")), slugify(raw.get("winner_slug"))
        if (not loser or not winner or loser == winner
                or loser not in cluster_tags or winner not in cluster_tags):
            return None
        params = {"loser_slug": loser, "winner_slug": winner}
        concept_ids = []
    else:                                    # retire_tag
        slug = slugify(raw.get("slug"))
        if not slug or slug not in cluster_tags:
            return None
        params = {"slug": slug}
        concept_ids = []
    return {"op": op, "params": params, "concept_ids": concept_ids, "rationale": rationale}


def propose_ops(cluster: dict, propose: Completer, *, blob_by_id: dict, facets_by_id: dict,
                asserted: list[dict], valid_ids: set[str], root: Path) -> tuple[list[dict], float]:
    """ONE sharp call over the cluster (id + title + statement + facets + tags + a few VERIFIED quotes + the
    asserted edges among them) → the coerced, deduped, capped list of surviving op descriptors + cost. A
    raised proposer propagates — the driver isolates the cluster as errored (no marker → retried next run),
    exactly like dream's route. Pure-injectable: offline-tested with a fake that echoes scripted ops."""
    members = [m for m in cluster["members"] if m in blob_by_id]
    member_ids = set(members)
    cluster_tags: set[str] = set()
    for m in members:
        cluster_tags |= set(facets_by_id.get(m, {}).get("tags", []))
    quotes = {m: _verified_quotes(blob_by_id[m], root) for m in members}
    among = [e for e in asserted if e["src"] in member_ids and e["dst"] in member_ids]
    comp = propose(PROPOSE_SYSTEM, _propose_user(members, blob_by_id, facets_by_id, quotes, among, cluster_tags))
    cost = completer.cost_of(comp)
    parsed = completer.parse_json_object(comp.text) or {}
    raw_ops = parsed.get("ops")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_ops if isinstance(raw_ops, list) else []:
        desc = _clean_op(raw, member_ids=member_ids, valid_ids=valid_ids, cluster_tags=cluster_tags)
        if desc is None:
            continue
        pid = mint_proposal_id(desc)
        if pid in seen:                      # the SAME structural edit proposed twice in one call → once
            continue
        seen.add(pid)
        out.append(desc)
        if len(out) >= OPS_PER_CLUSTER_MAX:
            break
    return out, cost


# --- route on stakes: auto-apply LOW, queue HIGH as an append-only proposal ------------------------

def _op_stakes_of(desc: dict) -> float:
    """The op's stakes on the 3c-i `op_stakes` gradient — keyed by the descriptor's stakes-name (`relate` →
    `assert_edge`), with the breadth fields passed through so a wider op (more losers/children/parts) scores
    a touch higher. ONE source for the routing decision — `op_stakes` (ADR-0015), never a re-derived policy."""
    probe: dict = {"op": _STAKES_KEY.get(desc["op"], desc["op"])}
    p = desc.get("params") or {}
    for k in ("loser_ids", "child_ids", "parts"):
        if isinstance(p.get(k), list):
            probe[k] = p[k]
    return op_stakes(probe)


def mint_proposal_id(desc: dict) -> str:
    """A DETERMINISTIC proposal id from the op's IDENTITY (kind + params, NOT the rationale): a re-proposal of
    the SAME structural edit re-versions the SAME `garden_proposal` (latest-wins, no duplicate), while a
    changed rationale is just a new version — dream's stable-minted-id resumability discipline, one level up."""
    identity = blobstore.canonical_json({"op": desc["op"], "params": desc["params"]})
    return PROPOSAL_PREFIX + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def _proposal_blob(proposal_id: str, root: Path | None = None) -> dict | None:
    """The latest VERSION of a `garden_proposal` source — its raw content dict, or None if unknown/malformed.
    The single loader the queue fold and the 3d gate both go through, so they agree on what "the proposal" is.
    The proposal carries NO lifecycle field; its resolution is a separate decision (`RESOLVE_VERBS`)."""
    h = blobstore.latest_version(proposal_id, root or config.data_root())
    if not h:
        return None
    try:
        obj = json.loads(blobstore.get(h, root or config.data_root()))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) and obj.get("proposal_id") else None


def queue_proposal(desc: dict, *, cluster_leader: str, stakes: float, root: Path | None = None,
                   run_id: str, model: str) -> tuple[str, bool]:
    """QUEUE a high-stakes op as an append-only `garden_proposal` blob for the 3d gate (NOT applied). Keyed on
    the deterministic proposal id, latest-wins; a QUEUED artifact with NO lifecycle field — its resolution is a
    separate 3d decision (`RESOLVE_VERBS`), never a status on the blob. CONTENT is run-invariant ({proposal_id,
    op, params, concept_ids, rationale, stakes, cluster_leader, prompt_version}) so a crash-retry re-ingests
    byte-identically; the producer/cost ride in `origin_ref`. The rationale rides in the body as the
    human-facing justification — UNTRUSTED, surfaced to 3d, never acted on here. Returns (hash, written).

    REJECTED-OP SUPPRESSION (the L2 loop closing — the canonical why): the gardener REMEMBERS a 3d verdict.
    `mint_proposal_id` is deterministic on op identity, so a re-gardened cluster re-proposes the SAME id —
    re-queuing a fresh version over a dismissed op would RESURRECT it, the "re-suggests dismissed things"
    trust-killer `review.reject_proposal` exists to close (MemPrompt). So once the 3d gate has RESOLVED a
    proposal — `latest_decision(pid).verb` in `RESOLVE_VERBS` (rejected, the load-bearing case, OR accepted,
    already applied) — leave it standing: skip the re-queue, return its existing version. Decision-sourced, so
    accept AND reject both suppress. The FIRST queue (no decision) and a still-OPEN re-queue (byte-identical
    no-op, or a new-rationale version) proceed."""
    root = root or config.data_root()
    pid = mint_proposal_id(desc)
    last = blobstore.latest_decision(pid, root)          # the 3d verdict if any — derived, never a stored status
    if last and last.get("verb") in RESOLVE_VERBS:
        return blobstore.latest_version(pid, root), False
    content = {"proposal_id": pid, "op": desc["op"], "params": desc["params"],
               "concept_ids": desc["concept_ids"], "rationale": desc["rationale"],
               "stakes": round(float(stakes), 4), "cluster_leader": cluster_leader,
               "prompt_version": PROPOSE_PROMPT_VERSION}
    body = blobstore.canonical_json(content)
    return blobstore.ingest(body, source_kind=PROPOSAL_KIND, source_id=pid,
                            origin_ref={"stage": "garden", "phase": "ops", "op": desc["op"], "model": model,
                                        "run_id": run_id, "prompt_version": PROPOSE_PROMPT_VERSION}, root=root)


def open_proposals(root: Path | None = None) -> list[dict]:
    """The OPEN proposal queue — `latest_by_kind('garden_proposal')` folded latest-wins per proposal id, MINUS
    any the 3d gate has RESOLVED (its `latest_decision` verb in `RESOLVE_VERBS`). DECISION-DRIVEN, byte-symmetric
    with tier-1's `review.pending` (the takeaway blob carries no review state either — the decision IS the
    lifecycle, ADR-0007/0017): a queued proposal has no status field, so an accept/reject DECISION is the only
    thing that drops it from this fold — no blob re-version, no flipped field. Sorted for stable bytes; a
    malformed/absent blob is skipped, never fatal."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)         # `gp-` targets carry only RESOLVE_VERBS decisions
    out: list[dict] = []
    for h in blobstore.latest_by_kind(PROPOSAL_KIND, root).values():
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if not (isinstance(obj, dict) and obj.get("proposal_id")):
            continue
        d = decisions.get(obj["proposal_id"])
        if d and d.get("verb") in RESOLVE_VERBS:         # accepted/rejected → out of the open queue
            continue
        out.append(obj)
    out.sort(key=lambda p: (p.get("op", ""), p["proposal_id"]))
    return out


# the ops `_apply_op` can auto-apply directly — every op the proposer emits EXCEPT `split` (whose per-part
# EVIDENCE PARTITION is the 3d/human's to choose, never the machine's to guess). `process` auto-applies an op
# ONLY when it is BOTH low-stakes AND in this set; anything else QUEUES unconditionally, so a non-auto-applicable
# op — split, or a future kind `_apply_op` does not handle — at a deliberately-raised `--auto-max-stakes` is
# QUEUED for 3d, never stranded on `_apply_op`'s raise (N1). Keep in lockstep with `_apply_op`'s branches.
AUTO_APPLICABLE_OPS = frozenset({"merge", "abstract", "reparent", "retire", "relate", "merge_tags", "retire_tag"})


def _apply_op(desc: dict, *, root: Path, run_id: str) -> None:
    """AUTO-APPLY one op by calling its 3c-i fn directly (the trusted, append-only machinery). `process` routes
    here ONLY ops in `AUTO_APPLICABLE_OPS`, and only when low-stakes — so under the default threshold just the
    edge/tag/reparent ops land, while a deliberately-raised threshold lets the concept-altering branches
    (merge/abstract/retire) fire too (all still wired). An auto-applied edge/curation carries the rationale as
    its NOTE (provenance)."""
    op, p, note = desc["op"], desc["params"], desc.get("rationale", "")
    if op == "relate":
        assert_edge(p["src"], "relates-to", p["dst"], note=note, root=root, run_id=run_id, op="propose")
    elif op == "reparent":
        reparent(p["concept_id"], p["parent_id"], root=root, run_id=run_id)
    elif op == "merge_tags":
        merge_tags(p["loser_slug"], p["winner_slug"], note=note, root=root, run_id=run_id)
    elif op == "retire_tag":
        retire_tag(p["slug"], note=note, root=root, run_id=run_id)
    elif op == "merge":
        merge(p["loser_ids"], p["winner_id"], root=root, run_id=run_id)
    elif op == "abstract":
        abstract(p["child_ids"], p["title"], p["statement"], root=root, run_id=run_id)
    elif op == "retire":
        retire(p["concept_id"], reason=note, root=root)
    else:                                    # UNREACHABLE: `process` routes split (and any non-auto-applicable
        # op) straight to the 3d queue, so this guards the AUTO_APPLICABLE_OPS invariant rather than handling a
        # live case — a split's per-part evidence partition is the human's, never auto-guessed here.
        raise AssertionError(f"op {op!r} reached _apply_op but is not auto-applicable — process must queue it")


# --- the Block: ONE sharp proposer call per high-tension cluster, per-cluster commit ---------------

class GardenOpsBlock:
    """The gardener's structural-ops phase as a `block.Block` (ADR-0009): a SHARP proposer over the
    high-tension concept clusters, committing PER CLUSTER. `items()` loads the cluster view + the per-concept
    facets/blobs/edges ONCE (one `concept_graph` pass), then yields the clusters of size >= `min_cluster`;
    the driver sorts them by `priority` (cluster TENSION) and caps by --limit. `process()` runs ONE proposer
    call, coerces the proposal, and routes each surviving op on `op_stakes`: LOW → auto-apply (a 3c-i fn),
    HIGH / fuzzy-middle → QUEUE a `garden_proposal` for 3d. The driver writes the per-cluster `processed`
    marker LAST (resumable, fail-in-the-middle); a cluster already gardened against the CURRENT prompt/model is
    done-skipped — `params` carries the idempotency key, exactly like the tagger Block.

    The done-skip keys on the cluster LEADER (`key`), which is a RECALL TRADE, not just a cost saver: a concept
    that JOINS an existing-leader cluster is not re-proposed against its new neighbours until a prompt bump or a
    leader change. A deliberate low-churn choice — one Sonnet call per cluster is expensive, unlike 3b's Haiku
    tagging — and re-gardening is anyway SAFE to skip (an auto-applied op is idempotent, a queued proposal
    re-versions its deterministic id, so no duplicate or corruption). The stronger-recall alternative — key the
    marker on a sorted-members fingerprint — is deferred until recall proves insufficient (ADR-0016)."""

    name = "garden_ops"
    commits_per_item = True
    finalize = block.no_finalize
    marker_extra = block.no_marker_extra

    def __init__(self, propose: Completer, *, model: str = PROPOSE_MODEL,
                 auto_max_stakes: float = AUTO_APPLY_MAX_STAKES, min_cluster: int = 2,
                 root: Path | None = None) -> None:
        self.propose = propose
        self.model = model
        self.auto_max_stakes = auto_max_stakes
        self.min_cluster = min_cluster
        self.root = root or config.data_root()
        # the done-key suffix: a cluster is done for (leader, PROMPT_VERSION, model).
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROPOSE_PROMPT_VERSION), ("model", model))
        # the index loaded ONCE in items() (the run's constant view), read by priority + process.
        self._blob_by_id: dict[str, dict] = {}
        self._facets_by_id: dict[str, dict] = {}
        self._valid: set[str] = set()
        self._asserted: list[dict] = []
        # run-total tallies (instance-scoped; the uniform Report stays stage-agnostic).
        self.n_clusters = 0
        self.n_proposed = 0                             # surviving ops (auto-applied + queued)
        self.n_applied = 0                              # auto-applied (low-stakes) ops
        self.n_queued = 0                               # proposals queued for the 3d gate
        self.applied: list[dict] = []                   # [{op, params, concept_ids, rationale, stakes}]
        self.proposals: list[dict] = []                 # the queued proposal contents (for --show / tests)

    def items(self, root: Path, *, source_id: str | None = None):
        """The concept CLUSTERS (3a facets + 3b tags), frozen at run start — `concept_graph` gives the
        clusters AND the per-node facets in one pass; `load_concepts` the full blobs (statement/evidence).
        `source_id` is ignored (the gardener is a global pass). Only clusters of >= `min_cluster` concepts are
        worth a sharp call (a singleton has nothing to merge/relate)."""
        graph = concepts.concept_graph(root)
        self._blob_by_id = {c["id"]: c for c in dream.load_concepts(root)}
        self._valid = set(self._blob_by_id)
        self._facets_by_id = {n["id"]: n["facets"] for n in graph["nodes"]}
        self._asserted = asserted_edges(root)
        clusters = [cl for cl in graph["clusters"] if len(cl["members"]) >= self.min_cluster]
        self.n_clusters = len(clusters)
        return clusters

    def key(self, cluster: dict) -> str:
        # the cluster's stable id (a valid concept id). Keying the done-skip on the LEADER is a RECALL trade: a
        # cluster that gains a member without changing leader is not re-proposed until a prompt/leader change.
        return cluster["leader"]

    def priority(self, cluster: dict) -> float:
        """Highest-TENSION cluster first (ADR-0011's modular signal) — see `cluster_tension`."""
        return cluster_tension(cluster, self._blob_by_id, self._facets_by_id)

    def process(self, cluster: dict, *, root: Path, run_id: str) -> tuple[int, float]:
        """ONE proposer call → coerce → route each surviving op on `op_stakes`. An op AUTO-APPLIES (a 3c-i fn)
        ONLY when it is BOTH low-stakes AND auto-applicable; everything else — a HIGH / fuzzy-middle op, OR a
        non-auto-applicable one (`split`) even at a raised threshold — QUEUES a `garden_proposal` for 3d
        (recall-first). Returns (n_outputs, cost) for the driver's budget gate; the marker is written LAST."""
        ops, cost = propose_ops(cluster, self.propose, blob_by_id=self._blob_by_id,
                                facets_by_id=self._facets_by_id, asserted=self._asserted,
                                valid_ids=self._valid, root=root)
        n_out = 0
        for desc in ops:
            stakes = _op_stakes_of(desc)
            # AUTO-APPLY needs BOTH gates: low-stakes AND a kind `_apply_op` handles. A `split` (or any future
            # non-auto-applicable op) thus QUEUES regardless of --auto-max-stakes — never routed to `_apply_op`'s
            # raise, so it can't be stranded (neither applied nor queued) at a manually-raised threshold (N1).
            if stakes <= self.auto_max_stakes and desc["op"] in AUTO_APPLICABLE_OPS:
                _apply_op(desc, root=root, run_id=run_id)
                self.n_applied += 1
                self.applied.append({**desc, "stakes": round(stakes, 4)})
            else:                                        # HIGH / fuzzy-middle / non-auto-applicable → QUEUE for 3d
                content, _ = queue_proposal(desc, cluster_leader=cluster["leader"], stakes=stakes,
                                            root=root, run_id=run_id, model=self.model)
                self.n_queued += 1
                self.proposals.append(content)
            n_out += 1
        self.n_proposed += n_out
        return n_out, cost


# --- run: a thin compat shim over the block driver (mirrors GardenBlock/dream) ---------------------

class _OpsReport:
    """The shape `run_propose` returns — a thin WRAPPER over the uniform `block.Report` the driver populated
    plus the GardenOpsBlock instance, exposing every field by reading THROUGH them (no copy → no desync, like
    GardenBlock's `_ShimReport`)."""
    def __init__(self, report: block.Report, blk: GardenOpsBlock) -> None:
        self._report = report
        self._blk = blk

    @property
    def n_clusters(self) -> int:
        return self._blk.n_clusters
    @property
    def n_proposed(self) -> int:
        return self._blk.n_proposed
    @property
    def n_applied(self) -> int:
        return self._blk.n_applied
    @property
    def n_queued(self) -> int:
        return self._blk.n_queued
    @property
    def applied(self) -> list[dict]:
        return self._blk.applied
    @property
    def proposals(self) -> list[dict]:
        return self._blk.proposals

    @property
    def run_id(self) -> str:
        return self._report.run_id
    @property
    def examined(self) -> int:
        return self._report.examined
    @property
    def processed(self) -> int:
        return self._report.processed
    @property
    def skipped(self) -> int:
        return self._report.skipped
    @property
    def errored(self) -> int:
        return self._report.errored
    @property
    def outputs(self) -> int:
        return self._report.outputs
    @property
    def cost_usd(self) -> float:
        return self._report.cost_usd
    @property
    def stopped_on_budget(self) -> bool:
        return self._report.stopped_on_budget


def run_propose(propose: Completer, *, model: str = PROPOSE_MODEL,
                auto_max_stakes: float = AUTO_APPLY_MAX_STAKES, max_usd: float | None = None,
                limit: int | None = None, priority: block.PriorityStrategy | None = None,
                progress: block.Progress | None = None, root: Path | None = None) -> _OpsReport:
    """Propose structural ops over the high-tension clusters — a thin shim over `block.run(GardenOpsBlock(...))`
    (mirrors GardenBlock/dream). The root is resolved ONCE and handed to BOTH the block and the driver. The
    sharp `propose` Completer is injected (Sonnet by default), offline-testable with a fake. `progress`
    defaults to None (silent)."""
    root = config.ensure_layout(root)
    blk = GardenOpsBlock(propose, model=model, auto_max_stakes=auto_max_stakes, root=root)
    report = block.run(blk, max_usd=max_usd, limit=limit, root=root, priority=priority, progress=progress)
    return _OpsReport(report, blk)


# --- CLI: the gardener's phase-2 surface (mirrors GardenBlock.main / dream) ------------------------

def propose_main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="garden propose",
        description="Propose structural ops over high-tension concept clusters: a sharp model proposes "
                    "merge/split/abstract/reparent/retire (+ tag curation); low-stakes auto-apply, "
                    "high-stakes queue for the 3d human gate (LLM).")
    ap.add_argument("--model", default=PROPOSE_MODEL,
                    help=f"claude model for the proposer (default: {PROPOSE_MODEL})")
    ap.add_argument("--limit", type=int, help="cap clusters examined this run (the tension-ordered top)")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this (between clusters)")
    ap.add_argument("--auto-max-stakes", type=float, default=AUTO_APPLY_MAX_STAKES,
                    help="op_stakes at/below which an op auto-applies; above it QUEUES for 3d "
                         f"(default {AUTO_APPLY_MAX_STAKES}; recall-first)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the clusters that would be gardened (skips done); no LLM calls")
    ap.add_argument("--show", action="store_true", help="print each applied op + each queued proposal")
    ap.add_argument("--proposals", action="store_true",
                    help="print the pending proposal queue (the open 3d backlog) and exit")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-cluster progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy over the clusters (default: greedy = highest-tension first)")
    args = ap.parse_args(argv)

    if args.proposals:                                  # just inspect the open 3d queue
        q = open_proposals()
        print(f"{len(q)} pending proposal(s) queued for the 3d gate:")
        for p in q:
            print(f"  {p['proposal_id']}  [{p['op']} · stakes {p['stakes']:.2f}]  {p['concept_ids']}")
            print(f"      {p['rationale']}")
        return

    propose = completer.make_cli_completer(args.model)
    blk = GardenOpsBlock(propose, model=args.model, auto_max_stakes=args.auto_max_stakes)
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, cap=args.max_usd, params=dict(blk.params), out_noun=PROPOSE_OUT_NOUN, verbose=args.verbose)
    report = block.run(blk, max_usd=args.max_usd, limit=args.limit, dry_run=args.dry_run,
                       priority=block.priority_strategy(args.priority), progress=progress)

    if args.dry_run:
        print(f"\ngarden-ops-{report.run_id}: {report.would_process} cluster(s) would garden "
              f"({report.skipped} already done for {PROPOSE_PROMPT_VERSION}/{args.model}).")
        return
    if args.show:
        for d in blk.applied:
            print(f"  applied  [{d['op']} · stakes {d['stakes']:.2f}]  {d['concept_ids']}  {d['rationale']!r}")
        for p in blk.proposals:
            print(f"  queued   [{p['op']} · stakes {p['stakes']:.2f}]  {p['concept_ids']}  {p['rationale']!r}")
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\ngarden-ops-{report.run_id}: {report.examined} clusters, {report.processed} gardened, "
          f"{report.skipped} skipped, {blk.n_applied} auto-applied, {blk.n_queued} queued{errs}, "
          f"${report.cost_usd:.4f}{tail}")


# --- CLI: mirrors the other stages' surface -------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="garden",
        description="Tag the valid concepts from a managed vocabulary, sharpening the concept graph (LLM).")
    ap.add_argument("--model", default=TAG_MODEL, help=f"claude model for the tagger (default: {TAG_MODEL})")
    ap.add_argument("--limit", type=int, help="cap concepts examined this run (the priority-ordered top)")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this (between concepts)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the concepts that would be tagged (skips done); no LLM calls")
    ap.add_argument("--show", action="store_true", help="print each concept's assigned tags")
    ap.add_argument("--vocab", action="store_true", help="print the current controlled vocabulary and exit")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-concept progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy over the concepts (default: greedy = untagged/facet-rich first)")
    args = ap.parse_args(argv)

    if args.vocab:                                      # just inspect the vocabulary fold
        vocab = vocabulary()
        print(f"{len(vocab)} tag(s) · fingerprint {vocab_fingerprint(vocab)}:")
        for slug, gloss in sorted(vocab.items()):
            print(f"  {slug}" + (f" — {gloss}" if gloss else ""))
        return

    tagger = completer.make_cli_completer(args.model)
    blk = GardenBlock(tagger, model=args.model)
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, cap=args.max_usd, params=dict(blk.params), out_noun=OUT_NOUN, verbose=args.verbose)
    report = block.run(blk, max_usd=args.max_usd, limit=args.limit, dry_run=args.dry_run,
                       priority=block.priority_strategy(args.priority), progress=progress)

    if args.dry_run:
        print(f"\ngarden-{report.run_id}: {report.would_process} concept(s) would tag "
              f"({report.skipped} already done for {PROMPT_VERSION}/vocab {blk.fingerprint}).")
        return
    if args.show:
        for a in blk.assignments:
            print(f"  {a['concept_id'][:14]}  {a['tags']}")
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\ngarden-{report.run_id}: {report.examined} examined, {report.processed} tagged, "
          f"{report.skipped} skipped, {blk.n_tagged} got tags, {blk.n_new_tags} new vocab{errs}, "
          f"${report.cost_usd:.4f}{tail}")


if __name__ == "__main__":
    import sys as _sys
    # one module, two gardener phases: `python -m ratchet.garden …` tags (3b); `… propose …` runs the
    # structural-ops proposer (3c-ii). A git-style subcommand keeps the phase-1 surface byte-identical.
    if len(_sys.argv) > 1 and _sys.argv[1] == "propose":
        propose_main(_sys.argv[2:])
    else:
        main()
