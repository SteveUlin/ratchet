"""dream v2 — incremental WORKING-SET CONSOLIDATION: route each un-consolidated glean event to one of
NEW / STRENGTHEN / NOOP and maintain a small, live CATALOG of durable, evidence-cited **takeaways**
(ADR-0010, superseding 0006's global re-cluster).

    … chunk → chunkset → glean → events → dream → takeaways → [human review] → concepts → generate
                                            (consolidation: a cheap router + a rare synthesizer)

v1 clustered the WHOLE event pile every run, then one Sonnet call per cluster. It under-merged (lexical
TF-IDF over short quotes is too sparse), churned (a new event shifted cluster signatures and re-fired
synthesis), and never "failed in the middle." v2 inverts it into a bounded, iterative, ORDER-aware loop
— and, with no embeddings available (only the authed claude CLI), uses the LLM ITSELF as the similarity
oracle over an IN-PROMPT catalog, not a vector index.

The model (ADR-0010):

  WORKING SET — a DERIVED query: every glean event (latest version) MINUS those whose latest lifecycle
  decision is `consolidated` or `stale`. Ordered by SALIENCE (markers × confidence) into a PRIORITY
  QUEUE, so the strongest evidence seeds/corroborates first and stragglers wait (and are forgotten
  first). This is `DreamBlock.items()`.

  CATALOG — the current takeaways: `latest_by_kind('takeaway')` minus any with a merge/retire/reject
  decision. Small by design (forgetting + the maturity gate bound it), so the WHOLE catalog renders into
  the route prompt — NO retrieval, NO top-K, NO embeddings; the router reads it all.

  ROUTE — one cheap Haiku call per event: given the event's VERIFIED verbatim quote + summary and the
  catalog as a numbered list, return {decision: new|strengthen|noop, takeaway_id}. A strengthen naming
  an id not in the catalog is coerced to `new` (defensive, like `_clean_relation`).

  APPLY —
    new        → SYNTHESIZE (one Sonnet call): mint a STABLE takeaway id (`t-`+sha256(seed_event)[:12]),
                 write the takeaway with the cited event's evidence (span + verbatim quote + context),
                 support {events:1, sessions:1}; then a `consolidated` decision for the event.
    strengthen → BUMP SUPPORT (cheap, NO LLM by default): a NEW VERSION of that takeaway id (the
                 blobstore TimeMap versions it, latest wins) — append the new event's evidence (dedup by
                 event_id), union distinct sessions, recompute support (the BIRCH sufficient-statistic),
                 max-merge markers, bump last_seen. RE-SYNTHESIZE the `why` only when lexical drift
                 crosses a threshold; else just bump. Then a `consolidated` decision.
    noop       → nothing durable; the event stays un-consolidated (later forgotten).

  TAKEAWAY IDENTITY — the key change from v1: `source_id` is a STABLE MINTED id, so an UPDATE is a new
  VERSION of the same id (no membership churn). The v1 coverage-conditioned supersession is GONE; the
  current set is `latest_by_kind('takeaway')` latest-per-id minus merged/retired. The stored content is a
  strict SUPERSET of what review reads, so review.py and the trust chain are UNCHANGED.

dream v2 is a `block.Block` (ADR-0009) that commits PER ITEM (the inversion of v1's per-run finalize):
`items()` yields the salience-ordered working set; `priority()` orders it; `process()` routes + applies,
committing each event's takeaway version + `consolidated` decision immediately (resumable, fail-in-the-
middle), and folds the result into the ON-INSTANCE catalog so the NEXT event routes against the updated
catalog. `finalize()` runs a conservative `forget` pass. The route and synth calls are SEPARATE injected
`Completer`s (Haiku + Sonnet), so the whole loop is offline-testable with fakes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import blobstore, block, completer, config
from .completer import Completer
from .glean import MARKER_KINDS          # the marker vocabulary is glean's; dream carries it upward

PROMPT_VERSION = "dream/2"               # bump to re-route NOT-yet-consolidated events with a sharper prompt
ROUTE_MODEL = "haiku"                    # the router is cheap + per-event → the small model
SYNTH_MODEL = "sonnet"                   # synthesis is rare (new + drift) → afford a sharper model
OUT_NOUN = "takeaways"                   # the per-event output noun the Progress bar/line shows

MATURITY_SESSIONS = 2                    # the review bar: corroborated across this many DISTINCT sessions
DRIFT_THRESHOLD = 0.5                    # lexical drift above which a strengthen re-synthesizes the why
FORGET_TAU = 4                           # dream cycles an event may sit un-consolidated before forget-eligible
FORGET_SALIENCE_FLOOR = 0.05             # AND its salience must be below this (never age alone — ADR-0010 §6)
CONTEXT_BYTES = 160                      # surrounding-context window stored alongside each evidence quote

TITLE_MAX = 80
WHY_MAX = 280
NOTE_MAX = 160

# Salience weights: surprise highest — a corrective/failure signal is the highest-value cue (ADR-0010 §8).
W_SURPRISE, W_INSIGHT, W_RESEARCH, EPS = 1.0, 0.7, 0.5, 0.01

_RELATION_KINDS = ("new", "strengthens", "refines", "contradicts")

ROUTE_SYSTEM = (
    "You are the ROUTER for a developer's long-term memory. A new OBSERVATION arrived from a Claude "
    "Code session; you are shown the CURRENT CATALOG of takeaways the memory already holds. Decide "
    "where the observation belongs:\n"
    "  - strengthen: the catalog ALREADY has a takeaway capturing this same underlying lesson — name "
    "its id (the observation is more evidence for it).\n"
    "  - new: nothing in the catalog covers it — it should seed a new takeaway.\n"
    "  - noop: it is noise, not a durable reusable lesson — drop it.\n"
    "The observation's verbatim QUOTE is ground truth; its one-line summary is only a hint. Prefer "
    "strengthen when a catalog entry plausibly covers the SAME lesson; prefer new for a genuinely "
    "distinct lesson; reserve noop for noise.\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{"decision": "new"|"strengthen"|"noop", "takeaway_id": the catalog id to strengthen or null}'
)

SYNTH_SYSTEM = (
    "You distill ONE raw observation from a developer's Claude Code session into a durable, reusable "
    "TAKEAWAY: the underlying 'why' a future session would benefit from knowing.\n\n"
    "The observation has a VERBATIM quote (the ground truth — trust it) and a one-line machine summary "
    "(a hint that may be imprecise or wrong). State the principle it carries and WHY it holds — do not "
    "just restate the quote.\n\n"
    "You are also given the developer's already-known CONCEPTS. Judge how this takeaway relates: new "
    "(nothing covers it), strengthens (more evidence for one), refines (narrows/extends one), or "
    "contradicts (overturns one — the most important to surface).\n\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{\n'
    '  "title": a short noun phrase naming the takeaway (<= 80 chars),\n'
    '  "why": one or two sentences — the durable principle and why it holds (<= 280 chars),\n'
    '  "relation": {"kind": "new"|"strengthens"|"refines"|"contradicts", "concept_id": the related '
    'concept id or null, "note": a brief reason (<= 160 chars)},\n'
    '  "confidence": 0-1, how durable and reusable this is,\n'
    '  "drop": true if this observation is noise, not a durable learning (then everything else is ignored)\n'
    '}'
)

# Lexical stopwords for the drift/merge similarity — drop connective words so similarity keys on the
# technical content (tool names, paths, error strings). Deliberately small: domain terms (git/jj) survive.
_STOPWORDS = frozenset(
    "the a an and or but if then else when of to in on at by for with from into over as is are was "
    "were be been being it its this that these those you your he she they we i me my our their them "
    "do does did done can could should would will shall may might must not no yes so than too very "
    "use used using user about which who what why how out up down off here there".split())


# --- the concept seam: dream reads the curated-knowledge layer the review gate writes ----------

def load_concepts(root: Path | None = None) -> list[dict]:
    """The current VALID concept set — the human-reviewed source of truth dream judges belief-change
    against (ADR-0006/0007). A concept is a versioned blob the `review` stage ingests; "valid" is
    derived: the latest version of each concept source, minus any whose latest decision is `retire`.
    This is the loop closing — review's accepts become the concepts dream reads next run. Empty until
    review runs, so every takeaway is `new`. Malformed/absent → skipped, never fatal."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    out: list[dict] = []
    for sid, h in blobstore.latest_by_kind("concept", root).items():
        d = decisions.get(sid)
        if d and d.get("verb") == "retire":      # retired out of the valid set (ADR-0007 §4)
            continue
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str) and obj["id"]:
            out.append(obj)
    return out


