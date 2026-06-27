"""dream — the synthesis stage: cluster glean's fleeting events and distill each cluster into one
durable, evidence-cited **takeaway** (ADR-0006).

    … chunk → chunkset → glean → events → dream → takeaways → [human review] → concepts → generate
                                            (synthesis, LLM, rare + bigger model)

glean runs cheap and per-chunk (Haiku); dream is its opposite — INFREQUENT, over the whole event
pile, with a SHARPER model (sleep-time compute: spend here, it's rare and amortized). It does two
things a per-chunk pass cannot: it sees events *together* (so it can find the recurring "why" behind
many observations), and it re-reads the TRUSTED verbatim quote rather than leaning on glean's
untrusted one-line summary. The summary was only ever triage; the quote is the ground truth.

Two stages, by the same split that governs the rest of ratchet — deterministic work is cheap and
reproducible, the LLM call is not:
  1. CLUSTER (deterministic, stdlib, no LLM): group events by lexical similarity over their trusted
     quotes. Cheap enough to recompute every run, so it is NOT a stored blob — the blobstore models
     single-parent lineage and a clustering is a fan-in over many events. The partition is recorded
     where it is actually useful: each cluster's member ids land in its `processed` decision blob, and
     each takeaway cites its events. Auditable and re-derivable without a new artifact.
  2. SYNTHESIZE (LLM, one call per cluster): write the cluster's "why" + a name, judge how it relates
     to already-known CONCEPTS, and cite the events it relied on. The trust chain extends one level:
     takeaway → cited event ids → each event's verified byte span → immutable cleaned blob.

Takeaways ARE blobs now (ADR-0007): a logical takeaway is a RAW blob whose `source_id` is the
deterministic `cluster_signature` (the hash of its sorted member event ids), and each synthesis is an
immutable VERSION (content = `blob_hash(canonical-json(record))`). Identical membership => the same
source_id => a re-synthesis is a new version under it, `prev`-linked, latest wins; a membership change
=> a new source_id => a distinct logical takeaway. The append-only `events/dream-*.jsonl` stream and
the `state/` processed ledger retire: "already done" is a `processed` decision blob (verb='processed',
target=cluster_signature, keyed on (stage, prompt_version, model)), and `current_takeaways` derives
from the blobstore TimeMap, not a log-fold.

EVOLUTION is first-class (sulin): names, summaries, and groupings change over time — a cluster may
need to split or merge as more evidence arrives. We never mutate a takeaway in place (that would
orphan its evidence spans and lose history). Instead a re-run re-clusters globally and a new takeaway
**supersedes** the current takeaways it replaces: grow, split (one→many), and merge (many→one) are
all the same mechanism. Supersession is COVERAGE-CONDITIONED — a prior takeaway is folded out only
when ALL its events are re-covered by takeaways committed in the same run, so a dropped or errored
split-child can never orphan its parent's events (`run`). `current_takeaways` folds the supersede
links to "now"; nothing is edited, only appended.

Idempotency mirrors glean: a `processed` decision is keyed by (cluster_signature, prompt_version,
model). An unchanged cluster is skipped; a changed one (new signature) re-synthesizes. Bump
PROMPT_VERSION or the model to re-synthesize the same groupings with a sharper prompt.

The LLM call is injected as a `Completer`; everything else (clustering, parsing, citation
verification, supersede linking, the fold) is pure and tested offline with a fake.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import blobstore, completer, config
from .completer import Completer
from .glean import MARKER_KINDS          # the marker vocabulary is glean's; dream carries it upward

PROMPT_VERSION = "dream/1"               # bump to re-synthesize the same clusters with a sharper prompt
DREAM_MODEL = "sonnet"                    # dream is rare → afford a sharper model than glean's haiku
SIM_THRESHOLD = 0.34                      # cosine to a cluster centroid to JOIN it; high → under-merge
TITLE_MAX = 80
WHY_MAX = 280
NOTE_MAX = 160

_RELATION_KINDS = ("new", "strengthens", "refines", "contradicts")

SYSTEM_PROMPT = (
    "You synthesize a CLUSTER of related raw observations from a developer's Claude Code sessions "
    "into ONE durable, reusable takeaway: the single underlying 'why' a future session would benefit "
    "from knowing.\n\n"
    "Each observation has a VERBATIM quote (the ground truth — trust it over everything else) and a "
    "one-line machine summary (a hint that may be imprecise or wrong). Read the quotes. State the "
    "principle they share and WHY it holds — do not just restate one observation.\n\n"
    "You are also given the developer's already-known CONCEPTS. Judge how this takeaway relates to "
    "them: new (nothing covers it), strengthens (more evidence for one), refines (narrows/extends "
    "one), or contradicts (overturns one — the most important to surface).\n\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{\n'
    '  "title": a short noun phrase naming the takeaway (<= 80 chars),\n'
    '  "why": one or two sentences — the durable principle and why it holds (<= 280 chars),\n'
    '  "relation": {"kind": "new"|"strengthens"|"refines"|"contradicts", "concept_id": the related '
    'concept id or null, "note": a brief reason (<= 160 chars)},\n'
    '  "cites": the ids of the observations whose quotes actually support this (a subset of the ids '
    'given to you),\n'
    '  "confidence": 0-1, how durable and reusable this is,\n'
    '  "drop": true if these observations are noise, not a durable learning (then everything else is '
    'ignored)\n'
    '}\n'
    "If the cluster mixes unrelated things, cite only the coherent subset. Cite at least one "
    "observation unless drop is true."
)

# Lexical-clustering stopwords: drop the connective words so similarity keys on the technical content
# (tool names, paths, error strings). Deliberately small — domain terms like "git"/"jj" must survive.
_STOPWORDS = frozenset(
    "the a an and or but if then else when of to in on at by for with from into over as is are was "
    "were be been being it its this that these those you your he she they we i me my our their them "
    "do does did done can could should would will shall may might must not no yes so than too very "
    "use used using user about which who what why how out up down off here there".split())


# --- the concept seam: dream reads the curated-knowledge layer the review gate writes ----------

def load_concepts(root: Path | None = None) -> list[dict]:
    """The current concept set — the human-reviewed source of truth dream judges belief-change
    against (ADR-0006). Empty until the review stage exists, so every takeaway is `new` for now; the
    seam is wired so the loop closes with zero re-architecture once concepts land. A concept file is
    a JSON object `{id, title, statement, ...}`; malformed files are skipped, never fatal."""
    root = root or config.data_root()
    d = root / "concepts"
    out: list[dict] = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str) and obj["id"]:
            out.append(obj)           # a non-string id is "malformed" too (it must be set-hashable downstream)
    return out


def _render_concepts(concepts: list[dict]) -> str:
    if not concepts:
        return "(none known yet — treat every takeaway as new)"
    return "\n".join(f"- id {c['id']}: {str(c.get('title', '')).strip()} — "
                     f"{str(c.get('statement', '')).strip()[:200]}" for c in concepts)


# --- gather: load events and resolve each to its TRUSTED quote (the untrusted summary is a hint) --

@dataclass
class ResolvedEvent:
    event: dict
    quote: str               # the verbatim evidence, resolved + re-validated from the cleaned blob
    span: tuple[int, int]    # the VALIDATED [start, end) byte span the quote came from (emitted as evidence)
    session_id: str | None   # the originating session (for support weighting by DISTINCT sessions)

    @property
    def id(self) -> str:
        return self.event["id"]


def _session_of(cleaned_hash: str, root: Path, cache: dict) -> str | None:
    """The originating session id for an event, via content-addressed lineage: cleaned blob →
    `derived_from` (raw blob) → its `source_id`. Cached per cleaned blob. Absent meta → unknown."""
    if cleaned_hash in cache:
        return cache[cleaned_hash]
    sid = None
    try:
        raw = blobstore.get_meta(cleaned_hash, root).get("derived_from")
        if raw:
            sid = blobstore.get_meta(raw, root).get("source_id")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        sid = None
    cache[cleaned_hash] = sid
    return sid


def gather_events(root: Path, *, min_confidence: float = 0.0) -> list[ResolvedEvent]:
    """The current glean event of every source (latest version each), **re-anchored** to its trusted
    quote. dream must not trust the recorded span: glean's substring anchor runs at the WRITE
    boundary, but an event is now a blob version whose content is just model output + a span (a buggy
    producer or an out-of-band write can plant a malformed span), and Python slicing silently accepts
    `None` (the whole blob) and clamps overshoot. So the span is accepted here only after it validates
    as in-bounds ints `0 <= start < end <= len(blob)` and the bytes decode — re-establishing "the
    quote is real bytes of an immutable blob" at dream's READ boundary. An event whose blob is gone
    (TTL-reclaimed) or whose span fails is dropped. The quote, not the summary, is what dream clusters
    and reasons over."""
    by_id: dict[str, dict] = {}
    for sid, h in blobstore.latest_by_kind("event", root).items():
        try:
            e = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(e, dict):
            e.setdefault("id", sid)   # source_id (event_id) is authoritative; mirror it in case content dropped it
            by_id[sid] = e            # latest_by_kind already folds each event_id to its newest VERSION —
                                      # only the untrusted summary can differ across re-extractions, the
                                      # span-derived quote is invariant, so clustering is unaffected
    resolved: list[ResolvedEvent] = []
    blobs: dict[str, bytes] = {}
    sessions: dict[str, str | None] = {}
    for e in by_id.values():
        if _score(e.get("confidence"), 0.0) < min_confidence:
            continue
        ch = e.get("cleaned_hash")
        ev = e.get("evidence") or []
        sp = ev[0] if ev and isinstance(ev[0], dict) else None
        if not ch or sp is None:
            continue
        bs, be = sp.get("byte_start"), sp.get("byte_end")
        if not (isinstance(bs, int) and isinstance(be, int) and 0 <= bs < be):
            continue                  # reject None / negative / inverted before slicing clamps it
        try:
            data = blobs.get(ch)
            if data is None:
                data = blobstore.get(ch, root).encode("utf-8")
                blobs[ch] = data
            if be > len(data):        # overshoot would be silently clamped — reject explicitly
                continue
            quote = data[bs:be].decode("utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        if not quote.strip():
            continue
        resolved.append(ResolvedEvent(event=e, quote=quote, span=(bs, be),
                                      session_id=_session_of(ch, root, sessions)))
    return resolved


# --- cluster: deterministic, stdlib TF-IDF + leader clustering (no LLM, recomputed every run) ----

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


def cluster(events: list[ResolvedEvent], *, threshold: float = SIM_THRESHOLD) -> list[list[ResolvedEvent]]:
    """Leader clustering (Hartigan) over TF-IDF(quote + summary) vectors: walk events in a fixed
    order, join each to the best existing cluster, else start a new one. A high threshold biases
    toward UNDER-merging — a false merge destroys a learning, a false split only costs the reviewer a
    second look. Two prior-art-grounded hardenings (ADR-0006) of the naive single-pass leader:

      ORDER (Hartigan / CRAN leaderCluster): leader is input-order dependent, and seeding from the
      most DISTINCTIVE points first gives better, more stable partitions. So events are ordered by
      descending pre-normalized TF-IDF mass (content-rich events seed clusters), id breaking ties so
      the order — and thus every cluster signature — stays reproducible.

      SEED CAP (IR-book complete-link; Swarm): an event joins only if it is similar to BOTH the
      drifting centroid (cohesion) AND the cluster's fixed SEED (a diameter cap from the origin). The
      seed cap is what stops centroid-drift CHAINING — a leader otherwise absorbs a string of
      pairwise-near but globally-distant events into one over-broad takeaway."""
    docs = [_tokens(rv.quote + " " + str(rv.event.get("summary", ""))) for rv in events]
    idf = _idf(docs)
    vecs = [_vec(d, idf) for d in docs]
    mass = [sum(tf * idf.get(t, 0.0) for t, tf in Counter(d).items()) for d in docs]
    seeds: list[dict[str, float]] = []         # each cluster's first (seeding) vector — fixed, no drift
    centroids: list[dict[str, float]] = []     # running L2-normalized mean per cluster
    sums: list[dict[str, float]] = []          # unnormalized sum (so the mean updates incrementally)
    members: list[list[int]] = []
    for i in sorted(range(len(events)), key=lambda j: (-mass[j], events[j].id)):
        best, best_sim = -1, threshold
        for c in range(len(centroids)):
            cs = _cos(vecs[i], centroids[c])
            if cs >= best_sim and _cos(vecs[i], seeds[c]) >= threshold:
                best, best_sim = c, cs
        if best < 0:
            seeds.append(vecs[i])
            centroids.append(dict(vecs[i]))
            sums.append(dict(vecs[i]))
            members.append([i])
        else:
            s = sums[best]
            for t, w in vecs[i].items():
                s[t] = s.get(t, 0.0) + w
            norm = math.sqrt(sum(w * w for w in s.values())) or 1.0
            centroids[best] = {t: w / norm for t, w in s.items()}
            members[best].append(i)
    return [[events[i] for i in grp] for grp in members]


def cluster_signature(cl: list[ResolvedEvent]) -> str:
    """A stable id for a cluster: the hash of its sorted member event ids. Identical membership →
    identical signature (idempotency); any membership change → a new signature (re-synthesis)."""
    return hashlib.sha256("|".join(sorted(rv.id for rv in cl)).encode()).hexdigest()[:16]


# --- synthesize: one LLM call per cluster → a verified, evidence-cited takeaway -------------------

_score = completer.clean_score   # shared untrusted-score hygiene (clamp + scrub NaN/inf)


def _clean_relation(v, known_ids: set[str]) -> dict:
    """Coerce the untrusted relation: an unknown kind → `new`, and a `concept_id` that isn't a real
    known concept → null (which forces `new` — you cannot strengthen/refine/contradict a concept that
    does not exist, so today, with no concepts, every relation is `new`)."""
    v = v if isinstance(v, dict) else {}
    kind = v.get("kind") if v.get("kind") in _RELATION_KINDS else "new"
    cid = v.get("concept_id")
    cid = cid if isinstance(cid, str) and cid in known_ids else None
    if cid is None and kind != "new":
        kind = "new"
    return {"kind": kind, "concept_id": cid, "note": str(v.get("note", "")).strip()[:NOTE_MAX]}


def build_takeaway(parsed: dict, cl: list[ResolvedEvent], signature: str, *,
                   known_concept_ids: set[str], model: str, run_id: str) -> dict | None:
    """Assemble a takeaway from a synthesis response — and VERIFY it. A citation is kept only if it
    names a real event IN THIS CLUSTER (the model can cite nothing it was not given); a takeaway with
    no surviving citation has no evidence and is dropped. `member_events` records the full
    deterministic partition (for supersede linking); `cites` is the model's evidence subset; each
    `evidence` pointer carries the event's REVALIDATED span (`rv.span`, from gather) — the same bytes
    that produced the trusted quote, never a re-read of the untrusted JSON. `supersedes` is filled by
    `run` after the whole run's coverage is known (it is empty until then)."""
    in_cluster = {rv.id: rv for rv in cl}
    seen: set[str] = set()
    cited_ids: list[str] = []
    for c in parsed.get("cites") or []:
        if isinstance(c, str) and c in in_cluster and c not in seen:
            seen.add(c)
            cited_ids.append(c)
    if not cited_ids:
        return None
    markers = {k: 0.0 for k in MARKER_KINDS}
    evidence = []
    for cid in cited_ids:
        rv = in_cluster[cid]
        evidence.append({"event_id": cid, "cleaned_hash": rv.event.get("cleaned_hash"),
                         "byte_start": rv.span[0], "byte_end": rv.span[1]})
        em = rv.event.get("markers") or {}
        for k in MARKER_KINDS:
            markers[k] = max(markers[k], _score(em.get(k)))   # a cluster is surprising if any member is
    sessions = {in_cluster[c].session_id for c in cited_ids if in_cluster[c].session_id}
    return {
        "id": signature,
        "title": str(parsed.get("title", "")).strip()[:TITLE_MAX],
        "why": str(parsed.get("why", "")).strip()[:WHY_MAX],
        "relation": _clean_relation(parsed.get("relation"), known_concept_ids),
        "member_events": sorted(rv.id for rv in cl),   # the full partition (supersede linking keys on this)
        "cites": cited_ids,                       # the evidence subset the synthesis actually used
        "evidence": evidence,
        "support": {"events": len(cited_ids), "sessions": len(sessions)},
        "markers": markers,
        "confidence": _score(parsed.get("confidence"), 0.5),
        "cluster_signature": signature,
        "supersedes": [],                         # filled by run() once the run's coverage is known
        "producer": {"stage": "dream", "model": model, "prompt_version": PROMPT_VERSION,
                     "run_id": run_id, "cost_usd": None},   # in-memory only; mirrored to origin_ref, not stored in content
    }


