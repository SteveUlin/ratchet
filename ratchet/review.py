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


def pending(root: Path | None = None, *, context_bytes: int = CONTEXT_BYTES) -> list[dict]:
    """The review queue: every MATURE takeaway with no terminal decision and no live snooze, each
    presented with its verified evidence. Derived from references — stores nothing (ADR-0007).

    The MATURITY GATE is `dream.current_takeaways`: it returns only takeaways whose NET distinct-session
    entrenchment (support sessions MINUS contradicting sessions, ADR-0012) crosses `dream.MATURITY_SESSIONS`
    (default 2). An incubating takeaway (a single net session so far) is deliberately kept OUT of the human
    gate — a one-off lesson costs review attention and risks promoting a false belief from a single moment
    — but it stays LIVE in the routing catalog so a later session can strengthen it across the bar; a once-
    mature takeaway that gets CONTESTED un-graduates back out of the queue (never deleted) and re-graduates
    if corroboration returns (see `incubating`)."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)   # lifecycle decisions only — producer markers excluded
    out: list[dict] = []
    for tk in dream.current_takeaways(root):       # the maturity gate (sessions >= MATURITY_SESSIONS)
        d = decisions.get(tk["id"])
        if d and _is_out_of_queue(d):
            continue
        out.append(_present(tk, root, context_bytes=context_bytes))
    return out


def incubating(root: Path | None = None, *, min_sessions: int = dream.MATURITY_SESSIONS) -> list[dict]:
    """The takeaways still BELOW the maturity bar — live in the routing catalog (dream can strengthen
    them), but not yet shown to the human gate. The counterpart to `pending`: `catalog` minus the mature
    set, minus anything already terminally decided. The bar is NET distinct-session entrenchment (support
    sessions minus contradicting sessions, ADR-0012), single-sourced in `dream.net_sessions`/
    `current_takeaways` — so "below the bar" here means below the SAME net gate `pending` graduates on,
    and a CONTESTED takeaway that un-graduated re-appears here (not silently lost). Surfaced so the
    reviewer can SEE what is accruing toward review (and decide to act early) without it crowding — or
    pre-biasing — the queue. A light projection (no evidence resolution); `needs` is the count of further
    distinct sessions to cross the net bar."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    mature = {t["id"] for t in dream.current_takeaways(root, min_sessions=min_sessions)}
    out: list[dict] = []
    for tk in dream.catalog(root):
        if tk["id"] in mature:
            continue
        d = decisions.get(tk["id"])
        if d and _is_out_of_queue(d):              # accepted/rejected/etc. directly — no longer accruing
            continue
        sup = tk.get("support") or {"events": 0, "sessions": 0}
        out.append({"takeaway_id": tk["id"], "title": tk.get("title", ""), "support": sup,
                    "needs": max(0, min_sessions - dream.net_sessions(tk))})   # net bar, single-sourced
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