def _render_concepts(concepts: list[dict]) -> str:
    if not concepts:
        return "(none known yet — treat every takeaway as new)"
    return "\n".join(f"- id {c['id']}: {str(c.get('title', '')).strip()} — "
                     f"{str(c.get('statement', '')).strip()[:200]}" for c in concepts)


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


def _resolve_event(e: dict, blobs: dict, sessions: dict, root: Path) -> ResolvedEvent | None:
    """Re-anchor one event to its trusted quote at the READ boundary. dream must NOT trust the recorded
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


# --- working set + catalog: the two DERIVED views the loop runs over ------------------------------

def working_set(root: Path | None = None, *, min_confidence: float = 0.0) -> list[ResolvedEvent]:
    """The un-consolidated events = `latest_by_kind('event')` (latest version per event_id) MINUS every
    event whose latest LIFECYCLE decision is `consolidated` or `stale`. Rides `latest_decisions`, which
    already EXCLUDES the producer `processed` markers — exactly right: a per-item `processed` marker is
    bookkeeping, not membership state (and reusing the fold avoids the resurrection-bug class review
    already guards). Each survivor is re-anchored to its trusted quote (`validate_span` at the read
    boundary). This is `DreamBlock.items()`'s source; the driver sorts it by `priority` (salience)."""
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
        rv = _resolve_event(e, blobs, sessions, root)
        if rv is not None:
            out.append(rv)
    return out


def catalog(root: Path | None = None) -> list[dict]:
    """The full LIVE routing catalog: `latest_by_kind('takeaway')` (latest version per stable id) MINUS
    any takeaway whose latest lifecycle decision is in {merge, retire, reject}. INCLUDES incubating
    takeaways (sessions < the maturity bar) so the router can strengthen them toward maturity. Small by
    design (forgetting + the maturity gate bound it) → the whole list renders into the route prompt; no
    retrieval, no embeddings. This REPLACES v1's supersede-fold for routing."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    out: list[dict] = []
    for sid, h in blobstore.latest_by_kind("takeaway", root).items():
        d = decisions.get(sid)
        if d and d.get("verb") in ("merge", "retire", "reject"):
            continue
        try:
            t = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(t, dict) and (t.get("id") or sid):
            t.setdefault("id", sid)
            out.append(t)
    return out


def current_takeaways(root: Path | None = None, *, min_sessions: int = MATURITY_SESSIONS) -> list[dict]:
    """The MATURITY GATE, and review's unchanged feed: `catalog(root)` filtered to MATURE takeaways
    (support.sessions >= min_sessions). `review.pending()` calls this with the default bar, so only
    takeaways corroborated across that many DISTINCT sessions reach the human gate; incubating ones stay
    live (routable via `catalog()`) but invisible to review. This REPLACES v1's supersede-conditioned
    current_takeaways — same name/return type, so review.py is untouched."""
    return [t for t in catalog(root)
            if (t.get("support") or {}).get("sessions", 0) >= min_sessions]


def render_catalog(cat: list[dict]) -> str:
    """The in-prompt routing list — a numbered list of `[N] id=<id>: <title> — <why>` (truncated). The
    WHOLE catalog goes in the prompt (no top-K). An empty catalog renders a sentinel so the router is
    never asked to strengthen against nothing."""
    if not cat:
        return "(no takeaways yet — every observation is new)"
    lines = []
    for i, t in enumerate(cat, 1):
        title = str(t.get("title", "")).strip()[:TITLE_MAX]
        why = str(t.get("why", "")).strip()[:WHY_MAX]
        lines.append(f"[{i}] id={t.get('id')}: {title} — {why}")
    return "\n".join(lines)


# --- scoring + similarity (pure, stdlib): salience for the queue, drift/merge for maintenance -----

_score = completer.clean_score   # shared untrusted-score hygiene (clamp + scrub NaN/inf)


def salience(event: dict) -> float:
    """A pure score of an un-consolidated event for the priority queue: confidence × weighted marker
    mass. Surprise weighed highest — a corrective/failure signal is the highest-value cue (ADR-0010 §8).
    Each untrusted field is scrubbed through `clean_score`. The recurrence bonus (does the event already
    match a catalog takeaway) is DEFERRED (it needs a pre-route scan). `DreamBlock.priority` delegates
    here."""
    conf = _score(event.get("confidence"), 0.5)
    m = event.get("markers") or {}
    mass = (W_SURPRISE * _score(m.get("surprise")) + W_INSIGHT * _score(m.get("insight"))
            + W_RESEARCH * _score(m.get("research")) + EPS)
    return conf * mass


def _event_markers(event: dict) -> dict:
    """The event's marker vector, each field scrubbed — the per-takeaway markers aggregate (max) these."""
    em = event.get("markers") or {}
    return {k: _score(em.get(k)) for k in MARKER_KINDS}


