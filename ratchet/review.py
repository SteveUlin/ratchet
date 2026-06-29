"""review — the human gate: promote synthesized takeaways into reviewed **concepts** (ADR-0008).

    … dream → takeaways → [REVIEW: sulin + Claude] → concepts → generate(skills/CLAUDE.md)

This is the ONE hard gate in the pipeline, and the stage where the loop closes: an accepted takeaway
becomes a concept blob, and dream's next run reads it (`load_concepts`) to judge belief-change. Every
upstream stage is automatic; this one is not — a human decides, because a false concept feeds back
into the system and is far costlier than a missed one.

`review.py` is the pure, testable BACKEND; the human interaction lives in the `/ratchet-review` skill,
where Claude is an active faithfulness-checker (the takeaway's `why` is untrusted — Claude checks it
against the verified evidence and escalates to investigate when a risk signal fires) and the human
makes the call. This module just serves the materials and records the verdict, all on the blob model:

- the **queue** is a derived query, not a stored list (ADR-0007): `dream.current_takeaways` minus
  anything with a terminal decision (accepted/rejected) or a live snooze — references only.
- **evidence** is re-resolved from the immutable blobs and re-validated (the trust chain reaches the
  reviewer: "verified real"), with an optional surrounding-context window for deep investigation.
- a **decision** (accept / reject / snooze / retire) is an append-only decision blob referencing its
  target; an **accept** also ingests the concept it mints and records Claude's assessment + the human's
  call as provenance. State is never a flipped field — it is the latest decision referencing the target.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import blobstore, config, dream

CONCEPT_ID_PREFIX = "c-"
ASSESSMENT_MAX = 2000
CONTEXT_BYTES = 240        # default surrounding-context window for the queue presentation


# --- resolve evidence: the trust chain reaches the reviewer ("verified real") --------------------

def resolve_evidence(takeaway: dict, root: Path, *, context_bytes: int = 0) -> list[dict]:
    """Each cited span, resolved from its immutable cleaned blob and RE-VALIDATED — the verbatim quote
    (trusted) plus, optionally, a surrounding window for the deep path, and the originating session.
    A span that no longer resolves is dropped (the blob was TTL-reclaimed) rather than shown unverified."""
    blobs: dict[str, bytes] = {}
    sessions: dict[str, str | None] = {}
    out: list[dict] = []
    for ev in takeaway.get("evidence") or []:
        ch = ev.get("cleaned_hash")
        if not ch:
            continue
        try:
            data = blobs.get(ch)
            if data is None:
                data = blobstore.get(ch, root).encode("utf-8")
                blobs[ch] = data
        except (FileNotFoundError, OSError):
            continue
        span = blobstore.validate_span(data, ev.get("byte_start"), ev.get("byte_end"))   # the read-side anchor
        if span is None:
            continue
        bs, be = span
        try:
            quote = data[bs:be].decode("utf-8")
        except UnicodeDecodeError:
            continue
        item = {"event_id": ev.get("event_id"), "cleaned_hash": ch, "byte_start": bs, "byte_end": be,
                "quote": quote, "verified": True, "session_id": blobstore.session_of(ch, root, sessions)}
        if context_bytes:
            cs, ce = max(0, bs - context_bytes), min(len(data), be + context_bytes)
            item["context"] = data[cs:ce].decode("utf-8", errors="replace")   # window edges may split a char
        out.append(item)
    return out


def _verified_pointers(takeaway: dict, root: Path) -> list[dict]:
    """The takeaway's evidence projected to stored pointers, but ONLY the spans that re-validate now —
    the same filter the reviewer's view passed through. What an accept bakes into a concept must be
    exactly what was verified, never the raw (possibly malformed/stale) takeaway evidence."""
    return [{"event_id": e["event_id"], "cleaned_hash": e["cleaned_hash"],
             "byte_start": e["byte_start"], "byte_end": e["byte_end"]}
            for e in resolve_evidence(takeaway, root)]


# --- the pending queue: a derived query over references ------------------------------------------

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _snooze_due(d: dict) -> bool:
    """A snooze re-surfaces when its `until` time has passed (the trigger is validated at snooze time,
    so it always parses here). "Re-surface on more evidence" is NOT a snooze trigger: under v2 more
    evidence STRENGTHENS the same stable takeaway id (a new version, latest wins), so a snoozed takeaway
    grows in place — a time trigger is the only re-surface lever the snooze needs to own."""
    until = d.get("until")
    if not until:
        return False
    try:
        dt = datetime.fromisoformat(until)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _now_dt() >= dt
    except (ValueError, TypeError):
        return False


def _is_out_of_queue(d: dict) -> bool:
    """A takeaway leaves the queue when its latest decision is terminal (accept/edit/reject/retire) or
    a snooze that is not yet due. `edit` is an accept that carried changes — also terminal."""
    verb = d.get("verb")
    if verb in ("accept", "edit", "reject", "retire"):
        return True
    if verb == "snooze":
        return not _snooze_due(d)
    return False


def importance(tk: dict, now: str | None = None, *, valid_times: dict | None = None,
               root: Path | None = None) -> float:
    """A takeaway's REVIEW IMPORTANCE — the signal `pending` orders DESCENDING so the human sees the
    highest-leverage calls first (the active-learning "spend your attention where it matters most"; ADR-0022).
    RECENCY-WEIGHTED net entrenchment × confidence: `dream.net_entrenchment` (support sessions minus
    contradicting ones, each weighted by its valid-time — the SAME signal the maturity gate graduates on,
    ADR-0012/0023) scaled by the takeaway's own durability `confidence`. So the queue's order respects
    recency for free: a belief sustained by RECENT corroboration outranks one entrenched only by old
    evidence. `valid_times` is built once in `pending` and threaded so the sort pays the transcript scan
    ONCE, not per takeaway; absent, `net_entrenchment` recomputes it from `root`. Deterministic, and the
    sort is STABLE, so equal-importance takeaways keep their derivation order."""
    conf = tk.get("confidence")
    conf = conf if isinstance(conf, (int, float)) else 0.5
    return dream.net_entrenchment(tk, now, valid_times=valid_times, root=root) * conf


def _evidence_in_topic(evidence: list, topic: str, root: Path, cache: dict) -> bool:
    """Does ANY cited span come from a PROJECT whose name contains `topic` (case-insensitive)? The
    `--topic` queue filter (ADR-0022) — a takeaway/concept spans the sessions it cites, so it matches the
    topic if any one of them does. Same lineage hop dream/glean use: `cleaned_hash` → raw
    `origin_ref.project` (`blobstore.project_of`, one cached meta read per span, no LLM)."""
    t = topic.lower()
    for ev in evidence or []:
        ch = ev.get("cleaned_hash") if isinstance(ev, dict) else None
        if not ch:
            continue
        proj = blobstore.project_of(ch, root, cache)
        if proj and t in proj.lower():
            return True
    return False


def pending(root: Path | None = None, *, context_bytes: int = CONTEXT_BYTES,
            limit: int | None = None, topic: str | None = None) -> list[dict]:
    """The review queue: every MATURE takeaway with no terminal decision and no live snooze, ORDERED by
    IMPORTANCE descending and presented with its verified evidence. Derived from references — stores
    nothing (ADR-0007).

    The MATURITY GATE is `dream.current_takeaways`: it returns only takeaways whose NET distinct-session
    entrenchment (support sessions MINUS contradicting sessions, ADR-0012) crosses `dream.MATURITY_SESSIONS`
    (default 2). An incubating takeaway (a single net session so far) is deliberately kept OUT of the human
    gate — a one-off lesson costs review attention and risks promoting a false belief from a single moment
    — but it stays LIVE in the routing catalog so a later session can strengthen it across the bar; a once-
    mature takeaway that gets CONTESTED un-graduates back out of the queue (never deleted) and re-graduates
    if corroboration returns (see `incubating`).

    ORDERING + the operator knobs (ADR-0022): the queue surfaces in no order otherwise, so `importance`
    (net entrenchment × confidence) sorts it DESCENDING — highest-leverage first. `--topic` filters to a
    PROJECT (substring match over cited evidence); `--limit N` returns the top-N for a one-sitting review.
    Filter + sort run on the RAW takeaways FIRST, so the expensive evidence resolution (`_present`) is paid
    only for the survivors the human will actually see."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)   # lifecycle decisions only — producer markers excluded
    valid_times = dream._session_valid_times(root) # session → valid-time, scanned ONCE for the gate + the sort
    cache: dict = {}
    tks: list[dict] = []
    for tk in dream.current_takeaways(root, valid_times=valid_times):   # the recency-weighted maturity gate
        d = decisions.get(tk["id"])
        if d and _is_out_of_queue(d):
            continue
        if topic is not None and not _evidence_in_topic(tk.get("evidence"), topic, root, cache):
            continue                               # FOCUS: drop takeaways with no evidence from the topic project
        tks.append(tk)
    tks.sort(key=lambda t: importance(t, valid_times=valid_times), reverse=True)   # IMPORTANCE desc; stable ties
    if limit is not None:
        tks = tks[:limit]                          # the top-N for a one-sitting review (after the ordering)
    return [_present(tk, root, context_bytes=context_bytes) for tk in tks]