def synthesize_cluster(cl: list[ResolvedEvent], complete: Completer, concepts: list[dict], *,
                       known_concept_ids: set[str], model: str, run_id: str) -> tuple[dict | None, float]:
    """One cluster → (takeaway or None, cost). None means the model judged the cluster noise (`drop`)
    or cited nothing verifiable — a successful adjudication, not an error (the caller marks it done so
    noise is not re-synthesized every run). Raises only if the injected completer itself fails; the
    caller isolates that and retries the cluster next run."""
    comp = complete(SYSTEM_PROMPT, _user_prompt(cl, concepts))
    cost = completer.cost_of(comp)
    parsed = completer.parse_json_object(comp.text) or {}
    if not parsed or parsed.get("drop"):
        return None, cost
    tk = build_takeaway(parsed, cl, cluster_signature(cl), known_concept_ids=known_concept_ids,
                        model=model, run_id=run_id)
    if tk is not None:
        tk["producer"]["cost_usd"] = round(cost, 8)
    return tk, cost


def _user_prompt(cl: list[ResolvedEvent], concepts: list[dict]) -> str:
    obs = "\n".join(f'- id {rv.id}: summary: {str(rv.event.get("summary", "")).strip()!r}\n'
                    f'  quote: """{rv.quote}"""' for rv in cl)
    return f"Known concepts:\n{_render_concepts(concepts)}\n\nObservations to synthesize:\n{obs}"


