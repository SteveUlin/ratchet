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

- the **queue** is a derived query, not a stored list (ADR-0007): the UNION of dream-v3 CLAIMS
  (`resolve.claim_pool` over the maturity bar — claims preferred when an id exists in both feeds) and
  the legacy v2 takeaways (`dream.current_takeaways`), minus anything with a terminal decision
  (accepted/rejected) or a live snooze — references only. After `resolve --reset-v2` retires every
  v2 takeaway the legacy arm naturally empties; until then both queue side by side.
- **evidence** is re-resolved from the immutable blobs and re-validated (the trust chain reaches the
  reviewer: "verified real"), with an optional surrounding-context window for deep investigation.
- a **decision** (accept / reject / snooze / retire / refresh) is an append-only decision blob referencing
  its target; an **accept** also ingests the concept it mints and records Claude's assessment + the human's
  call as provenance. State is never a flipped field — it is the latest decision referencing the target.

The v3 claim surfaces (design §6, ADR-0028) ride the same fold: the LLM-merge AUDIT CARD renders every
corroboration's verified quote next to the match key the resolver persisted (the v2-failure detector —
the reviewer audits the acceptance layer with the same evidence the model saw); a "why pending" badge
keeps a matured-but-unsynthesized claim VISIBLE (review never blocks on synthesize's cadence); the
card carries synthesize's PROPOSED kind (behavioral vs reference, ADR-0029) which `--accept` confirms
on the decision (`--kind` overrides; `--set-kind` re-kinds an existing concept — the backfill verb)
and the DERIVED scope proposal (which repo the evidence lives in, ADR-0030) which `--accept` likewise
confirms (`--scope` overrides; `--set-scope` re-scopes an existing concept);
CONTESTED claims near the bar surface via `--contested`; merge SUGGESTIONS are a derived render-time
query over residue-band claim pairs (§2.2 — zero stored state, nothing can harden); the human "not the
same" verdict is the ONE compound `reject-merge` decision (`resolve.reject_merge` — review-only by
policy), and a confirmed suggestion is `merge_claims` (edge re-pointing, dream.merge's union adapted
to the edge model).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import blobstore, concepts, config, dream, resolve, sig, subject, temporal

CONCEPT_ID_PREFIX = "c-"
ASSESSMENT_MAX = 2000
CONTEXT_BYTES = 240        # default surrounding-context window for the queue presentation

# --- the v3 claim-surface knobs: named, explained, CLI-overridable (ADR-0025/0026/0027) -----------
SUGGEST_MAX = 3            # UNTUNED — merge suggestions shown PER CARD (§6.5): they ride existing
                           # cards instead of adding queue items, and a card buried under suggestions
                           # stops being reviewable; overflow pairs simply wait for their own card.
SUGGEST_TTL_DAYS = 30.0    # UNTUNED — a suggested pair folds out after this long WITHOUT new evidence
                           # on either side (§2.2): a derived query cannot harden into a de-facto merge
                           # (Wikidata's P460 failure), and a stale suggestion re-shown forever is how
                           # review queues die. Must sit under resolve.ACTIVE_DAYS or the active view
                           # expires the pair first.
CONTESTED_WINDOW = 1.0     # one fresh session's weight — a claim carrying a live contradicts edge
                           # within this of the bar is CONTESTED-near-bar (§6.6): a wrong llm
                           # CONTRADICTS verdict must not silently suppress an almost-mature claim.
SITTING_LIMIT = 10         # the CLI's default --pending/--incubating slice — a sitting's worth: review
                           # fatigue collapses past ~10-20 careful verdicts, and the queue is importance-
                           # ordered, so the top slice is always the most valuable. Never a hidden cap:
                           # the header states "top N of M" and --limit 0 loads everything (ADR-0027's
                           # explained-knob discipline). The LIBRARY default stays unlimited — status
                           # counts the full backlog through pending()/incubating().


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
               root: Path | None = None, coalesce_hours: float = temporal.COALESCE_HOURS) -> float:
    """A takeaway's REVIEW IMPORTANCE — the signal `pending` orders DESCENDING so the human sees the
    highest-leverage calls first (the active-learning "spend your attention where it matters most"; ADR-0022).
    RECENCY-WEIGHTED net entrenchment × confidence: `temporal.net_entrenchment` (support sessions minus
    contradicting ones, each weighted by its valid-time — the SAME signal the maturity gate graduates on,
    ADR-0012/0023) scaled by the takeaway's own durability `confidence`. So the queue's order respects
    recency for free: a belief sustained by RECENT corroboration outranks one entrenched only by old
    evidence. `valid_times` is built once in `pending` and threaded so the sort pays the transcript scan
    ONCE, not per takeaway; absent, `net_entrenchment` recomputes it from `root`. Deterministic, and the
    sort is STABLE, so equal-importance takeaways keep their derivation order."""
    conf = tk.get("confidence")
    conf = conf if isinstance(conf, (int, float)) else 0.5
    return temporal.net_entrenchment(tk, now, valid_times=valid_times, root=root,
                                  coalesce_hours=coalesce_hours) * conf


def _evidence_in_source(evidence: list, source_filter: str, root: Path, cache: dict) -> bool:
    """Does ANY cited span come from a SOURCE whose handle contains `source_filter` (case-insensitive)?
    The `--source` queue filter (ADR-0022) — a takeaway/concept spans the sessions it cites, so it matches
    the filter if any one of them does. Same lineage hop dream/glean use: `cleaned_hash` → raw
    `origin_ref.project` (`blobstore.project_of`, one cached meta read per span, no LLM)."""
    t = source_filter.lower()
    for ev in evidence or []:
        ch = ev.get("cleaned_hash") if isinstance(ev, dict) else None
        if not ch:
            continue
        proj = blobstore.project_of(ch, root, cache)
        if proj and t in proj.lower():
            return True
    return False


def bar_status(tk: dict, bar: float, *, valid_times: dict | None = None,
               now: str | None = None, root: Path | None = None,
               coalesce_hours: float = temporal.COALESCE_HOURS) -> dict:
    """Make the maturity gate TRANSPARENT (ADR-0027): a takeaway's recency-weighted entrenchment SCORE, the
    operator's BAR, whether it clears, and a one-line plain reason. Corroboration is EVIDENCE of durability,
    not a quota — so this EXPLAINS the standing rather than hiding a count behind a constant. The score is
    `temporal.net_entrenchment` (the very signal the gate graduates on); the bar is the reviewer's knob, shown
    next to the score so the call is legible. A takeaway can sit below the bar two ways — too few sessions
    yet, or enough sessions whose evidence has AGED below the recency-weighted line — and the note says which,
    because the remedy differs (wait for recurrence vs. needs RECENT corroboration)."""
    score = temporal.net_entrenchment(tk, now, valid_times=valid_times, root=root,
                                   coalesce_hours=coalesce_hours)
    raw = temporal.net_sessions(tk)
    mature = score >= bar
    if mature:
        why = f"corroborated across {raw} distinct session(s); weighted {score:.2f} ≥ bar {bar:.2f}"
    elif raw < temporal.MATURITY_SESSIONS:
        why = (f"seen in {raw} session(s) so far (weighted {score:.2f} < bar {bar:.2f}) — "
               f"a learning earns the queue by RECURRING, so it waits for corroboration in another session")
    else:
        why = (f"seen in {raw} session(s) but the evidence has AGED (weighted {score:.2f} < bar {bar:.2f}) — "
               f"needs RECENT corroboration to cross the line")
    return {"entrenchment": round(score, 2), "bar": round(bar, 2), "mature": mature, "rationale": why}


# --- the v3 claim surfaces: audit card, corroboration story, suggestions, contested (§6) ----------
#
# Everything here is a DERIVED render-time view over resolve's fold — claim_pool, live_edges,
# reject_merge_facts — computed when the reviewer looks and stored nowhere (ADR-0013). The claim id
# space is dream's `t-…` (mint_takeaway_id), so claims ride the same decision folds as takeaways; the
# one v3 wrinkle is `resolve.decision_binds`: after --reset-v2 a claim can be re-minted under a
# RETIRED takeaway's id, and a decision older than the claim's birth targeted that predecessor, never
# the live claim — so every claim-side decision read carries the binds guard.


def _claim_materials(root: Path) -> dict:
    """The shared per-render fold the claim surfaces read: reject-merge facts (the pair-block +
    retraction reads), live edges by claim, and the event-blob caches the audit card resolves
    subjects/quotes through. Built once per pending/context render, threaded everywhere."""
    rm = resolve.reject_merge_facts(root)
    return {"rm": rm, "edges": resolve.live_edges(root, rm),
            "ev_hashes": blobstore.latest_by_kind("event", root),
            "ev_cache": {}, "subj_cache": {}}


def _claim_view(claim_id: str, root: Path) -> dict | None:
    """One claim's folded view (live edges + event blobs), or None if the id is not a live claim —
    the claim-side sibling of `_load_takeaway`. The stored claim blob is seed identity ONLY (§2.1),
    so every consumer (accept, context) must read the FOLD, never the blob."""
    for c in resolve.claim_pool(root):
        if c["id"] == claim_id:
            return c
    return None


def _subjects_disjoint(key: dict, others: list[dict]) -> bool:
    """Does this evidence's subject share NOTHING (no repo, no file) with the claim's OTHER evidence?
    The audit card's ⚠ condition (§6.3): a disjoint-subject merge is exactly the shape of the v2
    failure (Zig+JAX+NEP-50 shared no scope). An EMPTY key on either side cannot DEMONSTRATE
    disjointness (the §3.1 empty-subject discipline, mirrored) — unknown reads as not-disjoint, so
    the ⚠ marks only evidence whose scopes are POSITIVELY apart."""
    if subject.is_empty(key):
        return False
    known = [o for o in others if not subject.is_empty(o)]
    if not known:
        return False
    for o in known:
        if key.get("repo") and key.get("repo") == o.get("repo"):
            return False
        if set(key.get("files") or ()) & set(o.get("files") or ()):
            return False
    return True