def incubating(root: Path | None = None) -> list[dict]:
    """The takeaways still BELOW the maturity bar — live in the routing catalog (dream can strengthen
    them), but not yet shown to the human gate. The counterpart to `pending`: `catalog` minus the mature
    set, minus anything already terminally decided. The bar is the recency-WEIGHTED net entrenchment
    (`dream.current_takeaways`/`net_entrenchment`, ADR-0023) — the SAME gate `pending` graduates on, so a
    CONTESTED takeaway that un-graduated re-appears here (not silently lost). Surfaced so the reviewer can
    SEE what is accruing toward review (and decide to act early) without it crowding — or pre-biasing — the
    queue. A light projection (no evidence resolution); `needs` is the count of further distinct sessions to
    cross the bar, the human-legible RAW count (`MATURITY_SESSIONS - net_sessions`) — an integer shortfall,
    not the float weight (a takeaway held below the bar only by AGED evidence can read `needs 0` yet
    incubate; the honest signal there is "needs RECENT corroboration", surfaced by the weighted gate itself)."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    valid_times = dream._session_valid_times(root)
    mature = {t["id"] for t in dream.current_takeaways(root, valid_times=valid_times)}
    out: list[dict] = []
    for tk in dream.catalog(root):
        if tk["id"] in mature:
            continue
        d = decisions.get(tk["id"])
        if d and _is_out_of_queue(d):              # accepted/rejected/etc. directly — no longer accruing
            continue
        sup = tk.get("support") or {"events": 0, "sessions": 0}
        raw_needs = max(0, dream.MATURITY_SESSIONS - dream.net_sessions(tk))
        out.append({"takeaway_id": tk["id"], "title": tk.get("title", ""), "support": sup,
                    "needs": raw_needs,                    # distinct-session shortfall on the RAW count, but …
                    "needs_recent": raw_needs == 0})       # … if 0 yet incubating, it's held below the bar by
                    # AGED evidence (recency-weighted gate, ADR-0023) — it needs RECENT corroboration, not more
                    # old sessions; the flag stops the queue reading a misleading "needs 0".
    return out


def _present(takeaway: dict, root: Path, *, context_bytes: int) -> dict:
    """The reviewer's view of one takeaway: the synthesis (untrusted) + its verified evidence."""
    return {
        "takeaway_id": takeaway["id"],
        "title": takeaway.get("title", ""),
        "why": takeaway.get("why", ""),
        "relation": takeaway.get("relation") or {"kind": "new", "concept_id": None},
        "support": takeaway.get("support") or {"events": 0, "sessions": 0},
        "markers": takeaway.get("markers") or {},
        "confidence": takeaway.get("confidence"),
        "evidence": resolve_evidence(takeaway, root, context_bytes=context_bytes),
    }


