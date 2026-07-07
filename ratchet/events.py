"""events — the event-fold view: glean's output re-anchored to its TRUSTED verbatim quote, scored,
and folded into the derived views both consolidators run over (ADR-0032 homes it here).

An EVENT is glean's unit of extraction — a blob version carrying a summary, markers, confidence, and
a byte span of verbatim evidence. What this module owns is everything DERIVED from events that no
stage can claim: the un-consolidated WORKING SET (latest events minus consolidated/stale — the queue
resolve and dream's legacy arm both drain), the SALIENCE order that drains it, the robust evidence
ANCHOR a takeaway/claim cites, and the sufficient statistics folded off that evidence. It grew up
inside dream v2 and moved here when dream was tombstoned (ADR-0032). The trust discipline
throughout: a stored span is never believed — it re-validates at every read AND write boundary
(`blobstore.validate_span`), so a quote is always real bytes of an immutable blob.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import blobstore, completer, config
from .glean import MARKER_KINDS   # the marker vocab is glean's; events carry it up

_score = completer.clean_score   # shared untrusted-score hygiene (clamp + scrub NaN/inf)

# Salience weights: surprise highest — a corrective/failure signal is the highest-value cue (ADR-0010 §8).
W_SURPRISE, W_INSIGHT, W_RESEARCH, EPS = 1.0, 0.7, 0.5, 0.01

# No relevance multiplier (ADR-0036): salience is the event's OWN durable-value, blind to the store.
# glean's per-event novelty verdict (4b/ADR-0019) once scaled this to drain novel-first and sink `known`
# — but sinking a `known` re-occurrence starved the very corroboration that matures a claim. Novelty now
# lives only in resolve (ADR-0028); `salience` == `intrinsic_salience` again.

FORGET_TAU = 4                           # consolidator cycles (dream/resolve runs) an event may sit
                                         # un-consolidated before forget-eligible
FORGET_SALIENCE_FLOOR = 0.05             # AND its salience must be below this (never age alone — ADR-0010 §6)
CONTEXT_BYTES = 160                      # surrounding-context window stored alongside each evidence quote


# --- the resolved event: a glean event re-anchored to its TRUSTED verbatim quote -----------------

@dataclass
class ResolvedEvent:
    event: dict
    quote: str               # the verbatim evidence, resolved + re-validated from the cleaned blob
    span: tuple[int, int]    # the VALIDATED [start, end) byte span the quote came from (emitted as evidence)
    session_id: str | None   # the originating session (support weighting by DISTINCT sessions)

    @property
    def id(self) -> str:
        return self.event["id"]


def resolve_event(e: dict, blobs: dict, sessions: dict, root: Path) -> ResolvedEvent | None:
    """Re-anchor one event to its trusted quote at the READ boundary. A consolidator must NOT trust the recorded
    span: an event is a blob version (model output + a span), and a buggy producer or out-of-band write
    can plant a malformed span, while Python slicing silently accepts `None` (the whole blob) and clamps
    overshoot. So the span is accepted only after `validate_span` proves it in-bounds ints and the bytes
    decode — re-establishing "the quote is real bytes of an immutable blob." A gone blob (TTL) or a
    failing span drops the event."""
    ch = e.get("cleaned_hash")
    ev = e.get("evidence") or []
    sp = ev[0] if ev and isinstance(ev[0], dict) else None
    if not ch or sp is None:
        return None
    try:
        data = blobs.get(ch)
        if data is None:
            data = blobstore.get(ch, root).encode("utf-8")
            blobs[ch] = data
    except (FileNotFoundError, OSError):
        return None
    span = blobstore.validate_span(data, sp.get("byte_start"), sp.get("byte_end"))   # the read-side anchor
    if span is None:
        return None
    try:
        quote = data[span[0]:span[1]].decode("utf-8")
    except UnicodeDecodeError:    # a span splitting a multibyte char is not a real quote
        return None
    if not quote.strip():
        return None
    return ResolvedEvent(event=e, quote=quote, span=span,
                         session_id=blobstore.session_of(ch, root, sessions))


# --- the working set: the un-consolidated queue both consolidators drain -------------------------

def working_set(root: Path | None = None, *, min_confidence: float = 0.0) -> list[ResolvedEvent]:
    """The un-consolidated events = `latest_by_kind('event')` (latest version per event_id) MINUS every
    event whose latest LIFECYCLE decision is `consolidated` or `stale`. Rides `latest_decisions`, which
    already EXCLUDES the producer `processed` markers — exactly right: a per-item `processed` marker is
    bookkeeping, not membership state (and reusing the fold avoids the resurrection-bug class review
    already guards). Each survivor is re-anchored to its trusted quote (`validate_span` at the read
    boundary). This is `DreamBlock.items()`/`ResolveBlock.items()`'s source; the driver sorts it by
    `priority` (salience)."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    blobs: dict[str, bytes] = {}
    sessions: dict[str, str | None] = {}
    out: list[ResolvedEvent] = []
    for sid, h in blobstore.latest_by_kind("event", root).items():
        d = decisions.get(sid)
        if d and d.get("verb") in ("consolidated", "stale"):
            continue
        try:
            e = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(e, dict):
            continue
        e.setdefault("id", sid)                       # source_id is authoritative; mirror it
        if _score(e.get("confidence"), 0.0) < min_confidence:
            continue
        rv = resolve_event(e, blobs, sessions, root)
        if rv is not None:
            out.append(rv)
    return out


