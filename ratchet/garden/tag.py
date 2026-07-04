"""garden.tag — the gardener, phase 1: MANAGED TAGS, a cheap-AI grouping signal over concepts (ADR-0014).

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
tags) are the gardener's phase 2 (3c, `garden.ops`/`garden.propose`) — they ACT ON this signal; tagging
here only PRODUCES it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .. import blobstore, block, completer, config, dream
from ..completer import Completer
from ..concepts import concept_facets
from . import slugify
from .ops import _resolve_tags, tag_curation

PROMPT_VERSION = "garden/1"             # bump to re-tag every concept with a sharper prompt (idempotency key)
TAG_MODEL = "haiku"                     # tagging is cheap + per-concept → the small model (dream's router seat)
OUT_NOUN = "assignments"               # the per-item output noun the Progress bar/line shows

GLOSS_MAX = 120                       # the one-line meaning of a tag
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
    age = block.no_age                       # garden's backlog is bounded-SMALL (dozens–hundreds of
                                             # concepts/clusters, not a months-long transcript backlog), so
                                             # Greedy drains it before starvation bites — aging deferred, a
                                             # one-line `def age` opt-in if it ever does (ADR-0021)

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

class _ShimReport(block.ProxyReport):
    """The shape `garden.run` returns — a thin WRAPPER over the uniform `block.Report` the driver populated
    plus the GardenBlock instance, exposing every field by reading THROUGH them (no copy → no desync, like
    glean's `_ShimReport`). The uniform fields proxy the Report (the `block.ProxyReport` base); the
    genuinely-extra tallies proxy the block."""
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
    if report.breaker_tripped:
        tail += "  [stopped: breaker]"
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\ngarden-{report.run_id}: {report.examined} examined, {report.processed} tagged, "
          f"{report.skipped} skipped, {blk.n_tagged} got tags, {blk.n_new_tags} new vocab{errs}, "
          f"${report.cost_usd:.4f}{tail}")