def context_for(takeaway_id: str, root: Path | None = None, *, context_bytes: int = 1200) -> dict | None:
    """One takeaway with a WIDE evidence window — the deep path: when Claude escalates to investigate
    (thin support, a `why` that overreaches its quotes, a contradiction), it reads the surrounding
    transcript here rather than trusting the one-line synthesis."""
    root = root or config.data_root()
    tk = _load_takeaway(takeaway_id, root)
    return _present(tk, root, context_bytes=context_bytes) if tk else None


# --- decisions: append-only blobs that drive every derived view ----------------------------------

def _load_takeaway(takeaway_id: str, root: Path) -> dict | None:
    h = blobstore.latest_version(takeaway_id, root)
    if not h:
        return None
    try:
        return json.loads(blobstore.get(h, root))
    except (OSError, json.JSONDecodeError):
        return None


def _record(verb: str, target: str, root: Path, **fields) -> dict:
    """Append a decision blob referencing `target`. The body is unique per fact (`at` + `run_id`) so
    two same-verb decisions never collapse to one content-addressed blob; its source_id is its own
    hash ("a unique fact — hash is its id", ADR-0007). The recency fold keys on `meta.fetched_at`, so
    we pass the SAME `at` value there — the audited timeline and the folded timeline never diverge.
    Decisions are never versioned (prev=None)."""
    at = config.now()
    body = {"verb": verb, "target": target, "at": at, "run_id": config.run_id(), **fields}
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s),
                     origin_ref={"stage": "review", "verb": verb, "target": target},
                     fetched_at=at, prev=None, root=root)
    return body


def _mint_concept_id(takeaway: dict) -> str:
    """A fresh, stable concept id for a `new` takeaway. Derived from the accepted takeaway's identity +
    title — and in v2 the takeaway id is itself stable (a minted `t-…`, versioned in place), so this
    concept id is stable across re-reads. A later refinement reuses it via `relation.concept_id`, never
    re-mints."""
    return CONCEPT_ID_PREFIX + hashlib.sha256(
        f"{takeaway['id']}:{takeaway.get('title', '')}".encode()).hexdigest()[:12]