# --- the takeaway store + the derived "current" view (fold the supersede links) ------------------

def takeaway_content(tk: dict) -> dict:
    """The STORED content of a takeaway version — model output + intrinsic provenance ONLY (ADR-0007
    blob_shape). `producer` (model/run_id/cost_usd, all run-varying) is DROPPED here and moves to
    meta.origin_ref; `status` is gone (state is a decision). This projection is what makes "a re-
    synthesis with unchanged output is a no-op" hold: the canonical-json of this view re-hashes
    identically run-to-run, so only changed content (title/why/cites/supersedes/…) forks a new version.
    `id`/`cluster_signature` stay as convenience mirrors (== source_id, deterministic, hash-stable).
    `supersedes` is INTRINSIC (computed by run() from this run's coverage — ADR-0007 §1)."""
    return {k: tk[k] for k in (
        "id", "title", "why", "relation", "member_events", "cites", "evidence",
        "support", "markers", "confidence", "cluster_signature", "supersedes")}


def _ingest_takeaway(tk: dict, *, model: str, run_id: str, root: Path | None) -> None:
    """Freeze one takeaway as a RAW blob VERSION keyed on its `cluster_signature` (ADR-0007). The
    content is `takeaway_content(tk)` (run-invariant => an unchanged re-synthesis no-ops); `producer`
    is mirrored into `origin_ref` so provenance is answerable from meta alone."""
    blobstore.ingest(
        blobstore.canonical_json(takeaway_content(tk)), source_kind="takeaway",
        source_id=tk["cluster_signature"],
        origin_ref={"stage": "dream", "model": model, "prompt_version": PROMPT_VERSION,
                    "run_id": run_id, "cost_usd": tk["producer"].get("cost_usd")},
        root=root)