def _tokens(text: str) -> list[str]:
    """Content tokens for similarity: lowercase, split on non-identifier punctuation but KEEP the
    characters that carry technical meaning (`$ _ . / -`, so `$RATCHET_DATA_DIR` and `--max-turns`
    survive whole). Drop stopwords and 1-2 char noise."""
    return [w for w in re.split(r"[^a-z0-9$_./-]+", text.lower())
            if len(w) > 2 and w not in _STOPWORDS]


def _idf(docs: list[list[str]]) -> dict[str, float]:
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        for t in set(d):
            df[t] += 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def _vec(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """An L2-normalized TF-IDF vector as a sparse {token: weight} dict (empty if no content tokens)."""
    tf = Counter(tokens)
    v = {t: tf[t] * idf.get(t, 0.0) for t in tf}
    norm = math.sqrt(sum(w * w for w in v.values()))
    return {t: w / norm for t, w in v.items()} if norm else {}


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


def _drift(rv: ResolvedEvent, tk: dict) -> float:
    """Lexical distance of a strengthening event from the takeaway it bumps: `1 - cos` over TF-IDF of
    (event quote + summary) vs (takeaway title + why + its evidence quotes). High-distance evidence means
    the takeaway's `why` may no longer cover it → re-synthesize; near evidence is a cheap bump. Lexical
    (no embeddings) is low-precision, so the gate defaults to cheap-bump and re-synths only on a clearly
    distant event (ADR-0010's write-amplification caution)."""
    ev_quotes = " ".join(str(e.get("quote", "")) for e in (tk.get("evidence") or []))
    a = rv.quote + " " + str(rv.event.get("summary", ""))
    b = f"{tk.get('title', '')} {tk.get('why', '')} {ev_quotes}"
    docs = [_tokens(a), _tokens(b)]
    idf = _idf(docs)
    return 1.0 - _cos(_vec(docs[0], idf), _vec(docs[1], idf))


# --- the takeaway: stable minted id, robust-anchored evidence, BIRCH support stats ----------------

def mint_takeaway_id(seed_event_id: str) -> str:
    """The STABLE id of a new takeaway — DETERMINISTIC on the seeding event id (NOT random, NOT a
    cluster signature). Determinism is load-bearing for resumability: a crash-retry of the same `new`
    event re-mints the SAME id, so the duplicate is absorbed as a no-op TimeMap version (latest wins)
    instead of orphaning a second takeaway. Independent of prompt/model (a prompt bump never re-routes a
    consolidated event, so the id need not vary by params). Distinct event_ids → distinct ids; 12 hex of
    sha256 makes collision risk negligible."""
    return "t-" + hashlib.sha256(seed_event_id.encode("utf-8")).hexdigest()[:12]


def evidence_entry(rv: ResolvedEvent, *, context_bytes: int = CONTEXT_BYTES) -> dict:
    """A ROBUST ANCHOR (W3C Web Annotation) for one cited event: the byte span AND the verbatim quote +
    a little surrounding context. The span is RE-VALIDATED via `validate_span` at WRITE — the same
    immutable, content-addressed blob `working_set` resolved, so this re-anchors at the write boundary
    too. If the blob is gone, the (already-validated) span + quote still ship; only the context window is
    best-effort."""
    ch = rv.event.get("cleaned_hash")
    entry = {"event_id": rv.id, "cleaned_hash": ch,
             "byte_start": rv.span[0], "byte_end": rv.span[1],
             "quote": rv.quote, "context": rv.quote}
    try:
        data = blobstore.get(ch).encode("utf-8")
    except (FileNotFoundError, OSError):
        return entry
    span = blobstore.validate_span(data, rv.span[0], rv.span[1])   # re-validate at WRITE
    if span is None:
        return entry
    bs, be = span
    cs, ce = max(0, bs - context_bytes), min(len(data), be + context_bytes)
    entry["context"] = data[cs:ce].decode("utf-8", errors="replace")   # window edges may split a char
    return entry


def _clean_relation(v, known_ids: set[str]) -> dict:
    """Coerce the untrusted relation: an unknown kind → `new`, and a `concept_id` that isn't a real known
    concept → null (which forces `new` — you cannot strengthen/refine/contradict a concept that does not
    exist, so today, with no concepts, every relation is `new`). Unchanged from v1; powers review's
    loop-close."""
    v = v if isinstance(v, dict) else {}
    kind = v.get("kind") if v.get("kind") in _RELATION_KINDS else "new"
    cid = v.get("concept_id")
    cid = cid if isinstance(cid, str) and cid in known_ids else None
    if cid is None and kind != "new":
        kind = "new"
    return {"kind": kind, "concept_id": cid, "note": str(v.get("note", "")).strip()[:NOTE_MAX]}


def takeaway_content(tk: dict) -> dict:
    """The run-invariant STORED projection of a takeaway version — drops the in-memory `producer`/cost,
    keeps id, title, why, relation, cites, evidence, support, sessions_seen, markers, confidence,
    last_seen. This is what makes a no-op re-version re-hash IDENTICALLY: a strengthen that incorporates
    no new event re-serializes to the same bytes, so the blobstore no-ops it (no churn)."""
    return {k: tk[k] for k in (
        "id", "title", "why", "relation", "cites", "evidence",
        "support", "sessions_seen", "markers", "confidence", "last_seen")}


def _ingest_takeaway(tk: dict, *, model: str, run_id: str, root: Path | None) -> None:
    """Freeze one takeaway as a RAW blob VERSION keyed on its STABLE minted id (ADR-0007). The content is
    `takeaway_content(tk)` (run-invariant → an unchanged re-version no-ops); `producer` is mirrored into
    `origin_ref` so provenance is answerable from meta alone. An UPDATE re-uses the same source_id → a new
    prev-linked version (the TimeMap, latest wins) — no membership churn."""
    blobstore.ingest(
        blobstore.canonical_json(takeaway_content(tk)), source_kind="takeaway",
        source_id=tk["id"],
        origin_ref={"stage": "dream", "model": model, "prompt_version": PROMPT_VERSION,
                    "run_id": run_id, "cost_usd": (tk.get("producer") or {}).get("cost_usd")},
        root=root)


# --- ROUTE: one cheap Haiku call per event over the in-prompt catalog -----------------------------

def _route_user(rv: ResolvedEvent, cat: list[dict]) -> str:
    return (f'OBSERVATION\nquote: """{rv.quote}"""\n'
            f'summary: {str(rv.event.get("summary", "")).strip()!r}\n\n'
            f'CATALOG\n{render_catalog(cat)}')


def _clean_route(parsed: dict, catalog_ids: set[str]) -> dict:
    """Defensive coercion of the untrusted router JSON, mirroring `_clean_relation`. Rules: a decision
    not in {new, strengthen, noop} → `noop` (safest: no write, the event stays routable on a prompt
    bump); a `strengthen` whose takeaway_id is not a real catalog id (incl. null) → coerced to `new`
    (the spec's explicit rule — you cannot strengthen an id the model was not shown); `new` forces
    takeaway_id None (a new mints fresh)."""
    parsed = parsed if isinstance(parsed, dict) else {}
    decision = parsed.get("decision")
    if decision not in ("new", "strengthen", "noop"):
        return {"decision": "noop", "takeaway_id": None}
    if decision in ("new", "noop"):
        return {"decision": decision, "takeaway_id": None}
    tid = parsed.get("takeaway_id")
    if isinstance(tid, str) and tid in catalog_ids:
        return {"decision": "strengthen", "takeaway_id": tid}
    return {"decision": "new", "takeaway_id": None}      # strengthen of an unknown id → new


def route(rv: ResolvedEvent, cat: list[dict], complete_route: Completer) -> tuple[dict, float]:
    """ONE cheap Haiku call (the injected ROUTER) per event: system = the routing instruction; user =
    the event (verbatim quote + summary) + the WHOLE catalog as a numbered list. Returns (route_result,
    cost). The router may answer with a catalog list-NUMBER instead of the id (a common mis-naming) — map
    it back before coercion. A raised completer propagates so the driver isolates the event (retried next
    run). Pure-injectable: offline-tested with a fake that echoes a chosen decision/id."""
    comp = complete_route(ROUTE_SYSTEM, _route_user(rv, cat))
    cost = completer.cost_of(comp)
    parsed = completer.parse_json_object(comp.text) or {}
    tid = parsed.get("takeaway_id")
    if isinstance(tid, (int, str)) and not isinstance(tid, bool):   # accept the list-number, map → id
        s = str(tid).strip()
        if s.isdigit() and 1 <= int(s) <= len(cat):
            parsed = dict(parsed)
            parsed["takeaway_id"] = cat[int(s) - 1].get("id")
    rr = _clean_route(parsed, {t.get("id") for t in cat})
    return rr, cost


# --- APPLY: synthesize a new takeaway, or bump an existing one's support ---------------------------

def _synth_user(rv: ResolvedEvent, concepts: list[dict]) -> str:
    return (f"Known concepts:\n{_render_concepts(concepts)}\n\n"
            f'OBSERVATION\nquote: """{rv.quote}"""\n'
            f'summary: {str(rv.event.get("summary", "")).strip()!r}')


def synthesize_new(rv: ResolvedEvent, complete_synth: Completer, concepts: list[dict], *,
                   known_concept_ids: set[str], model: str, run_id: str) -> tuple[dict | None, float]:
    """Mint a takeaway from ONE event (the injected SYNTH Completer, Sonnet). `drop` or no usable `why`
    → (None, cost): a successful adjudication that this event is noise (apply marks it consolidated so it
    is not re-synthesized). Else build the v2 takeaway: a STABLE minted id, one robust-anchored evidence
    entry (span + verbatim quote + context, re-validated on write), support {1, 1}, the cited event's
    markers, a coerced relation, last_seen = now. Raises only if the completer fails (the driver isolates
    it)."""
    comp = complete_synth(SYNTH_SYSTEM, _synth_user(rv, concepts))
    cost = completer.cost_of(comp)
    parsed = completer.parse_json_object(comp.text) or {}
    if not parsed or parsed.get("drop"):
        return None, cost
    why = str(parsed.get("why", "")).strip()[:WHY_MAX]
    if not why:                                    # no usable principle → treat as noise
        return None, cost
    tk = {
        "id": mint_takeaway_id(rv.id),
        "title": str(parsed.get("title", "")).strip()[:TITLE_MAX],
        "why": why,
        "relation": _clean_relation(parsed.get("relation"), known_concept_ids),
        "cites": [rv.id],
        "evidence": [evidence_entry(rv)],
        "support": {"events": 1, "sessions": 1 if rv.session_id else 0},
        "sessions_seen": [rv.session_id] if rv.session_id else [],
        "markers": _event_markers(rv.event),
        "confidence": _score(parsed.get("confidence"), 0.5),
        "last_seen": config.now(),
        "producer": {"stage": "dream", "model": model, "prompt_version": PROMPT_VERSION,
                     "run_id": run_id, "cost_usd": round(cost, 8)},
    }
    return tk, cost


def update_support(tk: dict, rv: ResolvedEvent, complete_synth: Completer, concepts: list[dict], *,
                   known_concept_ids: set[str], drift_threshold: float,
                   model: str, run_id: str) -> tuple[dict, float]:
    """BUMP SUPPORT — the cheap path (NO LLM by default). Build a NEW VERSION of `tk` (same id): append
    the event's evidence IDEMPOTENTLY (dedup by event_id — a re-strengthen of an already-cited event is a
    byte-identical no-op, so sessions never inflate and a one-off can't falsely mature); union the
    distinct session; recompute support = {events: distinct cites, sessions: distinct sessions} — the
    BIRCH sufficient-statistic, correct without the raw events; max-merge markers; bump last_seen. DRIFT
    GATE: only when the event is lexically distant from the takeaway (`_drift` > threshold) is the `why`
    re-synthesized (one Sonnet call, cost > 0); else keep it (cost 0.0)."""
    nv = {
        "id": tk["id"],
        "title": str(tk.get("title", "")),
        "why": str(tk.get("why", "")),
        "relation": tk.get("relation") or {"kind": "new", "concept_id": None, "note": ""},
        "cites": list(tk.get("cites") or []),
        "evidence": [dict(e) for e in (tk.get("evidence") or [])],
        "sessions_seen": list(tk.get("sessions_seen") or []),
        "markers": {k: _score((tk.get("markers") or {}).get(k)) for k in MARKER_KINDS},
        "confidence": _score(tk.get("confidence"), 0.5),
        "last_seen": str(tk.get("last_seen", "")),
    }
    cited = {e.get("event_id") for e in nv["evidence"]}
    if rv.id in cited:                             # already incorporated → idempotent, byte-stable no-op
        nv["support"] = {"events": len(set(nv["cites"])), "sessions": len(set(nv["sessions_seen"]))}
        return nv, 0.0
    nv["evidence"].append(evidence_entry(rv))
    nv["cites"].append(rv.id)
    if rv.session_id and rv.session_id not in nv["sessions_seen"]:
        nv["sessions_seen"].append(rv.session_id)
    em = _event_markers(rv.event)
    nv["markers"] = {k: max(nv["markers"][k], em[k]) for k in MARKER_KINDS}
    nv["support"] = {"events": len(set(nv["cites"])), "sessions": len(set(nv["sessions_seen"]))}
    nv["last_seen"] = config.now()
    cost = 0.0
    if _drift(rv, tk) > drift_threshold:           # far evidence → the why may no longer cover it
        comp = complete_synth(SYNTH_SYSTEM, _synth_user(rv, concepts))
        cost = completer.cost_of(comp)
        parsed = completer.parse_json_object(comp.text) or {}
        why = str(parsed.get("why", "")).strip()[:WHY_MAX]
        if why:
            nv["why"] = why
            nv["title"] = str(parsed.get("title", "")).strip()[:TITLE_MAX] or nv["title"]
            nv["relation"] = _clean_relation(parsed.get("relation"), known_concept_ids)
            nv["confidence"] = _score(parsed.get("confidence"), nv["confidence"])
    return nv, cost


def apply(rv: ResolvedEvent, rr: dict, cat_by_id: dict, complete_synth: Completer, concepts: list[dict],
          *, known_concept_ids: set[str], drift_threshold: float, model: str, run_id: str,
          root: Path) -> tuple[int, float, dict | None]:
    """Route-result → commits. The COMMIT ORDER is the crash invariant: the takeaway version FIRST
    (`_ingest_takeaway`), the `consolidated` decision LAST — a retry before the consolidated decision
    re-does the (deterministic-id, dedup-idempotent) write; once consolidated, the event leaves the
    working set forever and is never re-enumerated.

      noop       → (0, 0.0, None): no write; the event stays un-consolidated (forget-eligible).
      new        → synthesize; a drop marks the event consolidated (no takeaway) so it is not
                   re-synthesized, returns (0, cost, None); else commit takeaway then consolidated,
                   return (1, cost, takeaway).
      strengthen → bump support; commit the new version then consolidated, return (1, cost, version).

    The returned dict is what `DreamBlock.process` folds into the on-instance catalog."""
    decision = rr["decision"]
    if decision == "noop":
        return 0, 0.0, None
    if decision == "new":
        tk, cost = synthesize_new(rv, complete_synth, concepts, known_concept_ids=known_concept_ids,
                                  model=model, run_id=run_id)
        if tk is None:
            _write_consolidated(rv.id, None, "new", run_id=run_id, root=root)   # noise: do not re-synth
            return 0, cost, None
        _ingest_takeaway(tk, model=model, run_id=run_id, root=root)             # takeaway FIRST
        _write_consolidated(rv.id, tk["id"], "new", run_id=run_id, root=root)   # consolidated LAST
        return 1, cost, tk
    # strengthen
    tk = cat_by_id.get(rr["takeaway_id"])
    if tk is None:                                 # id vanished between route + apply → fall back to new
        return apply(rv, {"decision": "new", "takeaway_id": None}, cat_by_id, complete_synth, concepts,
                     known_concept_ids=known_concept_ids, drift_threshold=drift_threshold,
                     model=model, run_id=run_id, root=root)
    nv, cost = update_support(tk, rv, complete_synth, concepts, known_concept_ids=known_concept_ids,
                              drift_threshold=drift_threshold, model=model, run_id=run_id)
    _ingest_takeaway(nv, model=model, run_id=run_id, root=root)                  # takeaway FIRST
    _write_consolidated(rv.id, nv["id"], "strengthen", run_id=run_id, root=root)  # consolidated LAST
    return 1, cost, nv


# --- the three v2 lifecycle decision blobs (folded by latest_decisions, no blobstore change) ------

def _write_decision(body: dict, *, run_id: str, root: Path | None) -> None:
    """Append one lifecycle decision blob, written exactly like `review._record`: source_id ==
    blob_hash(body), prev=None, fetched_at == body['at'] (so the audited and folded timelines agree). A
    `producer.stage == 'dream'` rides along so `forget` can count dream cycles off the stored markers."""
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s), prev=None,
                     origin_ref={"stage": "dream", "run_id": run_id}, fetched_at=body["at"], root=root)