def accept(takeaway_id: str, root: Path | None = None, *, edited: dict | None = None,
           assessment: str = "", reviewer: str = "sulin", note: str = "",
           allow_no_evidence: bool = False) -> str:
    """Promote a takeaway to a concept (the loop closes here) and record the accept. The concept stores
    ONLY the evidence that re-validates *now* — exactly the verified spans the reviewer saw, never the
    raw (possibly malformed/stale) takeaway evidence — so the trust chain reaches the concept, the
    system's most trusted artifact. A takeaway with no resolvable evidence is REFUSED (a belief with no
    anchor would feed dream/generate) unless `allow_no_evidence` overrides. Identity comes from the
    takeaway's `relation`: `strengthens`/`refines` an EXISTING concept → a new version of it; otherwise
    (incl. a stale/unknown concept_id) mint fresh. `edited` ({title?, why?}) corrects the synthesis
    before it becomes a concept, captured before/after. Returns the concept id."""
    root = root or config.data_root()
    tk = _load_takeaway(takeaway_id, root)
    if tk is None:
        raise ValueError(f"no takeaway {takeaway_id!r}")
    evidence = _verified_pointers(tk, root)         # only spans that re-validate (the reviewer's filter)
    if not evidence and not allow_no_evidence:
        raise ValueError(f"takeaway {takeaway_id!r} has no resolvable evidence — refusing to mint a "
                         f"concept with no verifiable backing (override with allow_no_evidence=True)")
    edited = edited or {}
    title = edited.get("title", tk.get("title", ""))
    statement = edited.get("why", tk.get("why", ""))
    rel = tk.get("relation") or {}
    known = dream.valid_concept_ids(root)
    concept_id = (rel["concept_id"] if rel.get("kind") in ("strengthens", "refines")
                  and isinstance(rel.get("concept_id"), str) and rel["concept_id"] in known
                  else _mint_concept_id(tk))        # reuse only an EXISTING concept; else mint fresh
    concept = {"id": concept_id, "title": title, "statement": statement,
               "evidence": evidence, "source_takeaway": takeaway_id}
    ch, _ = blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=concept_id,
                             origin_ref={"stage": "review", "reviewer": reviewer, "takeaway": takeaway_id}, root=root)
    fields = {"concept": concept_id, "concept_hash": ch, "reviewer": reviewer,
              "assessment": str(assessment)[:ASSESSMENT_MAX], "note": str(note)[:ASSESSMENT_MAX]}
    if edited:
        fields["edited"] = {"before": {"title": tk.get("title", ""), "why": tk.get("why", "")},
                            "after": {"title": title, "why": statement}}
    _record("edit" if edited else "accept", takeaway_id, root, **fields)
    return concept_id


def reject(takeaway_id: str, root: Path | None = None, *, reason: str = "", assessment: str = "",
           reviewer: str = "sulin") -> None:
    """Reject a takeaway. Persisted as a label (no fine-tuning): a future PROMPT_VERSION bump can fold
    rejections into negative few-shot + suppress semantic near-dupes, fixing the 're-suggests dismissed
    things' trust-killer (MemPrompt)."""
    _record("reject", takeaway_id, root or config.data_root(),
            reason=str(reason)[:ASSESSMENT_MAX], assessment=str(assessment)[:ASSESSMENT_MAX], reviewer=reviewer)


def snooze(takeaway_id: str, root: Path | None = None, *, until: str, reason: str = "",
           reviewer: str = "sulin") -> None:
    """Defer a takeaway until a concrete time. `until` is VALIDATED as ISO here, at write time — an
    unparseable trigger would otherwise never fire and the snooze would become a permanent graveyard,
    the exact failure mode the trigger exists to prevent. (Re-surfacing on *more evidence* is not a
    snooze concern: more evidence strengthens the same stable takeaway in place — see `_snooze_due`.)"""
    if not until:
        raise ValueError("snooze needs a re-surface time: --until <iso>")
    try:
        datetime.fromisoformat(until)
    except (ValueError, TypeError):
        raise ValueError(f"--until {until!r} is not an ISO datetime")
    _record("snooze", takeaway_id, root or config.data_root(),
            until=until, reason=str(reason)[:ASSESSMENT_MAX], reviewer=reviewer)


def retire(concept_id: str, root: Path | None = None, *, reason: str = "", reviewer: str = "sulin") -> None:
    """Take a concept OUT of the valid set (a contradiction the reviewer affirms). Not a deletion — the
    concept blob and its history stay; its latest lifecycle decision being `retire` drops it from
    `load_concepts`/`valid_concepts`. (Re-establishing a retired concept is a future manual action:
    dream stops surfacing it, so a refinement won't normally re-reference it — ADR-0008.)"""
    _record("retire", concept_id, root or config.data_root(),
            reason=str(reason)[:ASSESSMENT_MAX], reviewer=reviewer)


def valid_concepts(root: Path | None = None) -> list[dict]:
    """The current valid concept set — what dream judges belief-change against and `generate` projects
    to skills/CLAUDE.md. Same derivation as `dream.load_concepts` (kept there to avoid a review→dream
    cycle); re-exported here for inspection."""
    return dream.load_concepts(root)