def _write_processed(sig: str, *, model: str, run_id: str, event_ids: list[str], dropped: bool,
                     cost_usd: float, root: Path | None) -> None:
    """Write the `processed` decision blob — dream's producer 'done' marker (ADR-0007 §3/§5), the
    commit that folds the old idempotency ledger into the blob model. target = the cluster_signature
    (the stable per-cluster input id — pinned; do NOT fork a separate event-set hash); the
    (stage, prompt_version, model) key lives at the top level so `processed_index` rebuilds the same
    (cluster_signature, prompt_version, model) tuple set. The body is UNIQUE per logical fact
    (target+verb+stage+prompt+model+run_id+at) so blob_hash never conflates two distinct decisions;
    source_id == its own content_hash, prev=None (decisions are never re-versioned). The per-cluster
    audit (event_ids/dropped/cost) stays for forensics."""
    at = config.now()
    body = {
        "verb": "processed", "target": sig,
        "stage": "dream", "prompt_version": PROMPT_VERSION, "model": model,
        "run_id": run_id, "at": at,
        "producer": {"stage": "dream", "model": model, "run_id": run_id, "at": at},
        "event_ids": event_ids, "dropped": dropped, "cost_usd": round(cost_usd, 8),
    }
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s), prev=None,
                     origin_ref={"stage": "dream", "run_id": run_id}, fetched_at=at, root=root)