def _corroboration_story(corro: list[dict], subj_by_event: dict, valid_times: dict) -> list[str]:
    """The recurrence NARRATIVE (§6.2): one line per corroborating session, ordered by valid-time —
    where the lesson was seen (repo/file from its subject key) and, from the second sighting on, the
    gap since the first ("recurred after N days"). Recurrence-across-time is WHY the claim earned the
    queue, so the card says it in sulin's terms — the pattern in his work, not a bare count. An
    undated session reads as "undated" and skips the gap (never a fabricated number)."""
    rows = sorted(((str(valid_times.get(e.get("session_id")) or ""), e.get("session_id") or "?",
                    subj_by_event.get(e.get("event_id")) or {}) for e in corro),
                  key=lambda r: (r[0], r[1]))  # order by (valid-time, session); the subject dict is payload, never a tiebreaker
    out: list[str] = []
    first_vt: str | None = None
    first_key: dict = {}
    for vt, s, k in rows:
        if first_vt is None:
            where = ", ".join(([f"repo {k['repo']}"] if k.get("repo") else [])
                              + [f"file {f}" for f in (k.get("files") or ())[:2]]) or "no recorded subject"
            first_vt, first_key = vt, k
            out.append(f"seen in session {s} ({vt[:10] or 'undated'}, {where})")
            continue
        bits = []
        if k.get("repo"):
            bits.append("same repo" if k.get("repo") == first_key.get("repo") else f"repo {k['repo']}")
        shared = set(k.get("files") or ()) & set(first_key.get("files") or ())
        bits += ["same file"] if shared else [f"file {f}" for f in (k.get("files") or ())[:2]]
        where = ", ".join(bits) or "no recorded subject"
        line = f"again in session {s} ({vt[:10] or 'undated'}, {where})"
        if first_vt and vt:
            line += f" — recurred after {config.age_days(first_vt, now=vt):.0f} day(s)"
        out.append(line)
    return out


def claim_audit(claim: dict, root: Path, *, mats: dict, valid_times: dict,
                resolved_evidence: list[dict] | None = None) -> dict | None:
    """The LLM-MERGE AUDIT CARD (§6.3) — the v2-failure detector, rendered for any claim whose support
    rests on a `by:"llm"` corroborates edge (on the shipped two-band cascade, every merged claim).
    Each corroboration renders its VERIFIED quote (the ground truth) next to the match key the
    resolver persisted — stmt_sim, how many candidates the model saw, which model — so the reviewer
    audits the acceptance layer with exactly what the model saw; a per-edge ⚠ (`disjoint`) marks
    evidence whose subject shares nothing with the claim's other evidence. This replaces the old
    disjoint-AND-llm banner predicate, which missed the dominant same-subject case. Un-merged claims
    (seed-only support) return None — nothing to audit. Legacy blobs carry no match keys — moot: the
    v2 reset retires every v2 takeaway, so every claim here is v3-minted."""
    edges = mats["edges"].get(claim["id"], [])
    corro = sorted((e for e in edges if e.get("verb") == "corroborates"), key=lambda e: e["event_id"])
    if not any((e.get("match") or {}).get("by") == "llm" for e in corro):
        return None
    if resolved_evidence is None:
        resolved_evidence = resolve_evidence(claim, root)
    quotes = {ev["event_id"]: ev["quote"] for ev in resolved_evidence}
    subj_by_event: dict[str, dict] = {}
    for e in corro:
        ev = resolve.load_event(e["event_id"], mats["ev_hashes"], mats["ev_cache"], root)
        subj_by_event[e["event_id"]] = (resolve.event_subject(ev, root, mats["subj_cache"])
                                        if ev else {"repo": None, "files": []})
    rows: list[dict] = []
    for e in corro:
        m = e.get("match") or {}
        others = [subj_by_event[o["event_id"]] for o in corro if o["event_id"] != e["event_id"]]
        rows.append({
            "event_id": e["event_id"],
            "edge_id": resolve.edge_id(e["event_id"], "corroborates", claim["id"]),   # --reject-merge's handle
            "session_id": e.get("session_id"),
            "quote": quotes.get(e["event_id"]),          # verified via resolve_evidence, or None (blob gone)
            "by": m.get("by"),
            "match": ({"stmt_sim": m.get("stmt_sim"),
                       "candidates_shown": len(m.get("candidates_shown") or ()),
                       "model": m.get("model")} if m.get("by") == "llm" else None),
            "subject": subj_by_event[e["event_id"]],
            "disjoint": _subjects_disjoint(subj_by_event[e["event_id"]], others),
        })
    return {"corroborations": rows,
            "disjoint": any(r["disjoint"] for r in rows if r["by"] == "llm"),   # the card-level ⚠
            "story": _corroboration_story(corro, subj_by_event, valid_times)}


def _present_claim(c: dict, root: Path, *, context_bytes: int, mats: dict, valid_times: dict) -> dict:
    """The reviewer's view of one CLAIM: same card shape as `_present` (the skill iterates one queue)
    plus the v3 surfaces — kind, scope/subject, the why-pending badge (§6: a matured-but-unsynthesized
    claim APPEARS with its provisional title, never withheld — synthesize fills prose on demand), the
    why-stale flag (§7.3), the contested flag, and the audit card."""
    evidence = resolve_evidence(c, root, context_bytes=context_bytes)
    return {
        "takeaway_id": c["id"],                        # claims share dream's t-… id space; one queue key
        "kind": "claim",
        # the PROPOSED typology (ADR-0029) — `claim_kind`, because the card's `kind` is the queue-source
        # tag above. None until synthesize proposes one; --accept records it (--kind overrides).
        "claim_kind": c.get("kind"),
        "title": c.get("title", ""),
        "why": c.get("why"),
        "why_pending": not c.get("why"),               # the badge: provisional title, prose not yet minted
        "why_stale": bool(c.get("why_stale")),
        "relation": c.get("relation") or {"kind": "new", "concept_id": None},
        "support": c.get("support") or {"events": 0, "sessions": 0},
        "markers": c.get("markers") or {},
        "confidence": c.get("confidence"),
        "scope": c.get("scope"),                       # shown, never wired to a second bar (§3.4)
        # the DERIVED scope proposal (ADR-0030) — which repo's CLAUDE.md this lesson belongs to,
        # read off the live evidence (one repo → its label; 2+/none → global). --accept records it
        # (--scope overrides), mirroring claim_kind's propose→confirm shape.
        "scope_repo": c.get("scope_repo"),
        "subject": c.get("subject") or {"repos": [], "files": []},
        "contested": bool(c.get("contradicted_by")),   # a mature claim carrying a live contradiction
        "evidence": evidence,
        "audit": claim_audit(c, root, mats=mats, valid_times=valid_times, resolved_evidence=evidence),
    }


def merge_suggestions(root: Path | None = None, *, j_maybe: float | None = None,
                      j_high: float | None = None, h_min: float | None = None,
                      ttl_days: float = SUGGEST_TTL_DAYS, now: str | None = None,
                      pool: list[dict] | None = None, valid_times: dict | None = None,
                      rm: dict | None = None) -> list[dict]:
    """The DERIVED merge-suggestion query (§2.2/§6.5) — a pure function computed at render time,
    stored nowhere: pairs of live ACTIVE claims whose statement similarity lands in the residue band
    [J_MAYBE, J_HIGH) — the zone the $0 layer cannot separate and the residue call happened not to
    fuse — MINUS reject-merge'd pairs (never asked again), MINUS pairs where either side is trivial
    (min entropy < H_MIN — low-signal similarity is not evidence of one lesson, the cascade's own
    gate), and folded out after `ttl_days` without new evidence on either side. Each suggestion
    renders both claims' titles + ONE verified quote each — NEVER the stmt_sim number, which is noise
    at 0.2–0.35 (§6.5): the human judges the words, not a score. Confirm → `merge_claims`; dismiss →
    `reject_merge` on the pair."""
    root = root or config.data_root()
    j_maybe = sig.J_MAYBE if j_maybe is None else j_maybe
    j_high = sig.J_HIGH if j_high is None else j_high
    h_min = sig.H_MIN if h_min is None else h_min
    now = now or config.now()
    if valid_times is None:
        valid_times = temporal.session_valid_times(root)
    if rm is None:
        rm = resolve.reject_merge_facts(root)
    if pool is None:
        pool = resolve.claim_pool(root)
    active = [c for c in pool if resolve.is_active(c, now=now, valid_times=valid_times)]

    def freshest_age(c: dict) -> float:
        ss = [s for s in (c.get("sessions_seen") or []) if s]
        return min((config.age_days(valid_times.get(s), now=now) for s in ss), default=float("inf"))

    quotes: dict[str, str | None] = {}

    def quote_of(c: dict) -> str | None:
        if c["id"] not in quotes:
            ev = resolve_evidence(c, root)
            quotes[c["id"]] = ev[0]["quote"] if ev else None
        return quotes[c["id"]]

    out: list[dict] = []
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            a, b = active[i], active[j]
            if frozenset((a["id"], b["id"])) in rm["pairs"]:
                continue                               # the human said "not the same" — never re-ask
            if min(a["stmt_entropy"], b["stmt_entropy"]) < h_min:
                continue                               # triviality: degenerate overlap suggests nothing
            s = sig.jaccard(a["stmt_shingles"], b["stmt_shingles"])
            if not (j_maybe <= s < j_high):
                continue
            if min(freshest_age(a), freshest_age(b)) > ttl_days:
                continue                               # folded out: no new evidence within the TTL
            out.append({"pair": sorted((a["id"], b["id"])),
                        "claims": [{"claim_id": c["id"], "title": c.get("title", ""),
                                    "quote": quote_of(c)} for c in (a, b)]})
    out.sort(key=lambda r: r["pair"])
    return out