# --- the SECOND review tier: the gardener's queued structural-op proposals (3d, ADR-0017) --------
#
# Tier 1 (above) promotes synthesized TAKEAWAYS into concepts. Tier 2 here gates the gardener's
# STRUCTURAL ops (merge/split/abstract/reparent/retire of concepts + tag curation) — the high-stakes
# proposals 3c-ii (ADR-0016) QUEUED rather than auto-applied. Same human-gate shape (ADR-0008): the
# op's RATIONALE is UNTRUSTED (the exact status of a takeaway's `why`), so the reviewer is served the
# CITED concepts' RE-VALIDATED evidence as the ground truth, accept APPLIES the op via the trusted 3c-i
# machinery, and reject SUPPRESSES re-surfacing (the gardener remembers dismissals — `queue_proposal`).
#
# `garden` is imported FUNCTION-LOCALLY everywhere below: garden imports review (for `resolve_evidence`)
# at module load, so a top-level review→garden import would cycle — the same break `concepts`↔`garden`
# uses. review serves the materials + records the verdict; the blob shapes + op fns live in garden.


def _present_proposal(proposal: dict, root: Path, *, context_bytes: int) -> dict:
    """The reviewer's view of one structural-op proposal: the op + params + UNTRUSTED rationale, plus EACH
    cited concept with its title/statement, whether it is still VALID, and its RE-VALIDATED evidence — the
    trust chain reaches the reviewer so the faithfulness check (does the rationale FOLLOW from the evidence?)
    has ground truth to judge against. A `retire`/`merge` of a still-valid concept is visible via the `valid`
    flag the skill escalates on. A cited concept whose blob is gone shows empty evidence — the gap is shown,
    never silently dropped."""
    from . import garden
    valid = dream.valid_concept_ids(root)
    cited: list[dict] = []
    for cid in proposal.get("concept_ids") or []:
        blob = garden._concept_blob(cid, root) or {}    # the latest concept version, valid OR not
        cited.append({
            "concept_id": cid,
            "title": blob.get("title", ""),
            "statement": blob.get("statement", ""),
            "valid": cid in valid,
            "evidence": resolve_evidence({"evidence": blob.get("evidence") or []}, root,
                                         context_bytes=context_bytes),
        })
    return {
        "proposal_id": proposal.get("proposal_id"),
        "op": proposal.get("op"),
        "params": proposal.get("params") or {},
        "rationale": proposal.get("rationale", ""),
        "stakes": proposal.get("stakes"),
        "cluster_leader": proposal.get("cluster_leader"),
        "concepts": cited,
    }


def _proposal_in_topic(proposal: dict, topic: str, root: Path, cache: dict) -> bool:
    """Does a structural-op proposal touch the `topic` PROJECT? It cites concepts, not events, so the hop
    is one longer: each cited concept → its evidence spans → `cleaned_hash` → raw `origin_ref.project`
    (`_evidence_in_topic`). True if ANY cited concept has evidence from a project matching the substring."""
    from . import garden
    for cid in proposal.get("concept_ids") or []:
        blob = garden._concept_blob(cid, root) or {}
        if _evidence_in_topic(blob.get("evidence"), topic, root, cache):
            return True
    return False


def pending_proposals(root: Path | None = None, *, context_bytes: int = CONTEXT_BYTES,
                      limit: int | None = None, topic: str | None = None) -> list[dict]:
    """The SECOND review tier: the gardener's open structural-op proposals (`garden.open_proposals`), ORDERED
    by STAKES descending and each rendered for the human with its op + params + rationale and the CITED
    concepts' RE-VALIDATED evidence. The rationale is UNTRUSTED (a proposer's justification, like a takeaway's
    `why`); the EVIDENCE is the ground truth the trust chain carries to the reviewer (re-resolved via
    `resolve_evidence`, a stale span dropped). The rich VIEW over the raw `garden.open_proposals` source.
    Distinct from the takeaway `pending` tier — a parallel queue with its own accept/reject verbs.

    ORDERING + the operator knobs (ADR-0022): proposals carry `stakes` (the 3c-ii fuzzy gradient — how much
    an op changes what concepts EXIST/ASSERT, `garden.op_stakes`), so the queue sorts by it DESCENDING —
    the highest-leverage restructurings first. (The cluster's `tension` that DROVE a proposal is a propose-
    time signal NOT carried on the proposal blob, so `stakes` is the on-proposal importance stand-in.)
    `--topic` filters to a PROJECT (substring over the cited concepts' evidence); `--limit N` takes the
    top-N. Filter + sort run on the raw proposals FIRST, so the per-concept evidence resolution is paid only
    for the survivors."""
    from . import garden
    root = root or config.data_root()
    props = garden.open_proposals(root)
    if topic is not None:
        cache: dict = {}
        props = [p for p in props if _proposal_in_topic(p, topic, root, cache)]
    # stakes DESCENDING (a missing/non-numeric stakes sinks to 0.0); stable → ties keep open_proposals order.
    props = sorted(props, key=lambda p: p.get("stakes") if isinstance(p.get("stakes"), (int, float)) else 0.0,
                   reverse=True)
    if limit is not None:
        props = props[:limit]
    return [_present_proposal(p, root, context_bytes=context_bytes) for p in props]


