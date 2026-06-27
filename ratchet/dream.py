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

dream is a `block.Block` (ADR-0009) — but THE one block that commits per RUN, not per item. Its
`items()` runs the global deterministic prelude (gather + cluster) and yields one cluster per item;
`process()` synthesizes a cluster (one LLM call) but RECORDS it instead of committing — because the
coverage-conditioned supersession needs the whole run's emitted takeaways before any `supersedes` is
known. So `commits_per_item=False` and `finalize()` does every takeaway-blob + marker commit after
the run's coverage is settled. The done-set / commit ordering is `block.done_index` /
`block.write_processed`, generalized from this stage's old per-cluster ledger; the observable run
contract (cluster → synthesize → coverage-supersede → commit, all per-run) is UNCHANGED. `dream.run`
survives as a thin shim returning the old `RunReport` shape so callers stay untouched.

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
from dataclasses import dataclass
from pathlib import Path

from . import blobstore, block, completer, config
from .completer import Completer
from .glean import MARKER_KINDS          # the marker vocabulary is glean's; dream carries it upward

PROMPT_VERSION = "dream/1"               # bump to re-synthesize the same clusters with a sharper prompt
DREAM_MODEL = "sonnet"                    # dream is rare → afford a sharper model than glean's haiku
OUT_NOUN = "takeaways"                    # the per-cluster output noun the Progress bar/line shows
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
    """The current VALID concept set — the human-reviewed source of truth dream judges belief-change
    against (ADR-0006/0007). A concept is a versioned blob the `review` stage ingests; "valid" is
    derived, not stored: the latest version of each concept source, minus any whose latest decision is
    `retire`. This is the loop closing — review's accepts become the concepts dream reads next run.
    Empty until review runs, so every takeaway is `new`. Malformed/absent → skipped, never fatal."""
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
        try:
            data = blobs.get(ch)
            if data is None:
                data = blobstore.get(ch, root).encode("utf-8")
                blobs[ch] = data
        except (FileNotFoundError, OSError):
            continue
        span = blobstore.validate_span(data, sp.get("byte_start"), sp.get("byte_end"))   # the read-side anchor
        if span is None:
            continue
        try:
            quote = data[span[0]:span[1]].decode("utf-8")
        except UnicodeDecodeError:    # a span splitting a multibyte char is not a real quote
            continue
        if not quote.strip():
            continue
        resolved.append(ResolvedEvent(event=e, quote=quote, span=span,
                                      session_id=blobstore.session_of(ch, root, sessions)))
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


def processed_index(root: Path | None = None) -> set[tuple]:
    """The done-set keyed by (cluster_signature, prompt_version, model) — now `block.done_index`
    (ADR-0009). dream's params are `((prompt_version, PV), (model, model))`, so the generic
    `(target, *param-values)` key IS `(cluster_signature, prompt_version, model)` — the tuple shape is
    preserved verbatim, so callers that read `dream.processed_index()` are unchanged. A re-run skips a
    cluster whose membership is unchanged; a changed cluster (new signature) re-synthesizes; bumping
    PROMPT_VERSION or model flips the key."""
    return block.done_index("dream", config.ensure_layout(root))


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


# --- the Block: dream as the per-RUN-commit stage (ADR-0009's one finalize exception) ------------

@dataclass
class _Pending:
    """One synthesized-but-not-yet-committed cluster, recorded by `process` for `finalize`. dream is
    the lone `commits_per_item=False` block: synthesis streams (per cluster) but commit waits for the
    whole run's coverage. `cl` is kept so the marker's `event_ids` audit can list the cluster's full
    membership; `tk` is None when the model dropped/cited-nothing (the cluster is still marked done)."""
    cl: list[ResolvedEvent]
    sig: str
    tk: dict | None
    cost: float


