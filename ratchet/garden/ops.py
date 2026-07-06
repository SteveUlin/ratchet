"""garden.ops — the LLM-FREE gardener machinery (3c-i, ADR-0015): asserted edges, tag-vocab curation,
the deterministic structural ops (merge/split/abstract/reparent/retire + merge_tags/retire_tag), the
op-stakes gradient, and the append-only proposal QUEUE. NO model here — this is the trust-critical,
append-only foundation the 3c-ii proposer (`garden.propose`) DRIVES and the 3d human gate ACCEPTS.

Everything is the BLOB model (ADR-0007): state is DERIVED by folding append-only artifacts, never a
stored mutable set. Two pieces (both append-only folds):

  ASSERTED EDGES — a `concept_edge` blob is the gardener's DELIBERATE claim that two concepts relate
    (generalizes/supersedes/relates-to — ONE canonical hierarchy direction, `generalizes`; its inverse
    is read by reversing the edge), keyed on the edge identity `src|kind|dst`, latest-wins, retract = a
    new version with active:false. Distinct from 3a's DERIVED edges (recompute from provenance on every
    read, never stored). `generalizes` defines the hierarchy spine; `concepts.concept_graph` folds the
    active edges in alongside the derived ones.

  THE OPS — merge/split/abstract/reparent/retire of concepts + merge_tags/retire_tag of the vocab.
    Every op INGESTS new blobs/decisions ONLY; nothing is ever deleted. A concept leaves the valid set
    by a `supersede`/`split`/`retire` DECISION (`load_concepts` folds it out; the blob + history stay) —
    invalidate-don't-delete (Zep/AGM; dream's merge/WEAKEN, ADR-0012). Evidence is UNIONED/subset and
    RE-VALIDATED on write (review.accept's `_verified_pointers` discipline), so the trust chain — cited
    evidence → validated span → immutable blob — reaches every minted/versioned concept.

The proposal QUEUE (`queue_proposal`/`open_proposals`) is the same blob/fold shape: a `garden_proposal`
keyed on a DETERMINISTIC proposal id (the op's identity), latest-wins, with NO lifecycle field — its
resolution is a separate 3d decision (`RESOLVE_VERBS`), derived, never a stored status (ADR-0016/0017).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import blobstore, concepts, config, review
from . import slugify

# The asserted-edge kinds, ONE canonical direction each — `generalizes` is the hierarchy spine (its
# inverse `specializes` is NOT stored: read it by reversing a `generalizes` edge, so the two directions
# can never disagree), `supersedes` is lineage, `relates-to` is association. `assert_edge` REJECTS any
# other kind: in this append-only trust-critical store an unknown kind is a producer bug (a future 3c-ii
# typo), not data to silently fold.
ASSERTED_EDGE_KINDS = ("generalizes", "supersedes", "relates-to")
EDGE_KIND = "concept_edge"             # an asserted inter-concept edge blob (vs 3a's derived edges)
EDGE_SEP = "|"                         # edge identity = src|kind|dst (concept ids + kinds carry no `|`)
TAG_CURATION_KIND = "tag_curation"     # a vocab-curation redirect blob (merge_tags / retire_tag)
NOTE_MAX = 160                         # a short human note on an op/edge (matches concepts.NOTE_MAX)


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

def concept_blob(concept_id: str, root: Path) -> dict | None:
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
    agree). `concepts.load_concepts` folds `supersede`/`split` (with `retire`) out of the valid set — the
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
    valid = concepts.valid_concept_ids(root)               # the membership gate — winner AND losers
    winner = concept_blob(winner_id, root)
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
        lc = concept_blob(lid, root)
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
        _write_concept_decision(concepts.VERB_SUPERSEDE, lid, root, run_id=run_id, into=winner_id, op="merge")
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
    orig = concept_blob(concept_id, root)
    if orig is None or concept_id not in concepts.valid_concept_ids(root):
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
    _write_concept_decision(concepts.VERB_SPLIT, concept_id, root, run_id=run_id, parts=new_ids, op="split")
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
    valid = concepts.valid_concept_ids(root)
    children = [c for c in child_ids if c in valid and concept_blob(c, root) is not None]
    if not children:
        raise ValueError("abstract needs at least one valid child concept")
    if evidence is None:                                # default: UNION the children's evidence
        pooled: list[dict] = []
        for c in children:
            pooled.extend((concept_blob(c, root) or {}).get("evidence") or [])
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
    valid = concepts.valid_concept_ids(root)
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
# THE PROPOSAL QUEUE (3c-i, ADR-0016/0017): an append-only `garden_proposal` blob the 3c-ii proposer
# (`garden.propose`) and the deterministic staleness pass both QUEUE a high-stakes op into, for the 3d
# human gate. Keyed on a DETERMINISTIC proposal id (the op's identity), latest-wins — a QUEUED artifact
# with NO lifecycle field; `open_proposals` folds the queue and drops any proposal a 3d resolve DECISION
# (accept/reject) closed. The queue machinery is LLM-FREE; the proposer that FEEDS it lives in `propose`.
# ==================================================================================================

PROPOSE_PROMPT_VERSION = "garden-ops/1"   # bump to re-propose over every cluster with a sharper prompt. The
                                          # queue writer (`queue_proposal`) STAMPS it into every proposal blob
                                          # (the proposal's schema), so it lives HERE with the queue, not with
                                          # the proposer — `garden.propose` imports it for its done-key.

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


def mint_proposal_id(desc: dict) -> str:
    """A DETERMINISTIC proposal id from the op's IDENTITY (kind + params, NOT the rationale): a re-proposal of
    the SAME structural edit re-versions the SAME `garden_proposal` (latest-wins, no duplicate), while a
    changed rationale is just a new version — dream's stable-minted-id resumability discipline, one level up."""
    identity = blobstore.canonical_json({"op": desc["op"], "params": desc["params"]})
    return PROPOSAL_PREFIX + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def proposal_blob(proposal_id: str, root: Path | None = None) -> dict | None:
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
                   run_id: str, model: str) -> tuple[str | None, bool]:
    """QUEUE a high-stakes op as an append-only `garden_proposal` blob for the 3d gate (NOT applied). Keyed on
    the deterministic proposal id, latest-wins; a QUEUED artifact with NO lifecycle field — its resolution is a
    separate 3d decision (`RESOLVE_VERBS`), never a status on the blob. CONTENT is run-invariant ({proposal_id,
    op, params, concept_ids, rationale, stakes, cluster_leader, prompt_version}) so a crash-retry re-ingests
    byte-identically; the producer/cost ride in `origin_ref`. The rationale rides in the body as the
    human-facing justification — UNTRUSTED, surfaced to 3d, never acted on here. Returns (hash, written); the
    suppressed path returns the proposal's existing latest version — None when none was ever stored.

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