def _apply_proposal(proposal: dict, *, root: Path, run_id: str, reviewer: str,
                    split_parts: list[dict] | None, allow_no_evidence: bool):
    """APPLY a proposal's op by calling the matching 3c-i `garden` fn with the proposal's params — the SAME
    trusted, append-only machinery 3c-ii auto-applies the low-stakes ops through, so accept and auto-apply land
    byte-identical effects. Returns the op's own result (winner id / new part ids / parent id / edge hash / None).

    `split` is the one op the proposal cannot fully carry: its per-part EVIDENCE PARTITION is the human's to
    choose (why a split is never auto-applied — ADR-0016 N1), and the queued params hold only {title, statement}
    per part. So the reviewer supplies `split_parts` ({title, statement, evidence} each); absent it we fall back
    to the queued parts (no evidence → `garden.split` REFUSES unless `allow_no_evidence`), surfacing the
    requirement rather than guessing a partition."""
    from . import garden
    op, p = proposal["op"], proposal.get("params") or {}
    note = proposal.get("rationale", "")
    if op == "merge":
        return garden.merge(p["loser_ids"], p["winner_id"], root=root, run_id=run_id,
                            allow_no_evidence=allow_no_evidence)
    if op == "split":
        parts = split_parts if split_parts is not None else (p.get("parts") or [])
        return garden.split(p["concept_id"], parts, root=root, run_id=run_id,
                           allow_no_evidence=allow_no_evidence)
    if op == "abstract":
        return garden.abstract(p["child_ids"], p["title"], p.get("statement", ""), root=root,
                              run_id=run_id, allow_no_evidence=allow_no_evidence)
    if op == "reparent":
        garden.reparent(p["concept_id"], p["parent_id"], root=root, run_id=run_id)
        return None
    if op == "retire":
        garden.retire(p["concept_id"], reason=note, reviewer=reviewer, root=root)
        return None
    if op == "relate":
        return garden.assert_edge(p["src"], "relates-to", p["dst"], note=note, root=root,
                                  run_id=run_id, op="accept_proposal")[0]
    if op == "merge_tags":
        return garden.merge_tags(p["loser_slug"], p["winner_slug"], note=note, root=root, run_id=run_id)[0]
    if op == "retire_tag":
        return garden.retire_tag(p["slug"], note=note, root=root, run_id=run_id)[0]
    raise ValueError(f"cannot apply proposal op {op!r}")


def accept_proposal(proposal_id: str, root: Path | None = None, *, assessment: str = "",
                    reviewer: str = "sulin", note: str = "", split_parts: list[dict] | None = None,
                    allow_no_evidence: bool = False) -> dict:
    """Accept a queued structural-op proposal: APPLY the op (its 3c-i `garden` fn) so the concept graph
    reorganizes — a `merge` unions the winner + invalidates the losers, a `retire` drops a concept, etc. —
    THEN RECORD the accept (reviewer + Claude's faithfulness assessment). No status flip: the accept DECISION
    is the proposal's whole lifecycle (`garden.open_proposals` derives RESOLVED from
    `latest_decision(pid).verb in RESOLVE_VERBS`), so it drops from the open queue with no field to write —
    byte-symmetric with tier-1's `accept`. Apply runs FIRST: a refused op (e.g. a split whose parts re-validate
    to nothing) raises and leaves the proposal OPEN and unrecorded, never half-resolved. Tier 2's accept beside
    the takeaway `accept`. Returns {proposal_id, op, status, result}."""
    from . import garden
    root = root or config.data_root()
    proposal = garden._proposal_blob(proposal_id, root)
    if proposal is None:
        raise ValueError(f"no garden proposal {proposal_id!r}")
    run_id = config.run_id()
    result = _apply_proposal(proposal, root=root, run_id=run_id, reviewer=reviewer,
                             split_parts=split_parts, allow_no_evidence=allow_no_evidence)
    _record("accept_proposal", proposal_id, root, op=proposal["op"], reviewer=reviewer,
            assessment=str(assessment)[:ASSESSMENT_MAX], note=str(note)[:ASSESSMENT_MAX],
            result=result if isinstance(result, (str, list)) else None)
    return {"proposal_id": proposal_id, "op": proposal["op"], "status": "accepted", "result": result}