def load_takeaways(root: Path | None = None) -> list[dict]:
    """Every committed takeaway VERSION across all sources (ADR-0007). Raw — every snapshot, not just
    the latest; `current_takeaways` folds re-synthesis (latest per source) and supersession. Used where
    a full history is wanted; the "now" view needs only `latest_by_kind`."""
    out: list[dict] = []
    for m in blobstore.iter_meta(root):
        if m.get("kind", "raw") != "raw" or m.get("source_kind") != "takeaway":
            continue
        ch = m.get("content_hash")
        if not ch:
            continue
        try:
            out.append(json.loads(blobstore.get(ch, root)))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def processed_index(root: Path | None = None) -> set[tuple[str, str, str]]:
    """The done-set keyed by (cluster_signature, prompt_version, model) — DERIVED from `processed`
    decision blobs, no stored ledger (ADR-0007 §3/§5). One scan over dream's processed decisions; the
    tuple shape is preserved verbatim so `run`'s `key in done` logic is unchanged. A re-run skips a
    cluster whose membership is unchanged; a changed cluster (new signature) re-synthesizes; bumping
    PROMPT_VERSION or model changes the key."""
    return {(b["target"], b["prompt_version"], b["model"])
            for b in blobstore.decisions_for(None, root, verb="processed", stage="dream")
            if b.get("target") and "prompt_version" in b and "model" in b}