def _suggestions_by_claim(suggestions: list[dict]) -> dict[str, list[dict]]:
    """Suggestions folded onto the cards they ride (§6.5): claim id → its pairs, capped at SUGGEST_MAX
    per card so a well-connected claim's card stays reviewable (overflow pairs surface on the OTHER
    claim's card, or wait)."""
    by: dict[str, list[dict]] = {}
    for sg in suggestions:
        for cid in sg["pair"]:
            lst = by.setdefault(cid, [])
            if len(lst) < SUGGEST_MAX:
                lst.append(sg)
    return by


def contested(root: Path | None = None, *, maturity: float = temporal.MATURITY_WEIGHT,
              window: float = CONTESTED_WINDOW,
              coalesce_hours: float = temporal.COALESCE_HOURS) -> list[dict]:
    """CONTESTED-NEAR-BAR visibility (§6.6, v2's `contradicted_takeaways` carried forward): live claims
    carrying a live contradicts edge whose net entrenchment sits within `window` of the bar — above OR
    below. Above, the claim is in `pending` anyway (flagged `contested`); below, the contradiction is
    exactly what pushed it under, and a wrong llm CONTRADICTS verdict must not silently suppress an
    almost-mature claim — so the reviewer sees it here, with the contradicting quotes as ground truth
    (re-validated, like all evidence). Ordered by entrenchment descending — nearest the bar first."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    valid_times = temporal.session_valid_times(root)
    now = config.now()
    out: list[dict] = []
    for c in resolve.claim_pool(root):
        if not c.get("contradicted_by"):
            continue
        d = decisions.get(c["id"])
        if d and _is_out_of_queue(d) and resolve.decision_binds(d, c):
            continue                                   # already decided — no longer the gate's business
        if temporal.net_entrenchment(c, now, valid_times=valid_times,
                                  coalesce_hours=coalesce_hours) < maturity - window:
            continue
        st = bar_status(c, maturity, valid_times=valid_times, now=now, coalesce_hours=coalesce_hours)
        out.append({"claim_id": c["id"], "kind": "claim", "title": c.get("title", ""),
                    "support": c.get("support"), "contradictions": c.get("contradictions"),
                    "entrenchment": st["entrenchment"], "bar": st["bar"], "mature": st["mature"],
                    "rationale": st["rationale"],
                    "contradicting": [r["quote"] for r in resolve_evidence(
                        {"evidence": c.get("contradiction_evidence") or []}, root)]})
    out.sort(key=lambda r: -r["entrenchment"])
    return out


def reject_merge(spec: str, *, reason: str = "", reviewer: str | None = None,
                 root: Path | None = None) -> dict:
    """The human "not the same" verdict, parsed and handed to the ONE compound decision writer
    (`resolve.reject_merge`, §2.2 — review-only by policy, ADR-0008). Two spec forms:

      an EDGE id (`event|corroborates|claim`, the audit card's handle) — retraction + reopen +
        pair-block: the merge is torn out, the event re-enters the pool (epoch-keyed past its done
        marker) and seeds its own claim next tick, and the pair never re-forms;
      a claim PAIR (`A,B`, a dismissed merge suggestion) — pair-block only: no edge exists, nothing
        reopens, the suggestion query simply never asks again."""
    root = root or config.data_root()
    reviewer = reviewer or config.reviewer()
    spec = spec.strip()
    if resolve.EDGE_SEP in spec:
        parts = spec.split(resolve.EDGE_SEP)
        if len(parts) != 3 or parts[1] not in resolve.EDGE_VERBS or not (parts[0] and parts[2]):
            raise ValueError(f"--reject-merge edge form is event{resolve.EDGE_SEP}verb"
                             f"{resolve.EDGE_SEP}claim (verbs: {resolve.EDGE_VERBS}); got {spec!r}")
        return resolve.reject_merge(edge=spec, reason=reason, reviewer=reviewer, root=root)
    if "," in spec:
        a, _, b = (s.strip() for s in spec.partition(","))
        if not a or not b or a == b:
            raise ValueError(f"--reject-merge pair form is A,B (two distinct ids); got {spec!r}")
        return resolve.reject_merge(pair=[a, b], reason=reason, reviewer=reviewer, root=root)
    raise ValueError(f"--reject-merge takes an edge id (event{resolve.EDGE_SEP}verb"
                     f"{resolve.EDGE_SEP}claim) or a claim pair (A,B); got {spec!r}")


def merge_claims(loser_id: str, winner_id: str, root: Path | None = None, *, reason: str = "",
                 reviewer: str | None = None) -> dict:
    """CONFIRM a merge suggestion (§6.5) — dream.merge's evidence-union, restated in v3's edge model:
    under recompute-on-read there is no takeaway version to union, so the merge IS edge re-pointing.
    Every live edge on the loser (corroborates AND contradicts — a contradiction is never silently
    lost in a merge, ADR-0012) is re-written onto the winner with its match key preserved (plus
    `repointed_from`, so the audit trail says where it came from) and retracted from the loser; an
    edge the winner already holds for the same (event, verb) is only retracted from the loser (the
    winner's own audit key wins). Then ONE `merge` decision on the loser — `claim_pool` folds it out
    (invalidate-don't-delete; blob, edges, history stay). Commit order winner-gains → loser-retracts
    → decision LAST: a crash mid-way double-counts nothing permanent and a re-run completes the
    remainder. A reject-merge'd pair is REFUSED — the standing human verdict says "not the same", and
    decisions are append-only; overturning it is a deliberate new call, not a merge side-effect."""
    root = root or config.data_root()
    reviewer = reviewer or config.reviewer()
    if loser_id == winner_id:
        raise ValueError("merge_claims needs two distinct claims")
    rm = resolve.reject_merge_facts(root)
    if frozenset((loser_id, winner_id)) in rm["pairs"]:
        raise ValueError(f"pair ({loser_id}, {winner_id}) carries a reject-merge verdict — "
                         f"the standing call is 'not the same'")
    pool = {c["id"]: c for c in resolve.claim_pool(root)}
    for cid in (winner_id, loser_id):
        if cid not in pool:
            raise ValueError(f"no live claim {cid!r}")
    edges_by_claim = resolve.live_edges(root, rm)
    have = {(e["event_id"], e["verb"]) for e in edges_by_claim.get(winner_id, [])}
    run_id = config.run_id()
    moved = 0
    for e in sorted(edges_by_claim.get(loser_id, []), key=lambda e: (e["event_id"], e["verb"])):
        if (e["event_id"], e["verb"]) not in have:
            match = dict(e.get("match") or {})
            match["repointed_from"] = loser_id
            resolve.write_edge(e["event_id"], e["verb"], winner_id, session_id=e.get("session_id"),
                               match=match, root=root, run_id=run_id)     # winner gains FIRST
            moved += 1
        resolve.retract_edge(e["event_id"], e["verb"], loser_id, root=root, run_id=run_id)
    _record("merge", loser_id, root, into=winner_id, reviewer=reviewer,
            note=str(reason)[:ASSESSMENT_MAX], moved_edges=moved)         # the decision LAST
    return {"loser": loser_id, "winner": winner_id, "moved_edges": moved}


def pending(root: Path | None = None, *, context_bytes: int = CONTEXT_BYTES,
            brief: bool = False,
            limit: int | None = None, source_filter: str | None = None,
            maturity: float = temporal.MATURITY_WEIGHT,
            coalesce_hours: float = temporal.COALESCE_HOURS, with_total: bool = False):
    """The review queue: every takeaway AT/ABOVE the maturity bar with no terminal decision and no live
    snooze, ORDERED by IMPORTANCE descending and presented with its verified evidence + its bar standing.
    Derived from references — stores nothing (ADR-0007).

    The MATURITY GATE is `dream.current_takeaways(min_weight=maturity)`: it returns takeaways whose
    RECENCY-WEIGHTED net entrenchment (support sessions MINUS contradicting ones, each weighted by valid-time
    — ADR-0012/0023) crosses the bar. WHY a bar at all: a one-off lesson costs review attention and risks
    promoting a belief from a single moment, so a learning earns the queue by RECURRING across distinct,
    recent sessions. But the bar is the REVIEWER'S KNOB, not a hidden constant (ADR-0027): `maturity` lowers
    it to review more or raises it for only the most-corroborated, every item carries its score-vs-bar
    `rationale`, and what sits below is never hidden — `incubating` lists it with the same reasoning. A
    once-mature takeaway that is CONTESTED un-graduates (never deleted) and re-graduates if corroboration
    returns.

    ORDERING + the operator knobs (ADR-0022): `importance` (net entrenchment × confidence) sorts DESCENDING —
    highest-leverage first. `--source` filters to a SOURCE handle (substring over cited evidence); `--limit N` returns
    the top-N (`0`/`None` = everything — the escape hatch; the CLI defaults to `SITTING_LIMIT`). Filter + sort
    run on the RAW takeaways FIRST, so the expensive evidence resolution (`_present`) is paid only for the
    survivors the human will see. `with_total=True` returns `(cards, total)` — the backlog depth BEFORE the
    slice — so a bounded render can still state honestly what it was cut from.

    `brief=True` returns the INDEX instead: same ordering, same filters, but one light row per item
    (title, standing, badges — NO evidence resolution, audit cards, or merge suggestions). The sitting
    protocol is one-card-one-verdict, so a reviewer-side orchestrator needs the queue's SHAPE up front
    and the full render for only the claim under the lens — `card()` is that cursor. Without the split,
    a sitting's context cost is O(queue) even though its attention is O(1) (the 15-claim queue rendered
    ~56k tokens); with it, backlog size stops bounding the sitting.

    THE FEED IS A UNION (dream v3, §6/ADR-0028): v3 CLAIMS over the same bar queue beside the legacy v2
    takeaways — claims PREFERRED when an id exists in both feeds (the id space is shared:
    `mint_takeaway_id`), one importance order over both. After `--reset-v2` retires every v2 takeaway
    the legacy arm empties by itself. A claim card additionally carries the audit card, scope, the
    why-pending badge, the contested flag, and its merge suggestions (`_present_claim`)."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)   # lifecycle decisions only — producer markers excluded
    valid_times = temporal.session_valid_times(root) # session → valid-time, scanned ONCE for the gate + the sort
    now = config.now()
    cache: dict = {}
    rows: list[tuple[dict, str]] = []              # (view, kind) — one queue, two sources
    pool = resolve.claim_pool(root)
    claim_ids = {c["id"] for c in pool}
    for c in pool:
        if temporal.net_entrenchment(c, now, valid_times=valid_times,       # the same bar, same knob —
                                  coalesce_hours=coalesce_hours) < maturity:   # sittings incl. (ADR-0028)
            continue
        d = decisions.get(c["id"])
        if d and _is_out_of_queue(d) and resolve.decision_binds(d, c):
            continue                               # pre-birth decisions targeted the retired v2 predecessor
        if source_filter is not None and not _evidence_in_source(c.get("evidence"), source_filter, root, cache):
            continue
        rows.append((c, "claim"))
    for tk in dream.current_takeaways(root, min_weight=maturity, now=now,      # bar is the knob
                                      valid_times=valid_times):
        if tk["id"] in claim_ids:
            continue                               # the claim view is preferred — one id, one card
        d = decisions.get(tk["id"])
        if d and _is_out_of_queue(d):
            continue
        if source_filter is not None and not _evidence_in_source(tk.get("evidence"), source_filter, root, cache):
            continue                               # FOCUS: drop takeaways with no evidence from a matching source
        rows.append((tk, "takeaway"))
    rows.sort(key=lambda r: importance(r[0], now, valid_times=valid_times, coalesce_hours=coalesce_hours),
              reverse=True)                            # IMPORTANCE desc; stable; the ONE pinned now
    total = len(rows)                              # the backlog depth, counted BEFORE the sitting slice
    if limit is not None and limit > 0:
        rows = rows[:limit]                        # the top-N for a one-sitting review (after the ordering);
                                                   # 0/None = everything, so the escape hatch can't return []
    if brief:                                      # the INDEX: O(a line) per item — no evidence resolution,
        out = []                                   # no audit cards, no suggestions. The sitting protocol is
        for view, kind in rows:                    # one-card-one-verdict; the render should scale the same
            row = {"takeaway_id": view["id"], "kind": kind,          # way (sulin, 2026-07-06) — `card()` is
                   "title": view.get("title", ""),                   # the cursor that pays the full render
                   "why_pending": view.get("why") is None,           # for ONE claim at a time.
                   "claim_kind": view.get("kind") if kind == "claim" else None,
                   "scope_repo": view.get("scope_repo") if kind == "claim" else None,
                   "support": view.get("support") or {"events": 0, "sessions": 0},
                   "evidence_count": len(view.get("evidence") or []),
                   "contradictions": len(view.get("contradiction_evidence") or []),
                   **bar_status(view, maturity, valid_times=valid_times, now=now,
                                coalesce_hours=coalesce_hours)}
            out.append(row)
        return (out, total) if with_total else out
    mats = _claim_materials(root) if any(k == "claim" for _, k in rows) else None
    sugg = (_suggestions_by_claim(merge_suggestions(root, now=now, pool=pool,
                                                    valid_times=valid_times, rm=mats["rm"]))
            if mats is not None else {})
    out: list[dict] = []
    for view, kind in rows:
        if kind == "claim":
            card = {**_present_claim(view, root, context_bytes=context_bytes, mats=mats,
                                     valid_times=valid_times),
                    **bar_status(view, maturity, valid_times=valid_times, now=now,
                                 coalesce_hours=coalesce_hours)}
            card["merge_suggestions"] = sugg.get(view["id"], [])
        else:
            card = {**_present(view, root, context_bytes=context_bytes),
                    **bar_status(view, maturity, valid_times=valid_times, now=now,
                                 coalesce_hours=coalesce_hours)}
        out.append(card)
    return (out, total) if with_total else out


def card(takeaway_id: str, root: Path | None = None, *, context_bytes: int = CONTEXT_BYTES,
         maturity: float = temporal.MATURITY_WEIGHT,
         coalesce_hours: float = temporal.COALESCE_HOURS) -> dict | None:
    """ONE fully-rendered review card — the cursor to `pending(brief=True)`'s index. Pays the full
    render (verified evidence, the audit card, merge suggestions, bar standing) for exactly the claim
    under the reviewer's lens, so a sitting holds one card's worth of context at a time no matter how
    deep the backlog runs. Claims preferred over a same-id legacy takeaway (the queue's own precedence);
    None for an unknown id. Renders regardless of bar standing — the card SHOWS the standing, and a
    reviewer chasing an incubating or contested id gets the same full view the queue would give it."""
    root = root or config.data_root()
    valid_times = temporal.session_valid_times(root)
    now = config.now()
    view = _claim_view(takeaway_id, root)
    if view is not None:
        mats = _claim_materials(root)
        sugg = _suggestions_by_claim(merge_suggestions(root, now=now, valid_times=valid_times,
                                                       rm=mats["rm"]))
        full = {**_present_claim(view, root, context_bytes=context_bytes, mats=mats,
                                 valid_times=valid_times),
                **bar_status(view, maturity, valid_times=valid_times, now=now,
                             coalesce_hours=coalesce_hours)}
        full["merge_suggestions"] = sugg.get(view["id"], [])
        return full
    tk = _load_takeaway(takeaway_id, root)
    if tk is None:
        return None
    return {**_present(tk, root, context_bytes=context_bytes),
            **bar_status(tk, maturity, valid_times=valid_times, now=now,
                         coalesce_hours=coalesce_hours)}


def incubating(root: Path | None = None, *, maturity: float = temporal.MATURITY_WEIGHT,
               coalesce_hours: float = temporal.COALESCE_HOURS,
               source_filter: str | None = None) -> list[dict]:
    """The takeaways still BELOW the maturity bar — live in the routing catalog (dream can strengthen
    them), but not yet at the human gate. The counterpart to `pending`: `catalog` minus the mature set
    (at the SAME `maturity` bar, so lowering the bar moves takeaways from here into the queue), minus
    anything already terminally decided. A CONTESTED takeaway that un-graduated re-appears here (not
    silently lost). Surfaced — with each takeaway's score-vs-bar `rationale` (ADR-0027) — so the reviewer
    SEES what is accruing and WHY, and can act early, without it crowding or pre-biasing the queue. A light
    projection (no evidence resolution); `needs` is the human-legible RAW distinct-session shortfall
    (`MATURITY_SESSIONS - net_sessions`), complemented by `needs_recent` for the AGED case (enough sessions,
    but their evidence decayed below the weighted bar — the honest signal is "needs RECENT corroboration").
    The same UNION as `pending` (v3 claims beside legacy takeaways, claims preferred), tagged by `kind`.
    `source_filter` scopes it the same way `--source` scopes the queue (`_evidence_in_source` — a cached
    lineage hop per span, no evidence resolution), so a focused sitting sees a matching incubation count.
    ORDERED by entrenchment DESCENDING — nearest the bar first, so the CLI's bounded slice (SITTING_LIMIT)
    shows the takeaways closest to graduating, the ones a sitting can actually act on."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    valid_times = temporal.session_valid_times(root)
    now = config.now()
    cache: dict = {}

    def row(tk: dict, kind: str) -> dict:
        raw_needs = max(0, temporal.MATURITY_SESSIONS - temporal.net_sessions(tk))
        st = bar_status(tk, maturity, valid_times=valid_times, now=now,   # the ONE pinned now —
                        coalesce_hours=coalesce_hours)                    # net_entrenchment's contract
        return {"takeaway_id": tk["id"], "kind": kind, "title": tk.get("title", ""),
                "support": tk.get("support") or {"events": 0, "sessions": 0},
                "entrenchment": st["entrenchment"], "bar": st["bar"], "rationale": st["rationale"],
                "needs": raw_needs,                    # distinct-session shortfall on the RAW count, but …
                "needs_recent": raw_needs == 0}        # … if 0 yet incubating, it's held below the bar by
                # AGED evidence (recency-weighted gate, ADR-0023) — it needs RECENT corroboration, not more
                # old sessions; the flag stops the queue reading a misleading "needs 0".

    out: list[dict] = []
    pool = resolve.claim_pool(root)
    claim_ids = {c["id"] for c in pool}
    for c in pool:
        if temporal.net_entrenchment(c, now, valid_times=valid_times,
                                  coalesce_hours=coalesce_hours) >= maturity:
            continue
        d = decisions.get(c["id"])
        if d and _is_out_of_queue(d) and resolve.decision_binds(d, c):
            continue
        if source_filter is not None and not _evidence_in_source(c.get("evidence"), source_filter, root, cache):
            continue
        out.append(row(c, "claim"))
    mature = {t["id"] for t in dream.current_takeaways(root, min_weight=maturity, now=now,
                                                       valid_times=valid_times)}
    for tk in dream.catalog(root):
        if tk["id"] in mature or tk["id"] in claim_ids:
            continue
        d = decisions.get(tk["id"])
        if d and _is_out_of_queue(d):              # accepted/rejected/etc. directly — no longer accruing
            continue
        if source_filter is not None and not _evidence_in_source(tk.get("evidence"), source_filter, root, cache):
            continue
        out.append(row(tk, "takeaway"))
    out.sort(key=lambda r: -r["entrenchment"])     # nearest the bar first; stable → ties keep derivation order
    return out


def _present(takeaway: dict, root: Path, *, context_bytes: int) -> dict:
    """The reviewer's view of one takeaway: the synthesis (untrusted) + its verified evidence."""
    return {
        "takeaway_id": takeaway["id"],
        "kind": "takeaway",
        "title": takeaway.get("title", ""),
        "why": takeaway.get("why", ""),
        "relation": takeaway.get("relation") or {"kind": "new", "concept_id": None},
        "support": takeaway.get("support") or {"events": 0, "sessions": 0},
        "markers": takeaway.get("markers") or {},
        "confidence": takeaway.get("confidence"),
        "evidence": resolve_evidence(takeaway, root, context_bytes=context_bytes),
    }


def context_for(takeaway_id: str, root: Path | None = None, *, context_bytes: int = 1200) -> dict | None:
    """One takeaway OR claim with a WIDE evidence window — the deep path: when Claude escalates to
    investigate (thin support, a `why` that overreaches its quotes, a contradiction), it reads the
    surrounding transcript here rather than trusting the one-line synthesis. Claims preferred (the
    queue's own precedence), and the claim's deep view carries its audit card too."""
    root = root or config.data_root()
    c = _claim_view(takeaway_id, root)
    if c is not None:
        return _present_claim(c, root, context_bytes=context_bytes, mats=_claim_materials(root),
                              valid_times=temporal.session_valid_times(root))
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
           assessment: str = "", reviewer: str | None = None, note: str = "",
           kind: str | None = None, scope: str | None = None,
           allow_no_evidence: bool = False) -> str:
    """Promote a takeaway to a concept (the loop closes here) and record the accept. The concept stores
    ONLY the evidence that re-validates *now* — exactly the verified spans the reviewer saw, never the
    raw (possibly malformed/stale) takeaway evidence — so the trust chain reaches the concept, the
    system's most trusted artifact. A takeaway with no resolvable evidence is REFUSED (a belief with no
    anchor would feed dream/generate) unless `allow_no_evidence` overrides. Identity comes from the
    takeaway's `relation`: `strengthens`/`refines` an EXISTING concept → a new version of it; otherwise
    (incl. a stale/unknown concept_id) mint fresh. `edited` ({title?, why?}) corrects the synthesis
    before it becomes a concept, captured before/after. `kind` confirms/overrides the claim's proposed
    typology (ADR-0029; default = the proposal, else behavioral) and `scope` confirms/overrides its
    DERIVED scope (ADR-0030; default = the evidence-derived proposal, else global) — both recorded ON
    THE DECISION, where the valid-concept view reads them (`concepts.concept_kinds`/`concept_scopes`),
    never on the blob. Returns the concept id.

    A v3 CLAIM accepts through the same door (§5 — review appends a decision FACET, nothing forks):
    the id resolves to the claim's LIVE-EDGE FOLD first (`_claim_view` — the stored blob is seed
    identity only, so evidence exists ONLY in the fold), and the same `_verified_pointers` filter
    re-validates every span. A why-pending claim (why=null) accepts with statement '' unless the
    reviewer edits one in — never withheld waiting on synthesize (§6)."""
    root = root or config.data_root()
    reviewer = reviewer or config.reviewer()
    tk = _claim_view(takeaway_id, root) or _load_takeaway(takeaway_id, root)
    if tk is None:
        raise ValueError(f"no takeaway {takeaway_id!r}")
    evidence = _verified_pointers(tk, root)         # only spans that re-validate (the reviewer's filter)
    if not evidence and not allow_no_evidence:
        raise ValueError(f"takeaway {takeaway_id!r} has no resolvable evidence — refusing to mint a "
                         f"concept with no verifiable backing (override with allow_no_evidence=True)")
    edited = edited or {}
    title = edited.get("title", tk.get("title", ""))
    statement = edited.get("why", tk.get("why") or "")   # a claim's why is null until synthesize (§7.3)
    # the KIND the accept confirms (ADR-0029): the reviewer's explicit --kind wins; else the claim's
    # proposed kind (synthesize's, coerced); else behavioral (v2 takeaways and unsynthesized claims
    # carry no proposal). An explicit override outside the vocabulary is REFUSED, not coerced —
    # coercion absorbs a model's noise, never a reviewer's typo (their decision is authoritative).
    if kind is not None and kind not in concepts.CONCEPT_KINDS:
        raise ValueError(f"--kind must be one of {concepts.CONCEPT_KINDS}; got {kind!r}")
    kind = kind if kind is not None else concepts.clean_kind(tk.get("kind"))
    # the SCOPE the accept confirms (ADR-0030): the reviewer's explicit --scope wins; else the
    # claim's DERIVED proposal (scope_repo — the evidence's repo, or global); else global (v2
    # takeaways carry no derivation). The vocabulary is OPEN (any repo label), so only an explicit
    # BLANK is refused — an empty scope is a reviewer's slip, never a namable place for a rule.
    if scope is not None and not scope.strip():
        raise ValueError("--scope must be a repo label or 'global' — got an empty string")
    scope = scope.strip() if scope is not None else concepts.clean_scope(tk.get("scope_repo"))
    rel = tk.get("relation") or {}
    known = concepts.valid_concept_ids(root)
    concept_id = (rel["concept_id"] if rel.get("kind") in ("strengthens", "refines")
                  and isinstance(rel.get("concept_id"), str) and rel["concept_id"] in known
                  else _mint_concept_id(tk))        # reuse only an EXISTING concept; else mint fresh
    concept = {"id": concept_id, "title": title, "statement": statement,
               "evidence": evidence, "source_takeaway": takeaway_id}
    ch, _ = blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=concept_id,
                             origin_ref={"stage": "review", "reviewer": reviewer, "takeaway": takeaway_id}, root=root)
    fields = {"concept": concept_id, "concept_hash": ch, "kind": kind, "scope": scope,
              "reviewer": reviewer,
              "assessment": str(assessment)[:ASSESSMENT_MAX], "note": str(note)[:ASSESSMENT_MAX]}
    if edited:
        fields["edited"] = {"before": {"title": tk.get("title", ""), "why": tk.get("why", "")},
                            "after": {"title": title, "why": statement}}
    _record("edit" if edited else "accept", takeaway_id, root, **fields)
    return concept_id