class DreamBlock:
    """dream as a `block.Block` — the synthesis stage, and the ONE block that commits per RUN, not per
    item (ADR-0009). The coverage-conditioned supersession needs the whole run's emitted takeaways
    before any `supersedes` is known, so `commits_per_item=False`: `process` synthesizes a cluster
    (one LLM call) but only RECORDS it; `finalize` does the supersession pass + every takeaway-blob and
    marker commit. The flag is all-or-nothing (block.py docstring): nothing durable is written in
    `process`, everything in `finalize`.

    `items()` runs the global deterministic prelude once — gather (re-anchoring every event to its
    trusted quote) + cluster (the whole pile, recomputed each run) — and yields one cluster per item;
    `source_id` is ignored (dream is a global pass). The `min_events` floor and the cluster/event tallies
    land on the INSTANCE so the shim can surface the old `RunReport` fields; the uniform `block.Report`
    stays stage-agnostic."""

    name = "dream"
    commits_per_item = False                       # THE exception: commit per run, in finalize
    marker_extra = block.no_marker_extra           # never called (the driver writes no per-item marker
                                                   # when commits_per_item=False) — declared for Block
                                                   # conformance; dream's audit rides finalize's marker

    def __init__(self, complete: Completer, *, model: str = DREAM_MODEL,
                 threshold: float = SIM_THRESHOLD, min_confidence: float = 0.0,
                 min_events: int = 0) -> None:
        self.complete = complete
        self.model = model
        self.threshold = threshold
        self.min_confidence = min_confidence
        self.min_events = min_events
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROMPT_VERSION), ("model", model))
        # the rich RunReport-shaped state the shim/CLI read; populated across items/process/finalize.
        self.takeaways: list[dict] = []
        self.n_events = 0
        self.n_clusters = 0
        self.dropped = 0                           # clusters the model judged noise (no takeaway)
        self.below_threshold = False               # fewer than min_events → did nothing
        # the global prelude's products, computed once in items() and reused by finalize.
        self._concepts: list[dict] = []
        self._known_ids: set[str] = set()
        self._pending: list[_Pending] = []         # synthesized clusters awaiting the coverage commit

    # -- enumeration: the global gather + cluster (recomputed each run) -----------------------

    def items(self, root: Path, *, source_id: str | None = None):
        """The global, deterministic prelude — yield one cluster per item. dream is a whole-pile pass,
        so `source_id` is ignored. Below the `min_events` floor it yields NOTHING (and flags
        `below_threshold`); else it clusters the whole event pile and yields each cluster. `concepts`
        is loaded here (once) for `process` to reason against, and the event/cluster tallies land on the
        instance for the Report."""
        self._concepts = load_concepts(root)
        self._known_ids = {c["id"] for c in self._concepts}   # all str (load_concepts guarantees it)
        events = gather_events(root, min_confidence=self.min_confidence)
        self.n_events = len(events)
        if len(events) < self.min_events:          # a floor below which dreaming is pointless (not a
            self.below_threshold = True            # change-detector — the done-skip handles unchanged work)
            return
        clusters = cluster(events, threshold=self.threshold)
        self.n_clusters = len(clusters)
        yield from clusters

    def key(self, cl: list[ResolvedEvent]) -> str:
        return cluster_signature(cl)

    # -- synthesize ONE cluster — record, do NOT commit --------------------------------------

    def process(self, cl: list[ResolvedEvent], *, root: Path, run_id: str) -> tuple[int, float]:
        """Synthesize one cluster (one LLM call) and RECORD it — nothing durable is committed here
        (commits_per_item=False). Returns (0, cost): 0 outputs because no blob lands yet; `cost` feeds
        the driver's `--max-usd` gate (a budget stop simply means finalize commits the clusters
        synthesized before the stop). A raised completer propagates — the driver isolates it as
        `errored`, the cluster is absent from `_pending`, retried next run."""
        tk, cost = synthesize_cluster(cl, self.complete, self._concepts,
                                      known_concept_ids=self._known_ids,
                                      model=self.model, run_id=run_id)
        self._pending.append(_Pending(cl=cl, sig=cluster_signature(cl), tk=tk, cost=cost))
        return (0, cost)

    # -- finalize: coverage-conditioned supersession, then commit per cluster -----------------

    def finalize(self, *, root: Path, run_id: str) -> None:
        """The per-run commit — today's run() steps 2+3, lifted out of the loop (ADR-0009). The driver
        hands `finalize` NO item list (#6): dream tracks its own `_pending` on the instance (it commits
        nothing per item, so a driver-kept record would be empty anyway). Two steps:

          2. COVERAGE-CONDITIONED SUPERSESSION (the orphan fix, preserved EXACTLY): a prior current
             takeaway is eligible to fold out ONLY if ALL its events are re-covered by takeaways
             emitted this run — so a dropped/errored split-child can never orphan its parent's events;
             each emitted takeaway then supersedes every eligible prior it shares an event with
             (`- {tk id}` guards a same-id re-synthesis from superseding itself).
          3. COMMIT (crash-ordering preserved): each takeaway blob durable FIRST (content then meta),
             then its cluster's `processed` marker LAST — a crash before a marker re-processes that
             cluster; the re-synthesis re-appears as a new VERSION of the same cluster_signature (a
             no-op if bytes unchanged, else latest wins). model-bump-replaces is free: a bumped model
             is a new done-key, `_ingest_takeaway` writes a new version under the same source, and
             `current_takeaways` folds to the newest."""
        emitted = [p.tk for p in self._pending if p.tk is not None]
        coverage = set().union(*(set(tk["member_events"]) for tk in emitted)) if emitted else set()
        eligible = {p["id"]: set(p.get("member_events") or [])
                    for p in current_takeaways(root)}
        eligible = {pid: pev for pid, pev in eligible.items() if pev and pev <= coverage}
        for tk in emitted:
            ev = set(tk["member_events"])
            tk["supersedes"] = sorted({pid for pid, pev in eligible.items() if pev & ev} - {tk["id"]})

        for p in self._pending:
            if p.tk is not None:
                _ingest_takeaway(p.tk, model=self.model, run_id=run_id, root=root)   # durable first ...
                self.takeaways.append(p.tk)
            else:
                self.dropped += 1
            # ... then the cluster's commit marker LAST. event_ids/dropped/cost are the per-cluster
            # audit (the old _write_processed body); block.write_processed appends the uniform fields.
            block.write_processed(
                "dream", p.sig, self.params, n_outputs=1 if p.tk is not None else 0,
                cost_usd=p.cost, run_id=run_id, root=root,
                extra={"event_ids": sorted(rv.id for rv in p.cl), "dropped": p.tk is None})