def current_takeaways(root: Path | None = None) -> list[dict]:
    """The live takeaway set, derived from the blobstore TimeMap: take the LATEST version of every
    takeaway source (`latest_by_kind('takeaway')` — the prev-chain IS the per-source recency fold, so a
    re-synthesis or a prompt/model bump replaces its prior self by construction), then drop every
    takeaway that some surviving takeaway supersedes. This is how groupings/names/summaries evolve
    without editing anything in place. Recency is the TimeMap's (fetched_at, content_hash) — true
    lineage, not glob order — so the fold can't surface a stale record."""
    by_id: dict[str, dict] = {}
    for sid, h in blobstore.latest_by_kind("takeaway", root).items():
        try:
            t = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        tid = t.get("id") or sid      # id mirrors cluster_signature == source_id; fall back to meta
        by_id[tid] = t
    superseded: set[str] = set()
    for t in by_id.values():
        for s in t.get("supersedes") or []:
            superseded.add(s)
    return [t for tid, t in by_id.items() if tid not in superseded]


# --- run: cluster the whole event pile, synthesize the changed clusters --------------------------

@dataclass
class RunReport:
    run_id: str
    n_events: int = 0
    n_clusters: int = 0
    takeaways: list[dict] = field(default_factory=list)
    skipped: int = 0           # clusters already synthesized for (prompt_version, model)
    dropped: int = 0           # clusters the model judged noise (no takeaway, but marked done)
    errored: int = 0           # clusters whose LLM call failed (isolated, retried next run)
    cost_usd: float = 0.0
    stopped_on_budget: bool = False
    below_threshold: bool = False   # fewer than min_events to dream over → did nothing
    min_events: int = 0