def reject(takeaway_id: str, root: Path | None = None, *, reason: str = "", assessment: str = "",
           reviewer: str | None = None) -> None:
    """Reject a takeaway. Persisted as a label (no fine-tuning): a future PROMPT_VERSION bump can fold
    rejections into negative few-shot + suppress semantic near-dupes, fixing the 're-suggests dismissed
    things' trust-killer (MemPrompt)."""
    reviewer = reviewer or config.reviewer()
    _record("reject", takeaway_id, root or config.data_root(),
            reason=str(reason)[:ASSESSMENT_MAX], assessment=str(assessment)[:ASSESSMENT_MAX], reviewer=reviewer)


def snooze(takeaway_id: str, root: Path | None = None, *, until: str, reason: str = "",
           reviewer: str | None = None) -> None:
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
    reviewer = reviewer or config.reviewer()
    _record("snooze", takeaway_id, root or config.data_root(),
            until=until, reason=str(reason)[:ASSESSMENT_MAX], reviewer=reviewer)


def retire(concept_id: str, root: Path | None = None, *, reason: str = "", reviewer: str | None = None) -> None:
    """Take a concept OUT of the valid set (a contradiction the reviewer affirms). Not a deletion — the
    concept blob and its history stay; its latest lifecycle decision being `retire` drops it from
    `load_concepts`/`valid_concepts`. (Re-establishing a retired concept is a future manual action:
    dream stops surfacing it, so a refinement won't normally re-reference it — ADR-0008.)"""
    reviewer = reviewer or config.reviewer()
    _record("retire", concept_id, root or config.data_root(),
            reason=str(reason)[:ASSESSMENT_MAX], reviewer=reviewer)