def _write_consolidated(event_id: str, takeaway_id: str | None, decision: str, *,
                        run_id: str, root: Path | None) -> None:
    """The commit point that removes an incorporated event from the working set forever (across all
    params). `decision` is `new` | `strengthen`; `takeaway` is the minted id (None when synth dropped it
    as noise)."""
    at = config.now()
    _write_decision({"verb": "consolidated", "target": event_id, "takeaway": takeaway_id,
                     "decision": decision, "at": at, "run_id": run_id,
                     "producer": {"stage": "dream", "run_id": run_id, "at": at}}, run_id=run_id, root=root)


def _write_stale(event_id: str, reason: str, *, run_id: str, root: Path | None) -> None:
    """Forget's verdict on an aged, low-salience, never-consolidated event — also removes it from the
    working set; reversible (a fact, not a deletion)."""
    at = config.now()
    _write_decision({"verb": "stale", "target": event_id, "reason": reason, "at": at, "run_id": run_id,
                     "producer": {"stage": "dream", "run_id": run_id, "at": at}}, run_id=run_id, root=root)


def _write_merge(loser_id: str, winner_id: str, *, run_id: str, root: Path | None) -> None:
    """Sibling of review.retire: removes the loser takeaway from catalog/current_takeaways (its blob +
    history are retained; the union evidence lives on the winner)."""
    at = config.now()
    _write_decision({"verb": "merge", "target": loser_id, "into": winner_id, "at": at, "run_id": run_id,
                     "producer": {"stage": "dream", "run_id": run_id, "at": at}}, run_id=run_id, root=root)