def run(complete: Completer, *, model: str = DREAM_MODEL, threshold: float = SIM_THRESHOLD,
        min_confidence: float = 0.0, min_events: int = 0, max_usd: float | None = None,
        root: Path | None = None) -> RunReport:
    """Synthesize takeaways over the whole event pile, idempotently (ADR-0006). Clusters globally
    (cheap, deterministic), then for each cluster not already done for this (prompt_version, model):
    one LLM call. Supersession is **coverage-conditioned**: a prior current takeaway is folded out
    only when ALL its events are re-covered by takeaways COMMITTED this run — so a dropped/errored
    split-child can never orphan its parent's events — and a new takeaway supersedes EVERY eligible
    prior it shares an event with. That needs the whole run's coverage, so synthesis completes before
    any commit (a crash mid-run re-does the run — idempotent — trading crash-cost for correctness).

    Crash-safety mirrors glean: each takeaway blob durable first (content then meta, crash-safe on its
    own), the processed DECISION last; a cluster whose call FAILED writes no decision (retried next
    run); a cluster the model dropped IS marked done (noise not re-synthesized). A crash before the
    decision re-processes; the re-synthesis re-appears as a new VERSION of the same cluster_signature
    (a no-op if its bytes are unchanged, else latest wins), so the duplicate is absorbed (ADR-0007
    §5)."""
    root = config.ensure_layout(root)
    rid = config.run_id()
    done = processed_index(root)
    concepts = load_concepts(root)
    known_ids = {c["id"] for c in concepts}        # all str (load_concepts guarantees it) → set-safe
    events = gather_events(root, min_confidence=min_confidence)
    report = RunReport(run_id=rid, n_events=len(events), min_events=min_events)
    if len(events) < min_events:                   # a floor below which dreaming is pointless (not a
        report.below_threshold = True              # change-detector — the ledger skip handles unchanged work)
        return report

    clusters = cluster(events, threshold=threshold)
    report.n_clusters = len(clusters)

    # 1. synthesize every not-yet-done cluster. No supersede edges yet — those need the run's full
    #    coverage (step 2). Collect (cluster, signature, takeaway-or-None, cost) for what we processed.
    processed: list[tuple[list[ResolvedEvent], str, dict | None, float]] = []
    for cl in clusters:
        sig = cluster_signature(cl)
        if (sig, PROMPT_VERSION, model) in done:
            report.skipped += 1
            continue
        if max_usd is not None and report.cost_usd >= max_usd:
            report.stopped_on_budget = True
            break
        try:
            tk, cost = synthesize_cluster(cl, complete, concepts, known_concept_ids=known_ids,
                                          model=model, run_id=rid)
        except Exception:
            report.errored += 1                    # the injected seam is untrusted; isolate ANY failure
            continue
        report.cost_usd += cost
        processed.append((cl, sig, tk, cost))

    # 2. coverage-conditioned supersession. A prior current takeaway is eligible to be folded out only
    #    if ALL its events landed in a takeaway COMMITTED this run; each committed takeaway then
    #    supersedes every eligible prior it shares an event with (`- {tk id}` guards a same-id re-synth).
    emitted = [tk for _, _, tk, _ in processed if tk is not None]
    coverage = set().union(*(set(tk["member_events"]) for tk in emitted)) if emitted else set()
    eligible = {p["id"]: set(p.get("member_events") or [])
                for p in current_takeaways(root)}
    eligible = {pid: pev for pid, pev in eligible.items() if pev and pev <= coverage}
    for tk in emitted:
        ev = set(tk["member_events"])
        tk["supersedes"] = sorted({pid for pid, pev in eligible.items() if pev & ev} - {tk["id"]})

    # 3. commit: each takeaway blob durable first (content then meta), then its cluster's processed
    #    decision last — the commit ordering that makes a crash re-process, never leave a false 'done'
    #    (mirrors glean). A re-synthesis re-appears as a new VERSION of the same cluster_signature.
    for cl, sig, tk, cost in processed:
        if tk is not None:
            _ingest_takeaway(tk, model=model, run_id=rid, root=root)   # durable first ...
            report.takeaways.append(tk)
        else:
            report.dropped += 1
        _write_processed(sig, model=model, run_id=rid, event_ids=sorted(rv.id for rv in cl),
                         dropped=tk is None, cost_usd=cost, root=root)   # ... then the commit marker
    return report