def set_kind(concept_id: str, kind: str, root: Path | None = None, *, reason: str = "",
             reviewer: str | None = None) -> None:
    """Re-KIND an EXISTING concept (ADR-0029) — the reviewer-owned append-only decision `valid_concepts`
    reads FIRST (latest set_kind > the accept's kind > behavioral). This is the backfill path for
    concepts accepted before the typology existed, and the correction path when a kind call ages badly
    — like `retire`, a decision only the human writes; nothing upstream can flip it back. The vocabulary
    is closed and the target must be VALID: an out-of-vocabulary kind is refused (a reviewer's typo is
    an error, never silently coerced), and a retired/superseded target is refused because a fresh
    decision would become its LATEST lifecycle decision and pull it back into the valid set — re-kinding
    must never double as an accidental un-retire."""
    root = root or config.data_root()
    reviewer = reviewer or config.reviewer()
    if kind not in concepts.CONCEPT_KINDS:
        raise ValueError(f"kind must be one of {concepts.CONCEPT_KINDS}; got {kind!r}")
    if concept_id not in concepts.valid_concept_ids(root):
        raise ValueError(f"no valid concept {concept_id!r} — set-kind targets the valid set only "
                         f"(a decision on a retired concept would resurrect it)")
    _record(concepts.VERB_SET_KIND, concept_id, root, kind=kind,
            reason=str(reason)[:ASSESSMENT_MAX], reviewer=reviewer)