# --- forget: conservative straggler eviction (never age alone — ADR-0010 §6) ----------------------

def forget(root: Path | None = None, *, tau: int = FORGET_TAU,
           salience_floor: float = FORGET_SALIENCE_FLOOR, run_id: str) -> list[str]:
    """Stale the stragglers: for each un-consolidated event, `cycles_resident` = the count of DISTINCT
    dream run_ids in the stored dream-stage decisions whose `at` postdates the event blob (no mutable
    per-event state). Write a `stale` decision ONLY when (cycles_resident >= tau) AND (salience < floor)
    — the CONJUNCTION protects a sparse-but-recurring lesson; never age alone. Reversible. Cheap, no LLM
    — runs in `DreamBlock.finalize`. Returns the staled event ids."""
    root = root or config.data_root()
    ws = working_set(root)
    latest = blobstore.latest_by_kind("event", root)
    dream_decs = list(blobstore.decisions_for(None, root, stage="dream"))
    staled: list[str] = []
    for rv in ws:
        born = ""
        h = latest.get(rv.id)
        if h:
            try:
                born = blobstore.get_meta(h, root).get("fetched_at", "")
            except (OSError, json.JSONDecodeError):
                born = ""
        runs = {d.get("run_id") for d in dream_decs
                if d.get("run_id") and str(d.get("at", "")) > born}
        if len(runs) >= tau and salience(rv.event) < salience_floor:
            _write_stale(rv.id, "aged + low salience", run_id=run_id, root=root)
            staled.append(rv.id)
    return staled