def reject_proposal(proposal_id: str, root: Path | None = None, *, reason: str = "",
                    assessment: str = "", reviewer: str = "sulin") -> dict:
    """Reject a queued structural-op proposal — RECORD the reject; the op is NOT applied, the concept graph is
    untouched. No status flip: the reject DECISION is the proposal's lifecycle (`garden.open_proposals` drops
    it via `RESOLVE_VERBS`), and `garden.queue_proposal` reads that same decision so a re-gardened cluster never
    re-opens a dismissed op — the L2 loop closing, its full why at `queue_proposal`. Returns {proposal_id, op,
    status}."""
    from . import garden
    root = root or config.data_root()
    proposal = garden._proposal_blob(proposal_id, root)
    if proposal is None:
        raise ValueError(f"no garden proposal {proposal_id!r}")
    _record("reject_proposal", proposal_id, root, op=proposal["op"], reviewer=reviewer,
            reason=str(reason)[:ASSESSMENT_MAX], assessment=str(assessment)[:ASSESSMENT_MAX])
    return {"proposal_id": proposal_id, "op": proposal["op"], "status": "rejected"}


# --- CLI: the thin surface the /ratchet-review skill drives --------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="review",
                                 description="The human review gate: takeaways → reviewed concepts.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pending", action="store_true", help="the review queue (mature takeaways + verified evidence)")
    g.add_argument("--incubating", action="store_true",
                   help="takeaways still below the maturity bar (accruing toward review, not yet shown)")
    g.add_argument("--context", metavar="TAKEAWAY", help="one takeaway with a WIDE evidence window (deep path)")
    g.add_argument("--accept", metavar="TAKEAWAY", help="promote a takeaway to a concept")
    g.add_argument("--reject", metavar="TAKEAWAY", help="reject a takeaway")
    g.add_argument("--snooze", metavar="TAKEAWAY", help="defer a takeaway (needs --until)")
    g.add_argument("--retire", metavar="CONCEPT", help="take a concept out of the valid set")
    g.add_argument("--concepts", action="store_true", help="the current valid concept set")
    g.add_argument("--proposals", action="store_true",
                   help="the structural-op proposal queue (3d: op + rationale + cited concepts' evidence)")
    g.add_argument("--accept-proposal", metavar="PROPOSAL",
                   help="accept a structural-op proposal — APPLY the op via the 3c-i machinery")
    g.add_argument("--reject-proposal", metavar="PROPOSAL",
                   help="reject a structural-op proposal (NOT applied; suppresses re-surfacing)")
    ap.add_argument("--json", action="store_true", help="machine-readable output (the skill uses this)")
    # the operator knobs (ADR-0022) — apply to --pending and --proposals: a PRIORITIZED, SCOPED subset.
    ap.add_argument("--limit", type=int, metavar="N",
                    help="review the top-N only (by importance for takeaways, stakes for proposals)")
    ap.add_argument("--topic", help="filter the queue to a PROJECT whose name contains this substring")
    ap.add_argument("--split-parts",
                    help="a split accept's per-part EVIDENCE PARTITION: JSON [{title,statement,evidence}] "
                         "(the human's to choose — a queued split carries only title/statement)")
    ap.add_argument("--bytes", type=int, default=None, help="surrounding-context window size")
    ap.add_argument("--edit-title", help="accept with a corrected title")
    ap.add_argument("--edit-why", help="accept with a corrected why")
    ap.add_argument("--allow-no-evidence", action="store_true",
                    help="accept a takeaway with no resolvable evidence (a deliberate, recorded override)")
    ap.add_argument("--assessment", default="", help="Claude's faithfulness assessment (recorded as provenance)")
    ap.add_argument("--note", default="", help="a reviewer note")
    ap.add_argument("--reason", default="", help="reason for reject/snooze/retire")
    ap.add_argument("--until", help="snooze re-surface time (ISO)")
    args = ap.parse_args(argv)

    if args.pending:
        q = pending(context_bytes=args.bytes if args.bytes is not None else CONTEXT_BYTES,
                    limit=args.limit, topic=args.topic)
        if args.json:
            print(json.dumps(q, ensure_ascii=False, indent=2))   # a LIST — the skill iterates it
        else:
            _print_queue(q, incubating_count=len(incubating()))
    elif args.incubating:
        inc = incubating()
        if args.json:
            print(json.dumps(inc, ensure_ascii=False, indent=2))
        else:
            _print_incubating(inc)
    elif args.context:
        c = context_for(args.context, context_bytes=args.bytes if args.bytes is not None else 1200)
        if c is None:
            ap.error(f"no takeaway {args.context!r}")
        print(json.dumps(c, ensure_ascii=False, indent=2))
    elif args.accept:
        edited = {}
        if args.edit_title is not None:
            edited["title"] = args.edit_title
        if args.edit_why is not None:
            edited["why"] = args.edit_why
        cid = accept(args.accept, edited=edited or None, assessment=args.assessment, note=args.note,
                     allow_no_evidence=args.allow_no_evidence)
        print(json.dumps({"accepted": args.accept, "concept": cid, "edited": bool(edited)})
              if args.json else f"accepted → concept {cid}{' (edited)' if edited else ''}")
    elif args.reject:
        reject(args.reject, reason=args.reason, assessment=args.assessment)
        print(f"rejected {args.reject}")
    elif args.snooze:
        snooze(args.snooze, until=args.until, reason=args.reason)
        print(f"snoozed {args.snooze} until {args.until}")
    elif args.retire:
        retire(args.retire, reason=args.reason)
        print(f"retired concept {args.retire}")
    elif args.concepts:
        cs = valid_concepts()
        print(json.dumps(cs, ensure_ascii=False, indent=2) if args.json
              else "\n".join(f"  {c['id']}  {c.get('title', '')}" for c in cs) or "  (no valid concepts yet)")
    elif args.proposals:
        q = pending_proposals(context_bytes=args.bytes if args.bytes is not None else CONTEXT_BYTES,
                              limit=args.limit, topic=args.topic)
        if args.json:
            print(json.dumps(q, ensure_ascii=False, indent=2))   # a LIST — the skill iterates it
        else:
            _print_proposals(q)
    elif args.accept_proposal:
        split_parts = json.loads(args.split_parts) if args.split_parts else None
        res = accept_proposal(args.accept_proposal, assessment=args.assessment, note=args.note,
                              split_parts=split_parts, allow_no_evidence=args.allow_no_evidence)
        print(json.dumps(res, ensure_ascii=False) if args.json
              else f"accepted proposal {res['proposal_id']} → applied {res['op']}"
                   + (f" → {res['result']}" if res["result"] else ""))
    elif args.reject_proposal:
        res = reject_proposal(args.reject_proposal, reason=args.reason, assessment=args.assessment)
        print(json.dumps(res, ensure_ascii=False) if args.json
              else f"rejected proposal {res['proposal_id']} ({res['op']}) — not applied, won't re-surface")