# --- run: a thin compat shim over the block driver (keeps callers' RunReport reads untouched) -----

class RunReport:
    """The shape the old `dream.run` returned — a thin WRAPPER, not a copy (the spec's #4). It holds the
    uniform `block.Report` the driver populated plus the DreamBlock instance, and exposes every field by
    reading THROUGH them via @property: the uniform fields proxy the Report (run_id/skipped/errored/
    cost_usd/stopped_on_budget), the genuinely-extra instance tallies proxy the block (n_events/
    n_clusters/takeaways/dropped/below_threshold). Nothing is copied at construction, so the hand-
    maintained desync surface (a field set here that the block later changed) is gone. `min_events` is
    the run's own arg, carried so the CLI can print the threshold it gated on. Callers (the CLI,
    test_dream/test_review setup) read `.takeaways`/`.n_clusters`/`.skipped`/`.dropped`/etc."""
    def __init__(self, report: block.Report, blk: DreamBlock, *, min_events: int) -> None:
        self._report = report
        self._blk = blk
        self.min_events = min_events

    # the genuinely-extra instance tallies — read off the block (the Report stays stage-agnostic)
    @property
    def n_events(self) -> int:
        return self._blk.n_events
    @property
    def n_clusters(self) -> int:
        return self._blk.n_clusters
    @property
    def takeaways(self) -> list[dict]:
        return self._blk.takeaways
    @property
    def dropped(self) -> int:
        return self._blk.dropped
    @property
    def below_threshold(self) -> bool:
        return self._blk.below_threshold

    # the uniform fields — proxied straight off the wrapped Report (never copied → never stale)
    @property
    def run_id(self) -> str:
        return self._report.run_id
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


def run(complete: Completer, *, model: str = DREAM_MODEL, threshold: float = SIM_THRESHOLD,
        min_confidence: float = 0.0, min_events: int = 0, max_usd: float | None = None,
        limit: int | None = None, progress: block.Progress | None = None,
        root: Path | None = None) -> RunReport:
    """Synthesize takeaways over the whole event pile, idempotently (ADR-0006) — now a thin shim over
    the per-run-commit `DreamBlock` + `block.run` (ADR-0009). The observable contract is UNCHANGED:
    cluster globally (cheap, deterministic), one LLM call per not-yet-done cluster, coverage-conditioned
    supersession over the whole run's emitted takeaways, then commit (takeaway blob first, processed
    marker last). The driver streams synthesis progress per cluster and isolates a failing call
    (counts `errored`, the cluster retries next run); the supersession + commit live in `finalize`.

    Returns a `RunReport` WRAPPING the SAME `block.Report` + `DreamBlock` instance the driver populated,
    so existing callers' field reads are untouched. `progress` defaults to None (silent) so a setup
    helper doesn't spew per-cluster lines — the CLI injects a Progress to see them."""
    blk = DreamBlock(complete, model=model, threshold=threshold,
                     min_confidence=min_confidence, min_events=min_events)
    report = block.run(blk, max_usd=max_usd, limit=limit, root=root, progress=progress)
    return RunReport(report, blk, min_events=min_events)


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
    ap.add_argument("--limit", type=int, help="cap clusters examined this run (dream is global: no --all/--source-id)")
    ap.add_argument("--dry-run", action="store_true",
                    help="cluster only — print the groupings and a sample quote each; no LLM calls")
    ap.add_argument("--show", action="store_true", help="print each synthesized takeaway")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-cluster progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
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
    # the stage owns its Progress now (the driver only speaks the protocol). None when there is nothing
    # to watch (--quiet; --dry-run already returned above); else built from this stage's args + OUT_NOUN.
    progress = None if args.quiet else block.Progress(
        "dream", cap=args.max_usd, params={"prompt_version": PROMPT_VERSION, "model": args.model},
        out_noun=OUT_NOUN, verbose=args.verbose)
    report = run(complete, model=args.model, threshold=args.threshold,
                 min_confidence=args.min_confidence, min_events=args.min_events,
                 max_usd=args.max_usd, limit=args.limit, progress=progress)
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