# --- merge: maintenance de-dup (NOT in the hot per-event loop) -------------------------------------

def merge_candidates(cat: list[dict], *, threshold: float) -> list[tuple[str, str]]:
    """A recall-first hint: lexically score catalog pairs (TF-IDF over title + why) above `threshold` and
    return (loser_id, winner_id) — the higher-support takeaway wins. The LLM/human confirms (mis-coercion,
    not recall, is the dominant failure mode while the catalog stays small)."""
    docs = [_tokens(f"{t.get('title', '')} {t.get('why', '')}") for t in cat]
    idf = _idf(docs)
    vecs = [_vec(d, idf) for d in docs]
    pairs: list[tuple[str, str]] = []
    for i in range(len(cat)):
        for j in range(i + 1, len(cat)):
            if _cos(vecs[i], vecs[j]) >= threshold:
                a, b = cat[i], cat[j]
                sa = (a.get("support") or {}).get("events", 0)
                sb = (b.get("support") or {}).get("events", 0)
                w, l = (a, b) if sa >= sb else (b, a)
                pairs.append((l["id"], w["id"]))
    return pairs


def merge(loser_id: str, winner_id: str, complete_synth: Completer | None, *, model: str, run_id: str,
          root: Path | None = None, resynth: bool = True) -> dict:
    """The v2 replacement for v1's split/merge-by-resignature. Build a WINNER version that UNIONs the
    loser's evidence (dedup by event_id), sessions_seen, markers (max), last_seen (max) and recomputes
    support; optionally re-synthesize the `why` (one Sonnet call); commit the winner version, then a
    `merge` decision dropping the loser. The loser's blob + history are retained — its already-consolidated
    events keep a now-stale takeaway pointer (harmless; the union evidence lives on the winner)."""
    root = root or config.data_root()
    by_id = {t["id"]: t for t in catalog(root)}
    winner = by_id.get(winner_id)
    loser = by_id.get(loser_id)
    if winner is None:
        raise ValueError(f"no winner takeaway {winner_id!r}")
    nv = {
        "id": winner_id,
        "title": str(winner.get("title", "")),
        "why": str(winner.get("why", "")),
        "relation": winner.get("relation") or {"kind": "new", "concept_id": None, "note": ""},
        "evidence": [dict(e) for e in (winner.get("evidence") or [])],
        "sessions_seen": list(winner.get("sessions_seen") or []),
        "markers": {k: _score((winner.get("markers") or {}).get(k)) for k in MARKER_KINDS},
        "confidence": _score(winner.get("confidence"), 0.5),
        "last_seen": str(winner.get("last_seen", "")),
    }
    seen = {e.get("event_id") for e in nv["evidence"]}
    for e in (loser.get("evidence") or []) if loser else []:
        if e.get("event_id") not in seen:
            nv["evidence"].append(dict(e))
            seen.add(e.get("event_id"))
    nv["cites"] = sorted({e.get("event_id") for e in nv["evidence"] if e.get("event_id")})
    ss = set(nv["sessions_seen"]) | set((loser.get("sessions_seen") or []) if loser else [])
    nv["sessions_seen"] = sorted(s for s in ss if s)
    lm = (loser.get("markers") or {}) if loser else {}
    nv["markers"] = {k: max(nv["markers"][k], _score(lm.get(k))) for k in MARKER_KINDS}
    nv["last_seen"] = max(nv["last_seen"], str((loser or {}).get("last_seen", "")))
    nv["support"] = {"events": len(nv["cites"]), "sessions": len(nv["sessions_seen"])}
    cost = 0.0
    if resynth and complete_synth is not None and loser is not None:
        user = ("Two takeaways describe the SAME lesson; merge them into ONE.\n"
                f"A: {winner.get('title', '')} — {winner.get('why', '')}\n"
                f"B: {loser.get('title', '')} — {loser.get('why', '')}\n"
                'Return ONLY JSON {"title": ..., "why": ...}.')
        comp = complete_synth(SYNTH_SYSTEM, user)
        cost = completer.cost_of(comp)
        parsed = completer.parse_json_object(comp.text) or {}
        why = str(parsed.get("why", "")).strip()[:WHY_MAX]
        if why:
            nv["why"] = why
            nv["title"] = str(parsed.get("title", "")).strip()[:TITLE_MAX] or nv["title"]
    nv["producer"] = {"stage": "dream", "model": model, "prompt_version": PROMPT_VERSION,
                      "run_id": run_id, "cost_usd": round(cost, 8)}
    _ingest_takeaway(nv, model=model, run_id=run_id, root=root)     # winner version FIRST
    _write_merge(loser_id, winner_id, run_id=run_id, root=root)     # the merge decision LAST
    return nv