def _incubating_tail(incubating_count: int) -> str:
    """A one-line footer noting how many takeaways are still accruing below the maturity bar — so an
    empty/short queue does not read as 'dream learned nothing' when lessons are in fact incubating."""
    if not incubating_count:
        return ""
    return (f"\n({incubating_count} takeaway(s) incubating below the maturity bar — "
            f"see `--incubating`)")


def _print_queue(q: list[dict], *, incubating_count: int = 0) -> None:
    if not q:
        print("review queue empty — nothing to review." + _incubating_tail(incubating_count))
        return
    print(f"{len(q)} takeaway(s) to review:\n")
    for i, t in enumerate(q, 1):
        sup, rel = t["support"], t["relation"]["kind"]
        print(f"{i}/{len(q)} · {t['title']}  [{rel} · {sup['events']}ev/{sup['sessions']}sess]")
        print(f"  WHY: {t['why']}")
        for ev in t["evidence"]:
            print(f"    ✓ {ev['quote'][:100]!r}")
        print()
    tail = _incubating_tail(incubating_count)
    if tail:
        print(tail.lstrip("\n"))


def _print_proposals(q: list[dict]) -> None:
    """The structural-op proposal queue, with each cited concept's verified evidence inline (✓) — the human
    judges whether the UNTRUSTED rationale follows from that ground truth. A still-valid `retire`/`merge`
    target is flagged, the skill's escalation cue."""
    if not q:
        print("no structural-op proposals queued — nothing to review.")
        return
    print(f"{len(q)} structural-op proposal(s) to review:\n")
    for i, p in enumerate(q, 1):
        st = f" · stakes {p['stakes']:.2f}" if isinstance(p.get("stakes"), (int, float)) else ""
        print(f"{i}/{len(q)} · {p['op']}{st}  [{p['proposal_id']}]")
        print(f"  RATIONALE (untrusted): {p['rationale']}")
        print(f"  PARAMS: {p['params']}")
        for c in p["concepts"]:
            flag = "" if c["valid"] else "  (no longer valid)"
            print(f"    concept {c['concept_id']}{flag}: {c['title']}")
            for ev in c["evidence"]:
                print(f"      ✓ {ev['quote'][:100]!r}")
        print()


def _print_incubating(inc: list[dict]) -> None:
    if not inc:
        print("nothing incubating — every live takeaway has reached the maturity bar.")
        return
    print(f"{len(inc)} takeaway(s) incubating (below the maturity bar):\n")
    for t in inc:
        sup = t["support"]
        print(f"  {t['title']}  [{sup['events']}ev/{sup['sessions']}sess · needs {t['needs']} more session(s)]")


if __name__ == "__main__":
    main()