# --- CLI ------------------------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="dream", description="Cluster glean events and synthesize evidence-cited takeaways (LLM).")
    ap.add_argument("--model", default=DREAM_MODEL, help=f"claude model (default: {DREAM_MODEL})")
    ap.add_argument("--threshold", type=float, default=SIM_THRESHOLD,
                    help=f"cluster join similarity 0-1, higher = smaller clusters (default {SIM_THRESHOLD})")
    ap.add_argument("--min-confidence", type=float, default=0.0,
                    help="ignore events below this glean confidence")
    ap.add_argument("--min-events", type=int, default=0,
                    help="do nothing unless at least this many events exist (trigger on accumulation)")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this")
    ap.add_argument("--dry-run", action="store_true",
                    help="cluster only — print the groupings and a sample quote each; no LLM calls")
    ap.add_argument("--show", action="store_true", help="print each synthesized takeaway")
    args = ap.parse_args(argv)

    if args.dry_run:                               # eyeball the deterministic grouping before spending
        root = config.ensure_layout()
        events = gather_events(root, min_confidence=args.min_confidence)
        clusters = cluster(events, threshold=args.threshold)
        print(f"{len(events)} events → {len(clusters)} clusters (threshold {args.threshold}):")
        for cl in sorted(clusters, key=len, reverse=True):
            sample = cl[0].quote.strip().replace("\n", " ")
            print(f"  [{len(cl):>2}] {cluster_signature(cl)[:10]}  e.g. {sample[:72]!r}")
        return

    complete = completer.make_cli_completer(args.model)
    report = run(complete, model=args.model, threshold=args.threshold,
                 min_confidence=args.min_confidence, min_events=args.min_events, max_usd=args.max_usd)
    if report.below_threshold:
        print(f"dream-{report.run_id}: {report.n_events} events < min-events {report.min_events}, skipped")
        return
    if args.show:
        for t in report.takeaways:
            rel = t["relation"]["kind"]
            print(f"\n  • {t['title']}  [{rel}, {t['support']['events']}ev/"
                  f"{t['support']['sessions']}sess, conf {t['confidence']:.2f}]")
            print(f"    {t['why']}")
            if t["supersedes"]:
                print(f"    supersedes {', '.join(s[:10] for s in t['supersedes'])}")
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\ndream-{report.run_id}: {report.n_events} events → {report.n_clusters} clusters, "
          f"{len(report.takeaways)} takeaways, {report.dropped} dropped, {report.skipped} skipped"
          f"{errs}, ${report.cost_usd:.4f}{tail}")


if __name__ == "__main__":
    main()