# --- the Block: dream v2 as the sequential, stateful, per-EVENT-commit consolidator ----------------

class DreamBlock:
    """dream v2 as a `block.Block` (ADR-0009): the SEQUENTIAL, STATEFUL consolidator that commits PER
    ITEM (the inversion of v1's per-run finalize). `items()` loads the concepts + the ON-INSTANCE catalog
    once, then yields the working set; the driver sorts it by `priority` (salience) and caps by --limit.
    `process(rv)` routes + applies — committing the takeaway version + `consolidated` decision INSIDE
    process — then FOLDS the result into `self._cat`/`self._cat_by_id` IN MEMORY so the VERY NEXT event
    routes against the just-updated catalog. The driver writes the per-item `processed` marker LAST.
    `finalize()` runs the conservative `forget` pass.

    Resumability: each event's takeaway + `consolidated` decision land immediately, so a kill keeps every
    completed event; on resume `items()` recomputes the working set from the store (consolidated events
    already excluded) and reloads the catalog fresh — the in-memory catalog is only an intra-run
    optimization, always reconciled-from-store at run start, never a source of truth."""

    name = "dream"
    commits_per_item = True                        # the v2 inversion of v1's per-run finalize commit
    marker_extra = block.no_marker_extra           # no per-item audit fields beyond the uniform marker

    def __init__(self, complete_route: Completer, complete_synth: Completer, *,
                 route_model: str = ROUTE_MODEL, synth_model: str = SYNTH_MODEL,
                 min_confidence: float = 0.0, drift_threshold: float = DRIFT_THRESHOLD,
                 maturity: int = MATURITY_SESSIONS, forget: bool = True,
                 forget_tau: int = FORGET_TAU, forget_floor: float = FORGET_SALIENCE_FLOOR) -> None:
        self.complete_route = complete_route
        self.complete_synth = complete_synth
        self.route_model = route_model
        self.synth_model = synth_model
        self.min_confidence = min_confidence
        self.drift_threshold = drift_threshold
        self.maturity = maturity
        self.forget_on = forget
        self.forget_tau = forget_tau
        self.forget_floor = forget_floor
        # params is the done-key suffix: the per-item `processed` marker keys on (event_id, PV, synth_model).
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROMPT_VERSION), ("model", synth_model))
        # the on-instance catalog (loaded once in items(), folded as events consolidate within the run).
        self._concepts: list[dict] = []
        self._known_ids: set[str] = set()
        self._cat: list[dict] = []
        self._cat_by_id: dict[str, dict] = {}
        # RunReport-shaped tallies (read by the shim/CLI).
        self.takeaways: list[dict] = []
        self.n_events = 0
        self.n_new = 0
        self.n_strengthened = 0
        self.n_noop = 0
        self.staled: list[str] = []

    def items(self, root: Path, *, source_id: str | None = None):
        """Load the concepts + the ON-INSTANCE catalog ONCE, then yield the salience-ordered working set.
        `source_id` is ignored (dream is a global pass). The driver eager-lists this, sorts by `priority`,
        and caps by --limit."""
        self._concepts = load_concepts(root)
        self._known_ids = {c["id"] for c in self._concepts}      # all str (load_concepts guarantees it)
        self._cat = catalog(root)
        self._cat_by_id = {t["id"]: t for t in self._cat}
        ws = working_set(root, min_confidence=self.min_confidence)
        self.n_events = len(ws)
        return ws

    def key(self, rv: ResolvedEvent) -> str:
        return rv.id

    def priority(self, rv: ResolvedEvent) -> float:
        return salience(rv.event)

    def process(self, rv: ResolvedEvent, *, root: Path, run_id: str) -> tuple[int, float]:
        """Route the event against the on-instance catalog, apply the verdict (committing the takeaway +
        `consolidated` decision), then FOLD the result back into the catalog so the next event sees it.
        Returns (n_outputs, cost) for the driver's budget gate. A raised route/synth propagates — the
        driver isolates the event as errored (no consolidated decision → retried next run)."""
        rr, route_cost = route(rv, self._cat, self.complete_route)
        n_out, apply_cost, tk = apply(
            rv, rr, self._cat_by_id, self.complete_synth, self._concepts,
            known_concept_ids=self._known_ids, drift_threshold=self.drift_threshold,
            model=self.synth_model, run_id=run_id, root=root)
        if tk is not None:
            if rr["decision"] == "new":
                self.n_new += 1
            else:
                self.n_strengthened += 1
            self.takeaways.append(tk)
            folded = takeaway_content(tk)            # the stored projection (== what a re-load would give)
            self._cat_by_id[folded["id"]] = folded
            self._cat = list(self._cat_by_id.values())
        else:
            self.n_noop += 1                         # noop OR a synth-dropped new (no takeaway either way)
        return n_out, route_cost + apply_cost

    def finalize(self, *, root: Path, run_id: str) -> None:
        """The conservative straggler eviction (cheap, no LLM). Runs even though commits_per_item=True —
        the driver always calls finalize. Disabled via `forget=False`."""
        if self.forget_on:
            self.staled = forget(root, tau=self.forget_tau, salience_floor=self.forget_floor,
                                 run_id=run_id)


# --- run: a thin compat shim over the block driver ------------------------------------------------