def filter_by_source(events: list[ResolvedEvent], source_filter: str, root: Path) -> list[ResolvedEvent]:
    """PROCESSING FOCUS (`--source` on dream/resolve, ADR-0022): keep only events whose SOURCE handle CONTAINS
    `source_filter` (case-insensitive substring) — the originating project for transcripts, the file path
    for documents. The handle is reached by the SAME lineage hop the stage already walks for age/facets —
    event → `cleaned_hash` → raw `origin_ref.project` (`blobstore.project_of`, one cached meta read per
    event, no LLM/content). An event whose handle can't be resolved does NOT match (focus NARROWS — an
    unknowable origin is excluded, not waved through). Default (no filter) skips this entirely. A semantic
    TAG filter (focus by garden vocabulary, not the source handle) is the deferred richer sibling knob —
    this is the cheap provenance substring."""
    t = source_filter.lower()
    cache: dict = {}
    return [rv for rv in events
            if (p := blobstore.project_of(rv.event.get("cleaned_hash"), root, cache)) and t in p.lower()]


def event_born_map(root: Path | None = None) -> dict[str, str | None]:
    """event_id → its blob's `fetched_at` (the recency stamp) — the AGE source for the `Aging` priority
    policy (ADR-0021): an event's age = `now() - born`. ONE scan over `latest_by_kind('event')` + a meta
    read per event; built LAZILY (only when Aging is selected — Greedy never reads age) and shared by
    the blocks' `age` and the `--dry-run` previews so a preview can't lie about the aging order. A gone /
    unreadable meta maps to None → `config.age_days` treats it as fresh (0.0); never raises."""
    root = root or config.data_root()
    out: dict[str, str | None] = {}
    for sid, h in blobstore.latest_by_kind("event", root).items():
        try:
            out[sid] = blobstore.get_meta(h, root).get("fetched_at")
        except (OSError, json.JSONDecodeError):
            out[sid] = None
    return out


# --- salience: the priority-queue score (pure, stdlib) --------------------------------------------

def intrinsic_salience(event: dict) -> float:
    """The event's OWN durable-value salience: confidence × weighted marker mass (with its +EPS floor),
    no novelty term. Surprise weighed highest among markers — a corrective/failure signal is the
    highest-value cue (ADR-0010 §8). Each untrusted field is scrubbed through `clean_score`. Since
    ADR-0036 this IS `salience` (the relevance multiplier is gone); `forget` keeps naming this score, so
    eviction stays pinned to an event's own value, independent of any future queue-ordering term."""
    conf = _score(event.get("confidence"), 0.5)
    m = event.get("markers") or {}
    return conf * (W_SURPRISE * _score(m.get("surprise")) + W_INSIGHT * _score(m.get("insight"))
                   + W_RESEARCH * _score(m.get("research")) + EPS)


def salience(event: dict) -> float:
    """The priority-QUEUE score of an un-consolidated event — now IDENTICAL to `intrinsic_salience`
    (ADR-0036 removed the relevance multiplier). WHY the term is gone: relevance was glean's
    novelty-vs-the-GLOBAL-store verdict (4b/ADR-0019), and sinking `known` events was in direct
    conflict with corroboration. A lesson recurring in a DISTINCT session is exactly what matures a
    claim — but glean, primed with the global digest, marked that re-occurrence `known`, salience sank
    it, and under any budget cap it never reached resolve, so the corroboration never happened. Novelty
    against the store belongs in ONE place — resolve's statement-first matching (ADR-0028), which USES
    the re-occurrence to corroborate rather than suppressing it — never in the per-chunk extraction's
    queue order. Reading `relevance` off old events is dropped here (the field lingers on pre-0036 blobs,
    harmlessly ignored). The blocks' `priority` delegates here; `forget` already read `intrinsic_salience`."""
    return intrinsic_salience(event)


def event_markers(event: dict) -> dict:
    """The event's marker vector, each field scrubbed — the per-takeaway markers aggregate (max) these."""
    em = event.get("markers") or {}
    return {k: _score(em.get(k)) for k in MARKER_KINDS}


# --- evidence: the robust anchor + the sufficient statistics folded off it ------------------------

def evidence_entry(rv: ResolvedEvent, *, context_bytes: int = CONTEXT_BYTES,
                   root: Path | None = None) -> dict:
    """A ROBUST ANCHOR (W3C Web Annotation) for one cited event: the byte span AND the verbatim quote +
    a little surrounding context. The span is RE-VALIDATED via `validate_span` at WRITE — the same
    immutable, content-addressed blob `working_set` resolved, so this re-anchors at the write boundary
    too (`root` threads the caller's store, so an injected root re-validates against the SAME store the
    span resolved from). If the blob is gone, the (already-validated) span + quote still ship; only the
    context window is best-effort."""
    ch = rv.event.get("cleaned_hash")
    entry = {"event_id": rv.id, "cleaned_hash": ch,
             "byte_start": rv.span[0], "byte_end": rv.span[1],
             "quote": rv.quote, "context": rv.quote}
    try:
        data = blobstore.get(ch, root).encode("utf-8")
    except (FileNotFoundError, OSError):
        return entry
    span = blobstore.validate_span(data, rv.span[0], rv.span[1])   # re-validate at WRITE
    if span is None:
        return entry
    bs, be = span
    cs, ce = max(0, bs - context_bytes), min(len(data), be + context_bytes)
    entry["context"] = data[cs:ce].decode("utf-8", errors="replace")   # window edges may split a char
    return entry


def contradiction_stats(tk: dict) -> dict:
    """The BIRCH sufficient-statistic for the CONTRADICTION side, mirroring `support`: distinct
    contradicting events (by event_id) and distinct contradicting sessions (each contradiction_evidence
    entry carries the `session_id` it came from — so the count needs no parallel session list). Closed
    over the summary, correct without the raw events."""
    by = tk.get("contradicted_by") or []
    sess = {e.get("session_id") for e in (tk.get("contradiction_evidence") or []) if e.get("session_id")}
    return {"events": len(set(by)), "sessions": len(sess)}
