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
    so it always parses here). "Re-surface on more evidence" is NOT a snooze trigger: more evidence
    grows a cluster → a new `cluster_signature` → a fresh takeaway that surfaces on its own, while the
    snoozed one is superseded — so a corroboration counter on a now-frozen id would be inert."""
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


def pending(root: Path | None = None, *, context_bytes: int = CONTEXT_BYTES) -> list[dict]:
    """The review queue: every current takeaway with no terminal decision and no live snooze, each
    presented with its verified evidence. Derived from references — stores nothing (ADR-0007)."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)   # lifecycle decisions only — producer markers excluded
    out: list[dict] = []
    for tk in dream.current_takeaways(root):
        d = decisions.get(tk["id"])
        if d and _is_out_of_queue(d):
            continue
        out.append(_present(tk, root, context_bytes=context_bytes))
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
    title (stable across re-reads), NOT the cluster_signature alone (which shifts as the cluster
    grows) — a later refinement reuses this id via the takeaway's `relation.concept_id`, never re-mints."""
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
    known = {c["id"] for c in dream.load_concepts(root)}
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
    snooze concern: that grows the cluster into a fresh takeaway via supersession — see `_snooze_due`.)"""
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


# --- CLI: the thin surface the /ratchet-review skill drives --------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="review",
                                 description="The human review gate: takeaways → reviewed concepts.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pending", action="store_true", help="the review queue (takeaways + verified evidence)")
    g.add_argument("--context", metavar="TAKEAWAY", help="one takeaway with a WIDE evidence window (deep path)")
    g.add_argument("--accept", metavar="TAKEAWAY", help="promote a takeaway to a concept")
    g.add_argument("--reject", metavar="TAKEAWAY", help="reject a takeaway")
    g.add_argument("--snooze", metavar="TAKEAWAY", help="defer a takeaway (needs --until)")
    g.add_argument("--retire", metavar="CONCEPT", help="take a concept out of the valid set")
    g.add_argument("--concepts", action="store_true", help="the current valid concept set")
    ap.add_argument("--json", action="store_true", help="machine-readable output (the skill uses this)")
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
        q = pending(context_bytes=args.bytes if args.bytes is not None else CONTEXT_BYTES)
        if args.json:
            print(json.dumps(q, ensure_ascii=False, indent=2))
        else:
            _print_queue(q)
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


def _print_queue(q: list[dict]) -> None:
    if not q:
        print("review queue empty — nothing to review.")
        return
    print(f"{len(q)} takeaway(s) to review:\n")
    for i, t in enumerate(q, 1):
        sup, rel = t["support"], t["relation"]["kind"]
        print(f"{i}/{len(q)} · {t['title']}  [{rel} · {sup['events']}ev/{sup['sessions']}sess]")
        print(f"  WHY: {t['why']}")
        for ev in t["evidence"]:
            print(f"    ✓ {ev['quote'][:100]!r}")
        print()


if __name__ == "__main__":
    main()