class RunReport:
    """The shape `dream.run` returns — a thin WRAPPER over the uniform `block.Report` the driver
    populated plus the DreamBlock instance, exposing every field by reading THROUGH them. Nothing is
    copied at construction, so there is no hand-maintained desync surface."""
    def __init__(self, report: block.Report, blk: DreamBlock) -> None:
        self._report = report
        self._blk = blk

    @property
    def n_events(self) -> int:
        return self._blk.n_events
    @property
    def n_new(self) -> int:
        return self._blk.n_new
    @property
    def n_strengthened(self) -> int:
        return self._blk.n_strengthened
    @property
    def n_noop(self) -> int:
        return self._blk.n_noop
    @property
    def takeaways(self) -> list[dict]:
        return self._blk.takeaways
    @property
    def staled(self) -> list[str]:
        return self._blk.staled

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
    def cost_usd(self) -> float:
        return self._report.cost_usd
    @property
    def stopped_on_budget(self) -> bool:
        return self._report.stopped_on_budget


def run(complete_route: Completer, complete_synth: Completer, *, route_model: str = ROUTE_MODEL,
        synth_model: str = SYNTH_MODEL, min_confidence: float = 0.0,
        drift_threshold: float = DRIFT_THRESHOLD, maturity: int = MATURITY_SESSIONS,
        forget: bool = True, max_usd: float | None = None, limit: int | None = None,
        progress: block.Progress | None = None, root: Path | None = None) -> RunReport:
    """Consolidate the working set incrementally — a thin shim over `block.run(DreamBlock(...))` (mirrors
    v1's run/RunReport-wrapper pattern), so callers/CLI keep a stable surface. TWO separate injected
    Completers (route = Haiku, synth = Sonnet), offline-testable with fakes. `progress` defaults to None
    (silent) so a setup helper doesn't spew per-event lines."""
    blk = DreamBlock(complete_route, complete_synth, route_model=route_model, synth_model=synth_model,
                     min_confidence=min_confidence, drift_threshold=drift_threshold, maturity=maturity,
                     forget=forget)
    report = block.run(blk, max_usd=max_usd, limit=limit, root=root, progress=progress)
    return RunReport(report, blk)


# --- CLI ------------------------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="dream",
        description="Incremental working-set consolidation: route each un-consolidated event to "
                    "NEW/STRENGTHEN/NOOP and maintain evidence-cited takeaways (LLM).")
    ap.add_argument("--route-model", default=ROUTE_MODEL, help=f"router model (default: {ROUTE_MODEL})")
    ap.add_argument("--synth-model", default=SYNTH_MODEL, help=f"synthesis model (default: {SYNTH_MODEL})")
    ap.add_argument("--min-confidence", type=float, default=0.0, help="ignore events below this glean confidence")
    ap.add_argument("--drift-threshold", type=float, default=DRIFT_THRESHOLD,
                    help="lexical drift above which a strengthen re-synthesizes the why")
    ap.add_argument("--maturity", type=int, default=MATURITY_SESSIONS,
                    help="distinct-session bar a takeaway must cross to reach review")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this")
    ap.add_argument("--limit", type=int, help="cap events examined this run (the salience-ordered top)")
    ap.add_argument("--no-forget", action="store_true", help="skip the straggler-eviction (forget) pass")
    ap.add_argument("--merge", action="store_true", help="run the maintenance de-dup pass (list near-dup pairs)")
    ap.add_argument("--merge-threshold", type=float, default=0.5, help="lexical similarity for merge candidates")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the salience-ordered working set + catalog sizes; no LLM calls")
    ap.add_argument("--show", action="store_true", help="print each new/strengthened takeaway")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-event progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    args = ap.parse_args(argv)

    if args.merge:                                  # maintenance de-dup: list the candidate pairs
        root = config.ensure_layout()
        cat = catalog(root)
        pairs = merge_candidates(cat, threshold=args.merge_threshold)
        by_id = {t["id"]: t for t in cat}
        print(f"{len(pairs)} merge candidate(s) over {len(cat)} takeaways (threshold {args.merge_threshold}):")
        for loser, winner in pairs:
            print(f"  {loser[:12]} → {winner[:12]}  "
                  f"({by_id.get(loser, {}).get('title', '')!r} into {by_id.get(winner, {}).get('title', '')!r})")
        return

    if args.dry_run:                                # eyeball the queue + catalog before spending
        root = config.ensure_layout()
        ws = working_set(root, min_confidence=args.min_confidence)
        ws.sort(key=lambda rv: salience(rv.event), reverse=True)
        cat = catalog(root)
        mature = current_takeaways(root, min_sessions=args.maturity)
        print(f"{len(ws)} un-consolidated events (salience-ordered) · catalog {len(cat)} takeaways "
              f"({len(mature)} mature):")
        for rv in ws[:40]:
            sample = rv.quote.strip().replace("\n", " ")
            print(f"  [{salience(rv.event):.3f}] {rv.id[:12]}  {sample[:72]!r}")
        return

    complete_route = completer.make_cli_completer(args.route_model)
    complete_synth = completer.make_cli_completer(args.synth_model)
    progress = None if args.quiet else block.Progress(
        "dream", cap=args.max_usd, params={"prompt_version": PROMPT_VERSION, "model": args.synth_model},
        out_noun=OUT_NOUN, verbose=args.verbose)
    report = run(complete_route, complete_synth, route_model=args.route_model, synth_model=args.synth_model,
                 min_confidence=args.min_confidence, drift_threshold=args.drift_threshold,
                 maturity=args.maturity, forget=not args.no_forget, max_usd=args.max_usd,
                 limit=args.limit, progress=progress)
    if args.show:
        for t in report.takeaways:
            sup, rel = t["support"], t["relation"]["kind"]
            print(f"\n  • {t['title']}  [{rel}, {sup['events']}ev/{sup['sessions']}sess, "
                  f"conf {t['confidence']:.2f}]")
            print(f"    {t['why']}")
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\ndream-{report.run_id}: {report.n_events} events → {report.n_new} new, "
          f"{report.n_strengthened} strengthened, {report.n_noop} noop, {report.skipped} skipped, "
          f"{len(report.staled)} staled{errs}, ${report.cost_usd:.4f}{tail}")


if __name__ == "__main__":
    main()