def set_scope(concept_id: str, scope: str, root: Path | None = None, *, reason: str = "",
              reviewer: str | None = None) -> None:
    """Re-SCOPE an EXISTING concept (ADR-0030) — `set_kind`'s twin on the scope axis, the reviewer-
    owned append-only decision `valid_concepts` reads FIRST (latest set_scope > the accept's scope >
    global). The backfill path for concepts accepted before the axis existed, and the correction path
    when the derivation guessed wrong (e.g. a genuinely repo-local lesson whose evidence happens to
    span repos). The vocabulary is OPEN — any repo label — so only a BLANK scope is refused (nothing
    can live at an unnamable place); the target must be VALID for the same reason as set_kind: a
    fresh decision on a retired concept would become its latest lifecycle decision and pull it back
    into the valid set — re-scoping must never double as an accidental un-retire."""
    root = root or config.data_root()
    reviewer = reviewer or config.reviewer()
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a repo label or 'global' — got an empty string")
    if concept_id not in concepts.valid_concept_ids(root):
        raise ValueError(f"no valid concept {concept_id!r} — set-scope targets the valid set only "
                         f"(a decision on a retired concept would resurrect it)")
    _record(concepts.VERB_SET_SCOPE, concept_id, root, scope=scope.strip(),
            reason=str(reason)[:ASSESSMENT_MAX], reviewer=reviewer)


def refresh(concept_id: str, root: Path | None = None, *, edited: dict | None = None,
            reviewer: str | None = None, note: str = "") -> dict:
    """Re-SNAPSHOT a concept's prose from its source claim's LIVE fold — the reviewer's answer to the
    why-pending statement gap. Accept never withholds on synthesize's cadence (§6), so a why-pending
    claim (why=null) mints its concept with statement '' — and when synthesize later fills the claim's
    `why`, nothing carries it forward. Nothing MAY carry it forward automatically: the gate is the
    trust source (ADR-0008), the concept is the system's most trusted artifact, and an auto-refresh
    would land unreviewed LLM prose behind the human gate. So refresh is a HUMAN verb — the reviewer
    reads the synthesized why (or corrects it via `edited` {title?, why?}, accept's shape) and
    commands the re-read; the decision records before/after, so the audit trail says what the prose
    was and what it became.

    The refusals derive from the same pressures. A NO-OP (title and statement both already current) is
    refused, never silently absorbed: a decision blob is a fact, and a refresh that changed nothing
    would fake a review action — idempotence by refusal, accept's no-evidence discipline. A concept
    whose latest lifecycle decision is retire/supersede/split is refused because a fresh decision would
    become its LATEST and pull it back into the valid set (set_kind's guard — a refresh must never
    double as an accidental un-retire). A gone source is refused: with no live claim to re-read, a
    "refresh" would be invention. And only the PROSE moves — evidence + source_takeaway are carried
    byte-identical (the spans were verified at accept; swapping evidence is accept/garden's business),
    and kind/scope sit untouched because they are the DECISION's facts, folded from set_kind/set_scope/
    accept decisions (ADR-0029/0030), never blob fields — the same design that lets a garden re-version
    keep them lets this one. Returns {concept, concept_hash, before, after}."""
    from . import garden                           # function-local: garden imports review at module load
    root = root or config.data_root()
    reviewer = reviewer or config.reviewer()
    concept = garden.concept_blob(concept_id, root)
    if concept is None:
        raise ValueError(f"no concept {concept_id!r}")
    d = blobstore.latest_decisions(root).get(concept_id)   # the LIFECYCLE fold load_concepts reads
    if d and d.get("verb") in concepts.CONCEPT_INVALID_VERBS:
        raise ValueError(f"concept {concept_id!r} is out of the valid set (latest decision: "
                         f"{d['verb']}) — a refresh decision would become its latest and resurrect "
                         f"it; re-establishing a dead concept is a deliberate call, not a side-effect")
    src = concept.get("source_takeaway")
    if not isinstance(src, str) or not src:
        raise ValueError(f"concept {concept_id!r} records no source_takeaway — nothing to re-read "
                         f"(a garden-minted concept has no single source claim)")
    tk = _claim_view(src, root) or _load_takeaway(src, root)   # the LIVE fold first — accept's read
    if tk is None:
        raise ValueError(f"concept {concept_id!r}'s source {src!r} is gone (folded out or reclaimed) "
                         f"— nothing live to re-read")
    edited = edited or {}
    title = edited.get("title", tk.get("title", ""))
    statement = edited.get("why", tk.get("why") or "")   # accept's read: a null why snapshots ''
    before = {"title": concept.get("title", ""), "statement": concept.get("statement", "")}
    after = {"title": title, "statement": statement}
    if after == before:
        raise ValueError(f"refresh of {concept_id!r} would change nothing — title and statement "
                         f"already match the claim's current prose (run synthesize to fill the "
                         f"claim's why, or pass --edit-title/--edit-why)")
    blob = {**concept, "title": title, "statement": statement}   # same id/evidence/source_takeaway
    ch, _ = blobstore.ingest(blobstore.canonical_json(blob), source_kind="concept",
                             source_id=concept_id,
                             origin_ref={"stage": "review", "verb": "refresh",
                                         "reviewer": reviewer, "takeaway": src}, root=root)
    _record("refresh", concept_id, root, concept_hash=ch, before=before, after=after,
            reviewer=reviewer, note=str(note)[:ASSESSMENT_MAX])
    return {"concept": concept_id, "concept_hash": ch, "before": before, "after": after}


def valid_concepts(root: Path | None = None) -> list[dict]:
    """The current valid concept set — what dream judges belief-change against and `generate` projects
    to skills/CLAUDE.md. A thin re-export of `concepts.load_concepts`, for inspection. Each concept
    carries its derived `kind` (ADR-0029:
    latest set_kind decision > the accept's kind > behavioral) and `scope` (ADR-0030: latest
    set_scope decision > the accept's scope > global)."""
    return concepts.load_concepts(root)


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
    valid = concepts.valid_concept_ids(root)
    cited: list[dict] = []
    for cid in proposal.get("concept_ids") or []:
        blob = garden.concept_blob(cid, root) or {}    # the latest concept version, valid OR not
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


def _proposal_in_source(proposal: dict, source_filter: str, root: Path, cache: dict) -> bool:
    """Does a structural-op proposal touch a SOURCE matching `source_filter`? It cites concepts, not events,
    so the hop is one longer: each cited concept → its evidence spans → `cleaned_hash` → raw
    `origin_ref.project` (`_evidence_in_source`). True if ANY cited concept has evidence from a source
    handle matching the substring."""
    from . import garden
    for cid in proposal.get("concept_ids") or []:
        blob = garden.concept_blob(cid, root) or {}
        if _evidence_in_source(blob.get("evidence"), source_filter, root, cache):
            return True
    return False


def pending_proposals(root: Path | None = None, *, context_bytes: int = CONTEXT_BYTES,
                      limit: int | None = None, source_filter: str | None = None) -> list[dict]:
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
    `--source` filters to a SOURCE handle (substring over the cited concepts' evidence); `--limit N` takes the
    top-N. Filter + sort run on the raw proposals FIRST, so the per-concept evidence resolution is paid only
    for the survivors."""
    from . import garden
    root = root or config.data_root()
    props = garden.open_proposals(root)
    if source_filter is not None:
        cache: dict = {}
        props = [p for p in props if _proposal_in_source(p, source_filter, root, cache)]
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
                    reviewer: str | None = None, note: str = "", split_parts: list[dict] | None = None,
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
    proposal = garden.proposal_blob(proposal_id, root)
    if proposal is None:
        raise ValueError(f"no garden proposal {proposal_id!r}")
    reviewer = reviewer or config.reviewer()
    run_id = config.run_id()
    result = _apply_proposal(proposal, root=root, run_id=run_id, reviewer=reviewer,
                             split_parts=split_parts, allow_no_evidence=allow_no_evidence)
    _record("accept_proposal", proposal_id, root, op=proposal["op"], reviewer=reviewer,
            assessment=str(assessment)[:ASSESSMENT_MAX], note=str(note)[:ASSESSMENT_MAX],
            result=result if isinstance(result, (str, list)) else None)
    return {"proposal_id": proposal_id, "op": proposal["op"], "status": "accepted", "result": result}


def reject_proposal(proposal_id: str, root: Path | None = None, *, reason: str = "",
                    assessment: str = "", reviewer: str | None = None) -> dict:
    """Reject a queued structural-op proposal — RECORD the reject; the op is NOT applied, the concept graph is
    untouched. No status flip: the reject DECISION is the proposal's lifecycle (`garden.open_proposals` drops
    it via `RESOLVE_VERBS`), and `garden.queue_proposal` reads that same decision so a re-gardened cluster never
    re-opens a dismissed op — the L2 loop closing, its full why at `queue_proposal`. Returns {proposal_id, op,
    status}."""
    from . import garden
    root = root or config.data_root()
    proposal = garden.proposal_blob(proposal_id, root)
    if proposal is None:
        raise ValueError(f"no garden proposal {proposal_id!r}")
    reviewer = reviewer or config.reviewer()
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
    g.add_argument("--contested", action="store_true",
                   help="claims carrying a live contradicts edge within one session of the bar — a wrong "
                        "llm CONTRADICTS verdict must not silently suppress an almost-mature claim (§6.6)")
    g.add_argument("--reject-merge", metavar="EDGE|A,B",
                   help="the compound 'not the same' verdict (§2.2): an edge id (event|corroborates|claim, "
                        "the audit card's handle) retracts + reopens + pair-blocks; a claim pair A,B "
                        "dismisses a merge suggestion (pair-block only, never asked again)")
    g.add_argument("--merge-claims", nargs=2, metavar=("LOSER", "WINNER"),
                   help="confirm a merge suggestion: re-point the loser's live edges onto the winner "
                        "(match keys preserved) and fold the loser out via a merge decision")
    g.add_argument("--card", metavar="TAKEAWAY",
                   help="ONE fully-rendered card (evidence, audit, suggestions, standing) — the cursor "
                        "to --pending --brief's index: a sitting fetches the queue's shape once, then "
                        "one card per verdict, so context stays O(1) in backlog depth")
    g.add_argument("--context", metavar="TAKEAWAY", help="one takeaway with a WIDE evidence window (deep path)")
    g.add_argument("--accept", metavar="TAKEAWAY", help="promote a takeaway to a concept")
    g.add_argument("--reject", metavar="TAKEAWAY", help="reject a takeaway")
    g.add_argument("--snooze", metavar="TAKEAWAY", help="defer a takeaway (needs --until)")
    g.add_argument("--retire", metavar="CONCEPT", help="take a concept out of the valid set")
    g.add_argument("--refresh", metavar="CONCEPT",
                   help="re-snapshot a concept's title/statement from its source claim's LIVE fold — "
                        "accept never withholds on synthesize (§6/why-pending), so a why-pending "
                        "accept mints an EMPTY statement; once synthesize fills the claim's why, "
                        "this re-reads it on the reviewer's command (never automatically — the gate "
                        "is the trust source). Rides --edit-title/--edit-why/--note; a no-op refuses")
    g.add_argument("--set-kind", nargs=2, metavar=("CONCEPT", "KIND"),
                   help="re-kind an EXISTING concept (behavioral|reference) — an append-only reviewer "
                        "decision that outranks the kind recorded at accept; the backfill path for "
                        "concepts accepted before the typology (ADR-0029)")
    g.add_argument("--set-scope", nargs=2, metavar=("CONCEPT", "SCOPE"),
                   help="re-scope an EXISTING concept (a repo label, or 'global') — an append-only "
                        "reviewer decision that outranks the scope recorded at accept; the backfill "
                        "path for concepts accepted before the scope axis (ADR-0030)")
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
                    help=f"how many items a sitting loads (by importance for takeaways, stakes for "
                         f"proposals). --pending/--incubating default to {SITTING_LIMIT} — a sitting's "
                         f"worth: review fatigue collapses past ~10-20 careful verdicts, and the queue "
                         f"is importance-ordered so the top slice is always the most valuable. "
                         f"--limit 0 = everything (the escape hatch).")
    ap.add_argument("--source", help="filter the queue to items whose cited SOURCE handle contains this "
                    "substring, case-insensitive — the originating project for transcripts (e.g. taro), "
                    "the file path for documents (e.g. CLAUDE.md)")
    ap.add_argument("--brief", action="store_true",
                    help="with --pending: the INDEX — one light row per item (title, standing, badges), "
                         "no evidence resolution. Pair with --card <id> per verdict so a sitting's "
                         "context stays one card deep regardless of backlog size")
    ap.add_argument("--maturity", type=float, default=temporal.MATURITY_WEIGHT, metavar="BAR",
                    help=f"the maturity BAR a takeaway's recency-weighted corroboration must cross to surface "
                         f"in --pending (default {temporal.MATURITY_WEIGHT} ≈ {temporal.MATURITY_SESSIONS} recent "
                         f"sessions). LOWER it to review more, RAISE it for only the most-corroborated — the "
                         f"bar is yours, nothing is hidden: --incubating lists what sits below, with the "
                         f"reason. Applies to --pending and --incubating.")
    ap.add_argument("--coalesce-hours", type=float, default=temporal.COALESCE_HOURS, metavar="H",
                    help=f"the SITTING window the maturity count groups by: same-repo sessions whose "
                         f"valid-times fall within this many hours count as ONE sitting, so a "
                         f"/clear-split afternoon cannot fake 2-session maturity (default "
                         f"{temporal.COALESCE_HOURS:g}; 0 = off — count every session separately). "
                         f"Applies to --pending, --incubating and --contested.")
    ap.add_argument("--split-parts",
                    help="a split accept's per-part EVIDENCE PARTITION: JSON [{title,statement,evidence}] "
                         "(the human's to choose — a queued split carries only title/statement)")
    ap.add_argument("--bytes", type=int, default=None, help="surrounding-context window size")
    ap.add_argument("--edit-title", help="accept with a corrected title")
    ap.add_argument("--edit-why", help="accept with a corrected why")
    ap.add_argument("--kind", choices=concepts.CONCEPT_KINDS,
                    help="accept with this kind, overriding the claim's proposal (default: the "
                         "proposal, else behavioral). behavioral shapes conduct and projects into "
                         "CLAUDE.md; reference is a fact/mechanism kept for lookup, excluded from "
                         "generation by default (ADR-0029)")
    ap.add_argument("--scope", metavar="REPO|global",
                    help="accept with this scope, overriding the derived proposal (default: the "
                         "repo the claim's evidence lives in, else global). A repo-scoped concept "
                         "is excluded from the global projection and routed by `generate --repo` "
                         "into that repo's CLAUDE.md (ADR-0030)")
    ap.add_argument("--allow-no-evidence", action="store_true",
                    help="accept a takeaway with no resolvable evidence (a deliberate, recorded override)")
    ap.add_argument("--assessment", default="", help="Claude's faithfulness assessment (recorded as provenance)")
    ap.add_argument("--note", default="", help="a reviewer note")
    ap.add_argument("--reason", default="", help="reason for reject/snooze/retire/reject-merge/merge-claims")
    ap.add_argument("--until", help="snooze re-surface time (ISO)")
    args = ap.parse_args(argv)

    # the sitting default (--pending/--incubating only): a bounded, importance-ordered slice.
    # None → SITTING_LIMIT; an explicit 0 → everything (pending()/the slices treat 0 as no-limit).
    sitting_limit = SITTING_LIMIT if args.limit is None else args.limit

    if args.pending:
        q, total = pending(context_bytes=args.bytes if args.bytes is not None else CONTEXT_BYTES,
                           brief=args.brief,
                           limit=sitting_limit, source_filter=args.source, maturity=args.maturity,
                           coalesce_hours=args.coalesce_hours, with_total=True)
        if args.json:
            print(json.dumps(q, ensure_ascii=False, indent=2))   # a LIST — the skill iterates it
        elif args.brief:
            _print_brief(q, total=total)
        else:
            _print_queue(q, total=total,
                         incubating_count=len(incubating(maturity=args.maturity,
                                                         coalesce_hours=args.coalesce_hours,
                                                         source_filter=args.source)),
                         bar=args.maturity)
    elif args.card:
        c = card(args.card, context_bytes=args.bytes if args.bytes is not None else CONTEXT_BYTES,
                 maturity=args.maturity, coalesce_hours=args.coalesce_hours)
        if c is None:
            ap.error(f"no claim or takeaway {args.card!r}")
        if args.json:
            print(json.dumps(c, ensure_ascii=False, indent=2))
        else:
            _print_queue([c], total=1, bar=args.maturity)
    elif args.incubating:
        inc = incubating(maturity=args.maturity, coalesce_hours=args.coalesce_hours,
                         source_filter=args.source)
        total = len(inc)                                         # the full depth, then slice for the sitting
        if sitting_limit > 0:
            inc = inc[:sitting_limit]
        if args.json:
            print(json.dumps(inc, ensure_ascii=False, indent=2))
        else:
            _print_incubating(inc, total=total)
    elif args.contested:
        rows = contested(maturity=args.maturity, coalesce_hours=args.coalesce_hours)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            _print_contested(rows)
    elif args.reject_merge:
        body = reject_merge(args.reject_merge, reason=args.reason)
        if args.json:
            print(json.dumps(body, ensure_ascii=False, indent=2))
        elif body.get("edge_id"):
            print(f"reject-merge recorded: edge {body['edge_id']} retracted, event {body['target']} "
                  f"reopened, pair blocked")
        else:
            print(f"reject-merge recorded: pair {tuple(body['pair'])} blocked — never suggested again")
    elif args.merge_claims:
        res = merge_claims(args.merge_claims[0], args.merge_claims[1], reason=args.reason)
        print(json.dumps(res, ensure_ascii=False) if args.json
              else f"merged claim {res['loser']} into {res['winner']} "
                   f"({res['moved_edges']} edge(s) re-pointed)")
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
                     kind=args.kind, scope=args.scope, allow_no_evidence=args.allow_no_evidence)
        cv = next((c for c in valid_concepts() if c["id"] == cid), {})
        kind = cv.get("kind", concepts.KIND_BEHAVIORAL)
        scope = cv.get("scope", concepts.SCOPE_GLOBAL)
        print(json.dumps({"accepted": args.accept, "concept": cid, "kind": kind, "scope": scope,
                          "edited": bool(edited)})
              if args.json else f"accepted → concept {cid}"
                                + (f" ({kind})" if kind != concepts.KIND_BEHAVIORAL else "")
                                + (f" (scope: {scope})" if scope != concepts.SCOPE_GLOBAL else "")
                                + (" (edited)" if edited else ""))
    elif args.set_kind:
        set_kind(args.set_kind[0], args.set_kind[1], reason=args.reason)
        print(f"concept {args.set_kind[0]} re-kinded → {args.set_kind[1]}"
              + ("" if args.set_kind[1] == concepts.KIND_BEHAVIORAL
                 else " (kept + queryable; excluded from generate's default projection)"))
    elif args.set_scope:
        set_scope(args.set_scope[0], args.set_scope[1], reason=args.reason)
        print(f"concept {args.set_scope[0]} re-scoped → {args.set_scope[1]}"
              + ("" if args.set_scope[1] == concepts.SCOPE_GLOBAL
                 else " (out of the global projection; `generate --repo "
                      f"{args.set_scope[1]}` routes it)"))
    elif args.reject:
        reject(args.reject, reason=args.reason, assessment=args.assessment)
        print(f"rejected {args.reject}")
    elif args.snooze:
        snooze(args.snooze, until=args.until, reason=args.reason)
        print(f"snoozed {args.snooze} until {args.until}")
    elif args.retire:
        retire(args.retire, reason=args.reason)
        print(f"retired concept {args.retire}")
    elif args.refresh:
        edited = {}
        if args.edit_title is not None:
            edited["title"] = args.edit_title
        if args.edit_why is not None:
            edited["why"] = args.edit_why
        res = refresh(args.refresh, edited=edited or None, note=args.note)
        print(json.dumps(res, ensure_ascii=False) if args.json
              else f"refreshed concept {res['concept']}: "
                   + " · ".join(f"{k} {res['before'][k]!r} → {res['after'][k]!r}"
                                for k in ("title", "statement")
                                if res["before"][k] != res["after"][k]))
    elif args.concepts:
        cs = valid_concepts()
        print(json.dumps(cs, ensure_ascii=False, indent=2) if args.json
              else "\n".join(f"  {c['id']}  {c.get('title', '')}"
                             + (f"  [{c['kind']}]" if c.get("kind") != concepts.KIND_BEHAVIORAL else "")
                             + (f"  [scope: {c['scope']}]"
                                if c.get("scope") not in (None, concepts.SCOPE_GLOBAL) else "")
                             for c in cs) or "  (no valid concepts yet)")
    elif args.proposals:
        q = pending_proposals(context_bytes=args.bytes if args.bytes is not None else CONTEXT_BYTES,
                              limit=args.limit or None, source_filter=args.source)   # 0 = everything here too
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


def _incubating_tail(incubating_count: int, *, bar: float | None = None) -> str:
    """A one-line footer noting how many takeaways are still accruing below the maturity bar — so an
    empty/short queue does not read as 'dream learned nothing' when lessons are in fact incubating. The bar
    is the reviewer's knob, so the footer says how to move it (ADR-0027), not just that things are hidden."""
    if not incubating_count:
        return ""
    knob = f" — lower the bar (--maturity, now {bar:g}) to review more, or" if bar is not None else " —"
    return (f"\n({incubating_count} takeaway(s) below the maturity bar{knob} see `--incubating` "
            f"for each with its score and why)")


def _print_queue(q: list[dict], *, total: int | None = None, incubating_count: int = 0,
                 bar: float | None = None) -> None:
    """`total` is the backlog depth BEFORE the sitting slice (pending's with_total) — the header states
    the slice honestly ("top N of M"), so the operator always knows what the slice was cut from without
    loading it. Absent (a full render), the header simply counts what is shown."""
    if not q:
        print("review queue empty — nothing over the maturity bar yet." + _incubating_tail(incubating_count, bar=bar))
        return
    total = len(q) if total is None else total
    if len(q) < total:
        print(f"showing top {len(q)} of {total} pending (by importance) — --limit to widen\n")
    else:
        print(f"showing all {total} pending (by importance)\n")
    for i, t in enumerate(q, 1):
        sup, rel = t["support"], t["relation"]["kind"]
        head = f"{i}/{len(q)} · {t['title']}  [{rel} · {sup['events']}ev/{sup['sessions']}sess"
        if t.get("kind") == "claim":
            head += f" · claim · {t.get('scope')}"
        print(head + "]")
        if "rationale" in t:                       # the gate, made transparent (ADR-0027)
            print(f"  MATURITY: {t['rationale']}")
        if t.get("why_pending"):                   # never withheld waiting on synthesize (§6)
            print("  WHY: (pending — synthesize hasn't run yet; the title is the seed event's summary. "
                  "Run `python -m ratchet.synthesize` to fill it, or accept with --edit-why.)")
        else:
            print(f"  WHY: {t['why']}"
                  + ("  [⚠ why-stale: the live evidence diverged since this prose was written]"
                     if t.get("why_stale") else ""))
        if t.get("claim_kind") == concepts.KIND_REFERENCE:   # only the non-default is worth a card line
            print("  KIND: reference (proposed) — a fact/mechanism to look up, not conduct; kept but "
                  "excluded from generate's default projection. --accept records it; --kind overrides.")
        if t.get("scope_repo") not in (None, concepts.SCOPE_GLOBAL):   # same judgment: global is noise
            print(f"  SCOPE: {t['scope_repo']} (derived) — every live quote sits in this repo; the "
                  f"lesson belongs in its CLAUDE.md, not the global one (`generate --repo "
                  f"{t['scope_repo']}`). --accept records it; --scope overrides.")
        if t.get("contested"):
            print("  ⚡ CONTESTED: carries a live contradiction — `--contested` shows the other side")
        for ev in t["evidence"]:
            print(f"    ✓ {ev['quote'][:100]!r}")
        _print_audit(t.get("audit"))
        _print_suggestions(t.get("merge_suggestions"))
        print()
    tail = _incubating_tail(incubating_count, bar=bar)
    if tail:
        print(tail.lstrip("\n"))


def _print_audit(audit: dict | None) -> None:
    """The audit card (§6.3): every corroboration's verified quote beside the match key the resolver
    persisted — the reviewer sees exactly what the model saw; ⚠ marks disjoint-subject evidence."""
    if not audit:
        return
    print("  AUDIT — every corroboration, with what the matcher saw:")
    for r in audit["corroborations"]:
        quote = (r.get("quote") or "(blob gone — span no longer resolves)")[:90]
        if r.get("match"):
            m = r["match"]
            print(f"    llm   ✓ {quote!r}")
            print(f"          [stmt_sim {m['stmt_sim']} · {m['candidates_shown']} candidate(s) shown "
                  f"· model {m['model']} · edge {r['edge_id']}]")
        else:
            print(f"    {str(r.get('by') or '?'):5} ✓ {quote!r}")
        if r.get("disjoint"):
            subj = r.get("subject") or {}
            where = subj.get("repo") or ", ".join((subj.get("files") or ())[:2]) or "?"
            print(f"          ⚠ disjoint subject: {where} shares no repo/file with the other evidence")
    print("  STORY: " + "; ".join(audit["story"]))


def _print_suggestions(suggs: list[dict] | None) -> None:
    """Derived merge suggestions (§6.5): both claims' titles + a verified quote each — the words, never
    the stmt_sim number (noise in the residue band)."""
    if not suggs:
        return
    print("  MERGE? residue-similar live claim(s) — the same lesson twice?")
    for sg in suggs:
        a, b = sg["claims"]
        for tag, side in (("·", a), ("~", b)):
            print(f"    {tag} {side['claim_id']}  {side['title'][:70]}")
            print(f"        {(side.get('quote') or '(no resolvable quote)')[:90]!r}")
        print(f"      confirm: --merge-claims LOSER WINNER · not the same: "
              f"--reject-merge {','.join(sg['pair'])}")


def _print_contested(rows: list[dict]) -> None:
    """Contested-near-bar claims (§6.6), each with its bar standing and the contradicting quotes as
    ground truth — a wrong llm CONTRADICTS verdict is caught here, not silently suppressed."""
    if not rows:
        print("no contested claims near the bar — no live contradiction is holding anything back.")
        return
    print(f"{len(rows)} contested claim(s) within {CONTESTED_WINDOW:g} of the bar:\n")
    for r in rows:
        sup = r["support"]
        standing = "MATURE — also in --pending" if r["mature"] else "held under the bar by the contradiction"
        print(f"  {r['claim_id']}  {r['title'][:70]}  [{sup['events']}ev/{sup['sessions']}sess · "
              f"{r['entrenchment']:g}/{r['bar']:g} · {standing}]")
        print(f"      {r['rationale']}")
        for quote in r["contradicting"]:
            print(f"      ✗ {quote[:100]!r}")
        print()


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


def _print_brief(rows: list[dict], *, total: int | None = None) -> None:
    """The index, one line per item: standing, badges, title — the human twin of `--brief --json`.
    No evidence, by design; `--card <id>` is the full view."""
    if not rows:
        print("review queue empty — nothing over the maturity bar yet.")
        return
    head = f"top {len(rows)} of {total}" if total is not None and total > len(rows) else f"{len(rows)} item(s)"
    print(f"pending index ({head}; importance desc) — `--card <id>` renders one in full:")
    for r in rows:
        badges = "".join([" [why-pending]" if r.get("why_pending") else "",
                          f" [{r['contradictions']} contradiction(s)]" if r.get("contradictions") else "",
                          f" [{r['claim_kind']}]" if r.get("claim_kind") == "reference" else "",
                          f" [scope {r['scope_repo']}]" if r.get("scope_repo") else ""])
        s = r.get("support") or {}
        print(f"  {r['takeaway_id']}  {r['entrenchment']:.2f}≥{r['bar']:.2f}  "
              f"{s.get('events', 0)}ev/{s.get('sessions', 0)}s{badges}  {r.get('title', '')}")


def _print_incubating(inc: list[dict], *, total: int | None = None) -> None:
    if not inc:
        print("nothing incubating — every live takeaway has reached the maturity bar.")
        return
    total = len(inc) if total is None else total
    if len(inc) < total:
        print(f"showing {len(inc)} of {total} takeaway(s) below the maturity bar "
              f"(nearest the bar first) — --limit to widen\n")
    else:
        print(f"{total} takeaway(s) below the maturity bar (accruing toward review):\n")
    for t in inc:
        sup = t["support"]
        score = t.get("entrenchment")
        head = f"  {t['title']}  [{sup['events']}ev/{sup['sessions']}sess"
        head += f" · {score:g}/{t['bar']:g}]" if score is not None else "]"
        print(head)
        if "rationale" in t:                       # WHY it is below the bar (ADR-0027) — the remedy differs
            print(f"      {t['rationale']}")


if __name__ == "__main__":
    main()
