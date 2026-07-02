"""resolve — dream v3's statement-first entity resolver (design doc `dream-v3-design-2026-07-01.md`
§1–§3/§7, superseding dream v2's route+apply; ADR-0028 pending).

    … glean → events → RESOLVE → claims + corroborates/contradicts edges → [review] → concepts …

v2's router over-merged because identity was a FORCED CHOICE over the whole catalog and support was
mutated in place (it latched). v3 inverts both:

  CLAIM — the L1 node. The blob stores SEED IDENTITY ONLY ({id, title, why, relation, seed_event,
    born}); everything evidential — support, sessions, evidence, subject, stmt_sig, scope — derives
    at fold time from LIVE edges + event blobs (§2.1, ADR-0013: store nothing a blob already implies).
    That discipline is what makes split-by-retraction exact: tear out an edge and the fold IS the
    claim that never matched.

  EDGE — corroboration is a minted, retractable, AUDITED artifact (§2.2): a `claim_edge` blob keyed
    on `event|verb|claim`, latest-wins, retract = a new version active:false (garden.ops idiom). The
    `match` key ({stmt_sim, subj, by, candidates_shown, prompt_version, model}) is the audit record —
    from the edge alone one can reconstruct what the model saw. Support = distinct sessions of live
    corroborates edges, recomputed on read; it can never latch.

  THE CASCADE (§3.1, two bands shipped) — per event: candidates = subject-facet ∪ rare-shingle
    neighbors over the ACTIVE claim view (the union, recall-first); the pairwise triviality gate
    (min entropy >= H_MIN) and exact Jaccard settle NON-MATCH at $0 below J_MAYBE; the residue goes
    to ONE comparative-with-none Haiku call over the top-K_RESIDUE candidates — none the stated
    default, unparseable → none → mint. Zero candidates / none → MINT a claim NOW (deterministic id,
    title = event summary, why = null) + a corroborates(by:"seed") edge, folded in-batch so the next
    event reads it (read-your-writes, the v2 property kept).

  REJECT-MERGE (§2.2) — the human "not the same" verdict is ONE compound decision (verb
    `reject-merge`, target = the event). Three folds read the same blob: the edge fold as retraction,
    the working-set fold as reopen (its verb is neither `consolidated` nor `stale`, so
    `dream.working_set` re-admits the event unchanged), and the candidate filter as a permanent
    pair-block. The done-marker KEY carries an epoch (count of reject-merge decisions naming the
    event) so the Block driver's done-index does not skip a reopened event forever.

  BUDGET = BACKPRESSURE (§7.2) — the block owns `--max-usd` itself (never handed to the driver's
    break-on-budget): when spend hits the cap, a residue event is DEFERRED — no verdict, no marker,
    retried next tick — while $0 events run to completion. Paid work never starves free work.

`resolve` is a `block.Block` (ADR-0009): per-event commit order is claim version → edge →
`consolidated` decision LAST; `finalize` runs the v3 forget (residency counts resolve-stage runs
postdating max(fetched_at, latest reopen), plus a FORGET_MIN_DAYS wall-clock minimum, §7.3).
`--reset-v2` is the append-only migration: retire every live v2 takeaway, reopen every consolidated
event, idempotent. The LLM seam is one injected `Completer` (Haiku), offline-testable with fakes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import blobstore, block, completer, config, dream, sig, subject
from .completer import Completer
from .glean import MARKER_KINDS

PROMPT_VERSION = "resolve/1"   # bump to re-adjudicate NOT-yet-consolidated events with a sharper prompt
RESOLVE_MODEL = "haiku"        # the residue adjudicator is cheap + bounded → the small model
OUT_NOUN = "claims"

CLAIM_KIND = "claim"           # the L1 node blob (seed identity only, §2.1)
EDGE_KIND = "claim_edge"       # a corroborates/contradicts edge blob (deterministic-keyed, §2.2)
EDGE_SEP = "|"                 # edge identity = event|verb|claim (ids and verbs carry no `|`)
EDGE_VERBS = ("corroborates", "contradicts")

VERB_REJECT_MERGE = "reject-merge"   # the ONE compound human verdict (§2.2) — REVIEW mints it
VERB_REOPEN = "reopen"               # the v2-reset reopen (a consolidated event re-enters the pool)
# The verbs whose latest decision drop a claim from the pool — review's retire/reject plus the
# structural supersede/merge (invalidate-don't-delete; blob + history stay, only the fold moves).
CLAIM_INVALID_VERBS = ("retire", "reject", "merge", "supersede")
ACCEPT_VERBS = ("accept", "edit")    # review's promote verbs — the accept FACET the trusted view reads (§5)

# --- knobs: named, explained, CLI-overridable (the design-philosophy rule, ADR-0025/0026/0027) -----
# J_MAYBE / H_MIN live in sig.py (the measuring instrument that earns them); the rest are resolve's.

K_RESIDUE = 3                  # UNTUNED — candidates SHOWN to the one residue call (§3.1 step 2b), top-K
                               # by stmt_sim. Overflow candidates are simply not shown — the abstention
                               # posture holds: an unseen candidate defaults to no-merge, never to a
                               # forced comparison. 3 fits the measured residue (17 pairs corpus-wide).
K_RARE = 8                     # UNTUNED — how many of the event's RAREST pool-present shingles the
                               # statement channel queries (§3.1 step 1). Rarity = document frequency
                               # over the active pool, recomputed per tick; two tellings of one lesson
                               # share its unusual words even at ~0.2 overall similarity.
RARE_MIN = 2                   # UNTUNED — rare shingles a claim must share to become a candidate. 1
                               # would candidate on a single coincidental 4-gram; too high strands
                               # paraphrases before the residue call ever sees them.
FACET_DF_MAX = 0.5             # UNTUNED — the subject-facet devaluation cut (§3.1): a facet on MORE
                               # than this fraction of active claims contributes nothing to candidacy
                               # (the repo facet on a single-repo corpus links everything to everything
                               # — the one-line slice of Senzing's shared-feature devaluation). A facet
                               # on exactly ONE claim always contributes: df=1 is maximally
                               # discriminative, and the fraction test alone would nuke it in a tiny pool.
ACTIVE_FLOOR = 0.5             # UNTUNED — the ACTIVE view's entrenchment floor (§2.1/§3.3): a claim
                               # stays in the candidate indexes while its recency-weighted net
                               # entrenchment holds this (a fresh one-session seed scores ~1.0). The
                               # view is the pool's only drain — resolve mints on every zero-match, so
                               # without it the indexes grow monotonically.
ACTIVE_DAYS = 90.0             # UNTUNED — the ACTIVE view's recency arm: a claim with ANY evidence
                               # this recent stays active regardless of entrenchment decay. Too tight
                               # and a slow-recurring lesson falls out of candidacy and re-seeds
                               # (recoverable via merge suggestions, but noisy); too loose and the view
                               # stops bounding the indexes.
FORGET_MIN_DAYS = 7.0          # UNTUNED — the wall-clock minimum an event must have waited before
                               # forget may stale it (§7.3): v3 ticks are cheap and frequent, so cycle
                               # count alone would accelerate eviction; a week of real time keeps
                               # "aged" honest.

TITLE_MAX = dream.TITLE_MAX


# --- the residue adjudication prompt: comparative, explicit none, none the stated default (§3.1) ---

RESOLVE_SYSTEM = (
    "You are the MATCHER for a developer's long-term memory. A new OBSERVATION (a lesson from a "
    "Claude Code session) arrived; you are shown a SHORT list of candidate CLAIMS whose wording is "
    "similar. Decide whether the observation teaches the SAME underlying lesson as one candidate:\n"
    "  - none: it is a NEW lesson — no candidate teaches it. Most observations are new lessons, so "
    "NONE is the expected answer; pick a candidate only if it teaches the SAME lesson, not merely a "
    "related topic.\n"
    "  - same-as-N: candidate N teaches the same underlying lesson (the observation is more evidence "
    "FOR it).\n"
    "  - contradicts-N: the observation is evidence that candidate N is WRONG or no longer holds.\n"
    "The observation's verbatim QUOTE is ground truth; its one-line summary is only a hint.\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{"verdict": "none" | "same-as-N" | "contradicts-N"}   (N = a candidate number)'
)

_VERDICT_RE = re.compile(r"^(same-as|contradicts)-(\d+)$")


def _residue_user(rv: dream.ResolvedEvent, shown: list[dict]) -> str:
    lines = []
    for i, c in enumerate(shown, 1):
        stmt = str(c.get("title", "")).strip()[:TITLE_MAX]
        why = str(c.get("why") or "").strip()[:dream.WHY_MAX]
        lines.append(f"[{i}] id={c.get('id')}: {stmt}" + (f" — {why}" if why else ""))
    return (f'OBSERVATION\nquote: """{rv.quote}"""\n'
            f'summary: {str(rv.event.get("summary", "")).strip()!r}\n\n'
            f'CANDIDATES\n' + "\n".join(lines))


def _clean_verdict(parsed, n: int) -> tuple[str, int | None]:
    """Strict coercion of the untrusted matcher JSON (§3.1 step 2b): the ONLY accepted answers are
    "none", "same-as-N", "contradicts-N" (N a shown candidate number; a split {"verdict": "same-as",
    "candidate": N} is tolerated as the same fact). ANYTHING else — missing, malformed, out-of-range,
    prose — coerces to none: the abstention default, so a parse failure can only under-merge (a mint),
    never over-merge. Returns (verdict, candidate_number|None)."""
    parsed = parsed if isinstance(parsed, dict) else {}
    v = parsed.get("verdict")
    if not isinstance(v, str):
        return "none", None
    v = v.strip().lower()
    if v == "none":
        return "none", None
    m = _VERDICT_RE.match(v)
    if m and 1 <= int(m.group(2)) <= n:
        return m.group(1), int(m.group(2))
    k = parsed.get("candidate")
    if v in ("same-as", "contradicts") and isinstance(k, int) and not isinstance(k, bool) and 1 <= k <= n:
        return v, k
    return "none", None


def adjudicate(rv: dream.ResolvedEvent, shown: list[dict],
               complete_resolve: Completer) -> tuple[str, int | None, float]:
    """ONE comparative-with-none call over the event's top-K_RESIDUE residue candidates — the entire
    LLM acceptance layer (§0/§3.1). Returns (verdict, candidate_number, cost). A raised completer
    propagates so the driver isolates the event (retried next tick)."""
    comp = complete_resolve(RESOLVE_SYSTEM, _residue_user(rv, shown))
    cost = completer.cost_of(comp)
    verdict, k = _clean_verdict(completer.parse_json_object(comp.text) or {}, len(shown))
    return verdict, k, cost


# --- edges: deterministic-keyed claim_edge blobs, latest-wins, retract = active:false (§2.2) -------

def edge_id(event_id: str, verb: str, claim_id: str) -> str:
    return f"{event_id}{EDGE_SEP}{verb}{EDGE_SEP}{claim_id}"


def write_edge(event_id: str, verb: str, claim_id: str, *, session_id: str | None, match: dict,
               active: bool = True, root: Path | None = None, run_id: str) -> tuple[str, bool]:
    """Append a corroborates/contradicts edge as a `claim_edge` blob VERSION keyed on its identity
    `event|verb|claim` (the garden.ops idiom, ADR-0015). CONTENT is run-invariant ({event_id, claim_id,
    verb, session_id, match, active}) so a crash-retry re-ingests byte-identically (a no-op TimeMap
    version, never a duplicate edge); the producer rides in origin_ref. `match` is the AUDIT key —
    {stmt_sim, subj, by, candidates_shown, prompt_version, model} — the review card renders it and a
    gold set can re-score the verdict against it later. Rejects an unknown verb (a producer bug, not
    data to fold)."""
    if verb not in EDGE_VERBS:
        raise ValueError(f"write_edge: unknown verb {verb!r} (allowed: {EDGE_VERBS})")
    body = blobstore.canonical_json({"event_id": event_id, "claim_id": claim_id, "verb": verb,
                                     "session_id": session_id, "match": match, "active": bool(active)})
    return blobstore.ingest(body, source_kind=EDGE_KIND, source_id=edge_id(event_id, verb, claim_id),
                            origin_ref={"stage": "resolve", "run_id": run_id}, root=root)


def retract_edge(event_id: str, verb: str, claim_id: str, *, root: Path | None = None,
                 run_id: str) -> tuple[str, bool]:
    """Retract an edge — a new version with active:false, match key preserved (invalidate-don't-delete;
    the audit record survives its own retraction). This IS the split (§2.2, Senzing "unresolve"): the
    claim blob stores seed identity only, so the fold after retraction equals the fold had the event
    never matched. Re-retracting is byte-identical → no-op."""
    root = root or config.data_root()
    h = blobstore.latest_version(edge_id(event_id, verb, claim_id), root)
    if not h:
        raise ValueError(f"no edge {edge_id(event_id, verb, claim_id)!r} to retract")
    obj = json.loads(blobstore.get(h, root))
    return write_edge(event_id, verb, claim_id, session_id=obj.get("session_id"),
                      match=obj.get("match") or {}, active=False, root=root, run_id=run_id)


def reject_merge(event_id: str | None = None, *, edge_id: str | None = None,
                 pair: list[str] | None = None,
                 reason: str = "", reviewer: str = "sulin", root: Path | None = None,
                 run_id: str | None = None) -> dict:
    """The ONE compound human "not the same" verdict (§2.2) — a single append-only decision blob,
    verb `reject-merge`, TARGET = the event (so `dream.working_set`'s latest-decision fold reopens it
    unchanged — its verb is neither `consolidated` nor `stale`). The body carries the edge to retract
    and/or the claim pair to block; THREE folds read this one blob: `_live_edges` as retraction,
    the working set as reopen, and the candidate filter as a permanent pair-block. One append, atomic —
    no crash window can strand the event or let the next tick re-form the torn-out merge. REVIEW-only
    by policy (ADR-0008): a permanent negative constraint is a trust-boundary artifact.

    Two forms: with an `edge_id` the event is implied (derived when `event_id` is omitted) and all
    three folds fire; PAIR-ONLY (a dismissed merge SUGGESTION between two live claims, §6.5) carries
    no event — target is None, nothing retracts or reopens, and only the pair-block fold reads it
    (the suggestion query stops asking, resolve never pairs them)."""
    if not edge_id and not pair:
        raise ValueError("reject_merge needs an edge_id and/or a pair to block")
    if event_id is None and edge_id:
        parts = edge_id.split(EDGE_SEP)
        event_id = parts[0] if len(parts) == 3 else None
    root = root or config.data_root()
    at = config.now()
    body = {"verb": VERB_REJECT_MERGE, "target": event_id, "event_id": event_id,
            "edge_id": edge_id, "pair": sorted(pair) if pair else None,
            "reason": str(reason)[:dream.NOTE_MAX], "reviewer": reviewer,
            "at": at, "run_id": run_id or config.run_id(),
            "producer": {"stage": "review", "at": at}}
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s), prev=None,
                     origin_ref={"stage": "review", "verb": VERB_REJECT_MERGE, "target": event_id},
                     fetched_at=at, root=root)
    return body


def _reject_merge_facts(root: Path) -> dict:
    """The three derived reads of the compound decision (§2.2), in ONE scan: `edges` — edge source_ids
    the edge fold treats as retracted (permanently: even a re-written deterministic-keyed edge blob
    stays dead, which is what closes the re-form crash window); `pairs` — frozensets of ids the
    candidate filter blocks forever; `epochs` — per-event reject-merge COUNT, the done-key suffix that
    re-admits a reopened event to the Block driver. Also folds the latest reopen-ish timestamp per
    event (`reopen_at`, reject-merge OR the v2-reset `reopen`) for forget's residency clock (§7.3)."""
    edges: set[str] = set()
    pairs: set[frozenset] = set()
    epochs: Counter = Counter()
    reopen_at: dict[str, str] = {}
    for d in blobstore.decisions_for(None, root):
        verb = d.get("verb")
        if verb == VERB_REOPEN:
            t = d.get("target")
            if t:
                reopen_at[t] = max(reopen_at.get(t, ""), str(d.get("at", "")))
            continue
        if verb != VERB_REJECT_MERGE:
            continue
        eid = d.get("event_id") or d.get("target")
        if eid:
            epochs[eid] += 1
            reopen_at[eid] = max(reopen_at.get(eid, ""), str(d.get("at", "")))
        ridge = d.get("edge_id")
        if ridge:
            edges.add(ridge)
            parts = ridge.split(EDGE_SEP)
            if len(parts) == 3:
                pairs.add(frozenset((parts[0], parts[2])))
        pr = d.get("pair")
        if isinstance(pr, list) and len(pr) == 2:
            pairs.add(frozenset(pr))
    return {"edges": edges, "pairs": pairs, "epochs": epochs, "reopen_at": reopen_at}


def _live_edges(root: Path, rm: dict) -> dict[str, list[dict]]:
    """claim_id → its LIVE edges: latest version per `event|verb|claim` identity, active:true, minus
    any a reject-merge decision retracted (§2.2 — the decision outlives the blob's own latest-wins).
    Malformed/absent blobs are skipped, never fatal."""
    by_claim: dict[str, list[dict]] = {}
    for sid, h in blobstore.latest_by_kind(EDGE_KIND, root).items():
        if sid in rm["edges"]:
            continue                                   # reject-merge reads as retraction — permanently
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if not (isinstance(obj, dict) and obj.get("active")):
            continue
        if not (obj.get("event_id") and obj.get("claim_id") and obj.get("verb") in EDGE_VERBS):
            continue
        by_claim.setdefault(obj["claim_id"], []).append(obj)
    return by_claim


# --- the claim fold: every evidential attribute derives from live edges + event blobs (§2.1) -------

def corro_fingerprint(claim_id: str, event_ids) -> str:
    """The live corroborates-edge-set fingerprint (§7.3): sha256 over the SORTED edge source_ids
    (`event|corroborates|claim`) of the claim's live corroborating events, first 16 hex. `synthesize`
    stamps it on the claim version it mints — the exact edge set the prose consumed — and `_fold_claim`
    recomputes it from the live fold to flag `why_stale` on divergence. The `why` is the ONE stored
    non-derived field, so a retraction after synthesis would otherwise leave fused prose latched:
    staleness must be DETECTED here, not recomputed away."""
    ids = sorted(edge_id(e, "corroborates", claim_id) for e in set(event_ids))
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]



def _load_event(eid: str, ev_hashes: dict, cache: dict, root: Path) -> dict | None:
    if eid in cache:
        return cache[eid]
    obj = None
    h = ev_hashes.get(eid)
    if h:
        try:
            parsed = json.loads(blobstore.get(h, root))
            if isinstance(parsed, dict):
                parsed.setdefault("id", eid)
                obj = parsed
        except (OSError, json.JSONDecodeError):
            obj = None
    cache[eid] = obj
    return obj


def _event_subject(ev: dict, root: Path, cache: dict) -> dict:
    sp = (ev.get("evidence") or [{}])[0]
    sp = sp if isinstance(sp, dict) else {}
    return subject.subject_key(root, ev.get("cleaned_hash"), (sp.get("byte_start"), sp.get("byte_end")),
                               cache)


def scope_of(subj_keys: list[dict]) -> str:
    """The display scope (§3.4), recomputed from the corroborating events' subject keys: `local` if the
    evidence shares a repo or a file, `cross-cutting` if it spans >= 2 repos with NO shared file. Shown
    on the review card, never wired to a second maturity bar (the dual bar is backlog)."""
    repos = {k.get("repo") for k in subj_keys if k.get("repo")}
    file_df: Counter = Counter()
    for k in subj_keys:
        file_df.update(set(k.get("files") or ()))
    shared_file = any(c >= 2 for c in file_df.values())
    return "cross-cutting" if len(repos) >= 2 and not shared_file else "local"


def _fold_claim(content: dict, edges: list[dict], ev_hashes: dict, ev_cache: dict, blobs: dict,
                sessions: dict, subj_cache: dict, root: Path) -> dict:
    """One claim's DERIVED view (§2.1): the stored blob is seed identity only; support, sessions,
    evidence (shaped exactly like dream's, so `review.resolve_evidence` reads it unchanged), the
    contradiction side, subject, scope, and stmt_sig all fold from LIVE edges + event blobs here.

    The signature signs the SEED summary (= the stored title) ∪ the LIVE corroborating events'
    summaries — NEVER synthesized why/title prose (§2.1: prose jumps register and signing it chains
    transitive generalization). Entropy is the MAX over those summaries: a claim is trivial only if
    EVERY telling of it is low-signal. Sessions count from the EDGES (the edge carries session_id), so
    support survives a TTL-reclaimed event blob; only the evidence ENTRY needs the blob to re-anchor."""
    title = str(content.get("title", ""))
    shingles = set(sig.char_shingles(title))
    ent = sig.entropy(title)
    cites: list[str] = []
    sessions_seen: list[str] = []
    evidence: list[dict] = []
    subj_keys: list[dict] = []
    markers = {k: 0.0 for k in MARKER_KINDS}
    confs: list[float] = []
    corro = sorted((e for e in edges if e["verb"] == "corroborates"), key=lambda e: e["event_id"])
    contra = sorted((e for e in edges if e["verb"] == "contradicts"), key=lambda e: e["event_id"])
    for e in corro:
        eid = e["event_id"]
        if eid not in cites:
            cites.append(eid)
        s = e.get("session_id")
        if s and s not in sessions_seen:
            sessions_seen.append(s)
        ev = _load_event(eid, ev_hashes, ev_cache, root)
        if ev is None:
            continue
        summary = str(ev.get("summary", ""))
        shingles |= sig.char_shingles(summary)
        ent = max(ent, sig.entropy(summary))
        subj_keys.append(_event_subject(ev, root, subj_cache))
        em = dream._event_markers(ev)
        markers = {k: max(markers[k], em[k]) for k in MARKER_KINDS}
        confs.append(completer.clean_score(ev.get("confidence"), 0.5))
        rv = dream._resolve_event(ev, blobs, sessions, root)
        if rv is not None:
            evidence.append(dream.evidence_entry(rv))
    contradicted_by: list[str] = []
    contradiction_evidence: list[dict] = []
    for e in contra:
        eid = e["event_id"]
        if eid in contradicted_by:
            continue
        contradicted_by.append(eid)
        entry = {"event_id": eid, "session_id": e.get("session_id")}   # session rides even if the blob is gone
        ev = _load_event(eid, ev_hashes, ev_cache, root)
        rv = dream._resolve_event(ev, blobs, sessions, root) if ev else None
        if rv is not None:
            entry = dream.evidence_entry(rv)
            entry["session_id"] = e.get("session_id")
        contradiction_evidence.append(entry)
    view = {
        "id": content["id"],
        "title": title,
        "why": content.get("why"),
        # the why-staleness derivation (§7.3): synthesize stamped the edge-set fingerprint its prose
        # consumed; if the LIVE corroborates set has since diverged (a retraction, a new merge), the
        # prose no longer describes the evidence — flag it for the review surface. why=null claims and
        # pre-stamp blobs read as fresh (False): nothing synthesized, nothing to be stale.
        "why_fingerprint": content.get("why_fingerprint"),
        "why_stale": bool(content.get("why") is not None and content.get("why_fingerprint")
                          and content["why_fingerprint"] != corro_fingerprint(content["id"], cites)),
        "relation": content.get("relation") or {"kind": "new", "concept_id": None, "note": ""},
        "seed_event": content.get("seed_event"),
        "born": content.get("born"),
        "cites": cites,
        "evidence": evidence,
        "support": {"events": len(set(cites)), "sessions": len(set(sessions_seen))},
        "sessions_seen": sessions_seen,
        "contradicted_by": contradicted_by,
        "contradiction_evidence": contradiction_evidence,
        "markers": markers,
        "confidence": max(confs) if confs else 0.5,
        "subject": {"repos": sorted({k["repo"] for k in subj_keys if k.get("repo")}),
                    "files": sorted({f for k in subj_keys for f in (k.get("files") or ())})},
        "scope": scope_of(subj_keys),
        "stmt_shingles": frozenset(shingles),
        "stmt_entropy": ent,
        "_subj_keys": subj_keys,                       # in-memory only: the in-batch scope recompute
    }
    view["contradictions"] = dream._contradiction_stats(view)
    return view


def _decision_binds(d: dict, content: dict) -> bool:
    """Does a lifecycle decision BIND this claim? Only if it does not PREDATE the claim's birth. The
    guard exists for one real collision: a claim's deterministic id is `mint_takeaway_id(seed_event)`
    — the SAME id dream v2 minted for the takeaway seeded by that event — so the v2-reset's `retire`
    on the old takeaway lands on the id the v3 claim is later re-minted under. A decision cannot bind
    an artifact that did not yet exist, so a decision older than `born` is read as targeting the
    retired predecessor, never the live claim. A missing `born` reads as bound (conservative)."""
    born = str(content.get("born") or "")
    at = str(d.get("at") or d.get("fetched_at") or "")
    return not born or at >= born


def claim_pool(root: Path | None = None) -> list[dict]:
    """The current claim views: `latest_by_kind('claim')` MINUS any whose latest BINDING lifecycle
    decision is retire/reject/merge/supersede (invalidate-don't-delete; pre-birth decisions targeted
    the same-id v2 takeaway — see `_decision_binds`), each folded over its LIVE edges (§2.1). Sorted
    by id for stable output; malformed blobs skipped, never fatal."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    rm = _reject_merge_facts(root)
    edges_by_claim = _live_edges(root, rm)
    ev_hashes = blobstore.latest_by_kind("event", root)
    ev_cache: dict = {}
    blobs: dict = {}
    sessions: dict = {}
    subj_cache: dict = {}
    out: list[dict] = []
    for cid, h in sorted(blobstore.latest_by_kind(CLAIM_KIND, root).items()):
        try:
            content = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if not (isinstance(content, dict) and isinstance(content.get("id"), str) and content["id"]):
            continue
        d = decisions.get(cid)
        if d and d.get("verb") in CLAIM_INVALID_VERBS and _decision_binds(d, content):
            continue
        out.append(_fold_claim(content, edges_by_claim.get(cid, []), ev_hashes, ev_cache,
                               blobs, sessions, subj_cache, root))
    return out


def is_active(view: dict, *, now: str, valid_times: dict, floor: float = ACTIVE_FLOOR,
              days: float = ACTIVE_DAYS) -> bool:
    """The ACTIVE-view predicate (§2.1/§3.3) — the pool's derived drain, never a stored status:
    active = recency-weighted net entrenchment >= ACTIVE_FLOOR, OR any evidence within ACTIVE_DAYS.
    Inactive claims fold out of the candidate indexes; their blobs, edges, and history remain, and a
    re-seeded near-duplicate reconciles later through the derived merge-suggestion query at review.
    An undateable session reads as fresh (age 0 — the recall-safe direction, like recency_weight)."""
    if dream.net_entrenchment(view, now, valid_times=valid_times) >= floor:
        return True
    ss = view.get("sessions_seen") or []
    if not ss:
        return False
    return min(config.age_days(valid_times.get(s), now=now) for s in ss) <= days


def current_claims(root: Path | None = None, *, maturity: float = dream.MATURITY_WEIGHT,
                   now: str | None = None, valid_times: dict | None = None) -> list[dict]:
    """The MATURITY GATE over the fold — the single bar (§3.4/§4, `dream.MATURITY_WEIGHT`
    single-sourced): claims whose recency-weighted net entrenchment (distinct support sessions minus
    contradicting ones, each valid-time-weighted — `dream.net_entrenchment` reads the folded view
    unchanged) crosses the reviewer's bar. Same shape/semantics as `dream.current_takeaways`."""
    root = root or config.data_root()
    now = now or config.now()
    if valid_times is None:
        valid_times = dream._session_valid_times(root)
    return [c for c in claim_pool(root)
            if dream.net_entrenchment(c, now, valid_times=valid_times) >= maturity]


def high_confidence_view(root: Path | None = None, *, maturity: float = dream.MATURITY_WEIGHT) -> list[dict]:
    """The trusted subset (§5) — the ONLY thing generate reads and refine's prior: ONE graph, filtered
    to latest_decision == accept AND net_entrenchment >= the bar AND no live retire/supersede (the pool
    fold already drops those). Review appends a decision FACET; nothing forks."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    now = config.now()
    valid_times = dream._session_valid_times(root)
    out: list[dict] = []
    for c in claim_pool(root):
        d = decisions.get(c["id"])
        if not (d and d.get("verb") in ACCEPT_VERBS and _decision_binds(d, c)):
            continue                                   # a pre-birth accept trusted the same-id predecessor
        if dream.net_entrenchment(c, now, valid_times=valid_times) >= maturity:
            out.append(c)
    return out


# --- the candidate indexes: subject facets + rare shingles, over the ACTIVE view (§3.1/§3.3) -------

def _claim_facets(view: dict) -> list[str]:
    subj = view.get("subject") or {}
    return ([f"repo:{r}" for r in subj.get("repos") or ()]
            + [f"file:{f}" for f in subj.get("files") or ()])


def _event_facets(key: dict) -> list[str]:
    out = [f"file:{f}" for f in key.get("files") or ()]
    if key.get("repo"):
        out.append(f"repo:{key['repo']}")
    return out


def build_indexes(active_views: list[dict]) -> dict:
    """The per-tick candidate indexes over the ACTIVE pool (§3.3): `facets` — subject facet →
    claim-id set; `shingles` — char-shingle → claim-id set (the inverted index the rare-shingle
    channel queries; document frequency IS `len(index[shingle])`, so rarity needs no second
    structure); `n` — the active count the facet-devaluation fraction reads. O(active pool) to
    build, stdlib dicts; never ∝ the raw store."""
    idx = {"facets": {}, "shingles": {}, "n": 0}
    for v in active_views:
        index_claim(idx, v)
    return idx


def index_claim(idx: dict, view: dict) -> None:
    """Fold ONE claim into the indexes — used at build and for the in-batch read-your-writes fold
    (a just-minted claim is a candidate for the very next event in the tick)."""
    for f in _claim_facets(view):
        idx["facets"].setdefault(f, set()).add(view["id"])
    for s in view["stmt_shingles"]:
        idx["shingles"].setdefault(s, set()).add(view["id"])
    idx["n"] += 1


def candidate_ids(idx: dict, ev_shingles: frozenset, ev_subject: dict, *, k_rare: int = K_RARE,
                  rare_min: int = RARE_MIN, facet_df_max: float = FACET_DF_MAX) -> set[str]:
    """Candidate generation (§3.1 step 1) — the UNION of the two channels, recall-first:

    SUBJECT — claims sharing a repo/file facet. An EMPTY subject key contributes nothing (seed-only
    via subject, §3.1 step 0: empty∩empty is not overlap). A facet on more than FACET_DF_MAX of the
    active pool is DEVALUED — contributes nothing — except at df=1, which is always discriminative.

    STATEMENT — the rare-shingle channel, the paraphrase recall path (LSH bands only collide at
    J≳0.6; real same-lesson paraphrases sit at 0.21–0.24). Of the event's shingles PRESENT in the
    pool (a pool-absent shingle can collide with nothing, so it must not consume the recall budget),
    take the K_RARE rarest by document frequency (ties broken by shingle value — deterministic);
    a claim sharing >= RARE_MIN of them is a candidate."""
    cands: set[str] = set()
    n = max(1, idx["n"])
    if not subject.is_empty(ev_subject):
        for f in _event_facets(ev_subject):
            claims = idx["facets"].get(f)
            if not claims:
                continue
            if len(claims) >= 2 and len(claims) > facet_df_max * n:
                continue                               # devalued: the facet links everything to everything
            cands |= claims
    present = sorted((len(idx["shingles"][s]), s) for s in ev_shingles if s in idx["shingles"])
    hits: Counter = Counter()
    for _, s in present[:k_rare]:
        hits.update(idx["shingles"][s])
    cands |= {cid for cid, c in hits.items() if c >= rare_min}
    return cands


def _subj_score(ev_subject: dict, view: dict) -> float:
    """The soft scope signal between an event's subject key and a claim's FOLDED subject union — the
    claim-side generalization of `subject.subject_overlap` (same weights, `tools` fixed at 0). Recorded
    on the edge's match key for audit; never a veto (§3.4)."""
    from . import concepts                             # lazy: concepts→dream→glean→subject cycle guard
    subj = view.get("subject") or {}
    score = concepts.W_FILE * len(set(ev_subject.get("files") or ()) & set(subj.get("files") or ()))
    if ev_subject.get("repo") and ev_subject["repo"] in (subj.get("repos") or ()):
        score += concepts.W_REPO
    return score


# --- decisions: consolidated / stale / reopen, written in review/dream's idiom ---------------------

def _write_decision(body: dict, *, root: Path | None) -> None:
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s), prev=None,
                     origin_ref={"stage": "resolve", "run_id": body.get("run_id")},
                     fetched_at=body["at"], root=root)


def _write_consolidated(event_id: str, claim_id: str | None, decision: str, *, run_id: str,
                        root: Path | None) -> None:
    """The commit point that removes a resolved event from the working set (`dream.working_set` folds
    the same verb, so the two stages share one membership fold). `decision` records the cascade's
    verdict: `seed` (minted its own claim) | `corroborates` | `contradicts`."""
    at = config.now()
    _write_decision({"verb": "consolidated", "target": event_id, "claim": claim_id,
                     "decision": decision, "at": at, "run_id": run_id,
                     "producer": {"stage": "resolve", "run_id": run_id, "at": at}}, root=root)


def _write_stale(event_id: str, reason: str, *, run_id: str, root: Path | None) -> None:
    at = config.now()
    _write_decision({"verb": "stale", "target": event_id, "reason": reason, "at": at, "run_id": run_id,
                     "producer": {"stage": "resolve", "run_id": run_id, "at": at}}, root=root)


# --- forget: straggler eviction, reopen-aware + wall-clock-bounded (§7.3) --------------------------

def forget(root: Path | None = None, *, tau: int = dream.FORGET_TAU,
           salience_floor: float = dream.FORGET_SALIENCE_FLOOR, min_days: float = FORGET_MIN_DAYS,
           run_id: str) -> list[str]:
    """dream's conservative straggler eviction (never age alone — ADR-0010 §6), re-scoped for v3's
    cheap ticks (§7.3). It cannot live as a call into `dream.forget` because both amendments change
    what "resident" means: (a) residency counts distinct RESOLVE-stage runs postdating
    max(event.fetched_at, latest reopen) — a reopened event must not be mass-staled at its first
    finalize because pre-reopen runs counted against it; (b) a wall-clock FORGET_MIN_DAYS accompanies
    the cycle count — frequent cheap ticks must not accelerate eviction. Still the CONJUNCTION with
    low intrinsic salience (relevance orders, never evicts), still a reversible `stale` decision."""
    root = root or config.data_root()
    ws = dream.working_set(root)
    latest = blobstore.latest_by_kind("event", root)
    resolve_decs: list[dict] = []
    reopen_at: dict[str, str] = {}
    for d in blobstore.decisions_for(None, root):
        if (d.get("producer") or {}).get("stage") == "resolve":
            resolve_decs.append(d)
        if d.get("verb") in (VERB_REOPEN, VERB_REJECT_MERGE) and d.get("target"):
            t = d["target"]
            reopen_at[t] = max(reopen_at.get(t, ""), str(d.get("at", "")))
    staled: list[str] = []
    for rv in ws:
        born = ""
        h = latest.get(rv.id)
        if h:
            try:
                born = blobstore.get_meta(h, root).get("fetched_at", "")
            except (OSError, json.JSONDecodeError):
                born = ""
        born = max(born, reopen_at.get(rv.id, ""))     # the residency clock restarts at a reopen
        runs = {d.get("run_id") for d in resolve_decs
                if d.get("run_id") and str(d.get("at", "")) > born}
        if (len(runs) >= tau and dream._intrinsic_salience(rv.event) < salience_floor
                and config.age_days(born or None) >= min_days):
            _write_stale(rv.id, "aged + low salience", run_id=run_id, root=root)
            staled.append(rv.id)
    return staled


# --- the v2 reset: append-only migration, idempotent (§9 / RUNBOOK drain note) ---------------------

RESET_REASON = ("dream v3 reset (ADR-0028): v2 takeaways retire — v2 corroboration was co-occurrence, "
                "not recurrence; their events reopen and re-resolve into match-keyed claims")


def reset_v2(root: Path | None = None, *, dry_run: bool = False,
             run_id: str | None = None) -> tuple[list[str], list[str]]:
    """`--reset-v2`: retire every LIVE v2 takeaway (a `retire` decision, review's own verb — the blob
    + history stay) and REOPEN every event whose latest decision is `consolidated` (a `reopen`
    decision, so `dream.working_set`'s fold re-admits it and resolve re-consolidates it into claims).
    Append-only and IDEMPOTENT: `dream.catalog` already excludes retired takeaways, and a reopened
    event's latest decision is no longer `consolidated` — a second run finds nothing to do. Run the
    drain with `--no-forget` until pending hits 0 (§7.3): a freshly reopened backlog is all
    "stragglers" by cycle count. Returns (retired_ids, reopened_ids)."""
    root = root or config.data_root()
    run_id = run_id or config.run_id()
    retired: list[str] = []
    for tk in dream.catalog(root):
        retired.append(tk["id"])
        if not dry_run:
            at = config.now()
            _write_decision({"verb": "retire", "target": tk["id"], "reason": RESET_REASON,
                             "at": at, "run_id": run_id,
                             "producer": {"stage": "resolve", "run_id": run_id, "at": at}}, root=root)
    reopened: list[str] = []
    decisions = blobstore.latest_decisions(root)
    for sid in sorted(blobstore.latest_by_kind("event", root)):
        d = decisions.get(sid)
        if d and d.get("verb") == "consolidated":
            reopened.append(sid)
            if not dry_run:
                at = config.now()
                _write_decision({"verb": VERB_REOPEN, "target": sid, "reason": RESET_REASON,
                                 "at": at, "run_id": run_id,
                                 "producer": {"stage": "resolve", "run_id": run_id, "at": at}}, root=root)
    return retired, reopened


# --- the Block: the statement-first cascade as a per-event-commit driver stage (§3.1/§7) -----------

class Deferred(Exception):
    """A residue event deferred under --max-usd (§7.2): raised from process() so the driver isolates
    the event WITHOUT a marker — exactly the deferral contract (no verdict, no marker, retried next
    tick) — while $0 events continue. The driver tallies it under `errored`; the block's own
    `n_deferred` names it honestly on the resolve Report."""


class ResolveBlock:
    """The v3 resolver as a `block.Block` (ADR-0009): `items()` builds the claim pool, the ACTIVE-view
    indexes, and the reject-merge facts ONCE, then yields dream's salience-ordered working set;
    `process(rv)` runs the cascade, committing claim → edge → `consolidated` decision (LAST) per
    event and folding the result into the on-instance pool + indexes so the NEXT event reads it
    (read-your-writes — the duplicate-seed race stays closed). The done-marker KEY carries the
    event's reject-merge EPOCH so a reopened event re-enters (§2.2). `finalize()` runs the v3 forget.

    BUDGET is the block's, not the driver's (§7.2): `max_usd` caps RESIDUE spend only — when
    exhausted, a residue event raises `Deferred` (no marker, retried next tick) and $0 events keep
    flowing; the tick never breaks mid-list because a paid call starved free work."""

    name = "resolve"
    commits_per_item = True
    marker_extra = block.no_marker_extra

    def __init__(self, complete_resolve: Completer, *, model: str = RESOLVE_MODEL,
                 min_confidence: float = 0.0, topic: str | None = None,
                 maturity: float = dream.MATURITY_WEIGHT,
                 j_maybe: float | None = None, h_min: float | None = None,
                 k_residue: int = K_RESIDUE, k_rare: int = K_RARE, rare_min: int = RARE_MIN,
                 facet_df_max: float = FACET_DF_MAX, active_floor: float = ACTIVE_FLOOR,
                 active_days: float = ACTIVE_DAYS, max_usd: float | None = None,
                 forget: bool = True, forget_tau: int = dream.FORGET_TAU,
                 forget_floor: float = dream.FORGET_SALIENCE_FLOOR,
                 forget_min_days: float = FORGET_MIN_DAYS) -> None:
        self.complete_resolve = complete_resolve
        self.model = model
        self.min_confidence = min_confidence
        self.topic = topic
        self.maturity = maturity
        self.j_maybe = sig.J_MAYBE if j_maybe is None else j_maybe
        self.h_min = sig.H_MIN if h_min is None else h_min
        self.k_residue = k_residue
        self.k_rare = k_rare
        self.rare_min = rare_min
        self.facet_df_max = facet_df_max
        self.active_floor = active_floor
        self.active_days = active_days
        self.max_usd = max_usd                         # the RESIDUE-spend cap (deferral, not break — §7.2)
        self.forget_on = forget
        self.forget_tau = forget_tau
        self.forget_floor = forget_floor
        self.forget_min_days = forget_min_days
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROMPT_VERSION), ("model", model))
        # on-instance state, reconciled from the store at items() — never a source of truth.
        self._pool_by_id: dict[str, dict] = {}
        self._idx: dict = {"facets": {}, "shingles": {}, "n": 0}
        self._blocked: set[frozenset] = set()
        self._epochs: Counter = Counter()
        self._subj_cache: dict = {}
        self._spent = 0.0
        self._root: Path | None = None
        self._born: dict[str, str | None] | None = None
        # Report tallies.
        self.n_events = 0
        self.n_pool = 0
        self.n_active = 0
        self.n_minted = 0
        self.n_corroborated = 0
        self.n_contradicted = 0
        self.n_residue_calls = 0
        self.n_deferred = 0
        self.claims: list[dict] = []
        self.staled: list[str] = []

    def items(self, root: Path, *, source_id: str | None = None):
        """Build the pool + indexes once (the ACTIVE view only feeds candidacy, §3.3), then yield the
        working set. `source_id` is ignored (resolve is a global pass)."""
        self._root = root
        self._born = None
        self._subj_cache = {}
        self._spent = 0.0
        now = config.now()
        valid_times = dream._session_valid_times(root)
        pool = claim_pool(root)
        self._pool_by_id = {c["id"]: c for c in pool}
        active = [c for c in pool if is_active(c, now=now, valid_times=valid_times,
                                               floor=self.active_floor, days=self.active_days)]
        self._idx = build_indexes(active)
        self.n_pool, self.n_active = len(pool), len(active)
        rm = _reject_merge_facts(root)
        self._blocked = rm["pairs"]
        self._epochs = rm["epochs"]
        ws = dream.working_set(root, min_confidence=self.min_confidence)
        if self.topic is not None:
            ws = dream.filter_by_topic(ws, self.topic, root)
        self.n_events = len(ws)
        return ws

    def key(self, rv: dream.ResolvedEvent) -> str:
        """The done-marker key: the event id, EPOCH-suffixed once a reject-merge names it (§2.2). The
        driver's params are per-RUN, so the per-EVENT epoch must ride in the key — a reopened event's
        key is new and the done-index cannot skip it forever; epoch 0 keeps today's bare key."""
        ep = self._epochs.get(rv.id, 0)
        return rv.id if ep == 0 else f"{rv.id}#e{ep}"

    def priority(self, rv: dream.ResolvedEvent) -> float:
        return dream.salience(rv.event)

    def age(self, rv: dream.ResolvedEvent) -> float:
        """Age in days for the Aging policy (ADR-0021), exactly dream's: lazy `_event_born_map`, so a
        Greedy run pays nothing."""
        if self._born is None:
            self._born = dream._event_born_map(self._root)
        return config.age_days(self._born.get(rv.id))

    # -- the cascade -------------------------------------------------------------------------------

    def process(self, rv: dream.ResolvedEvent, *, root: Path, run_id: str) -> tuple[int, float]:
        summary = str(rv.event.get("summary", ""))
        ev_sh = sig.char_shingles(summary)
        ev_ent = sig.entropy(summary)
        ev_subj = subject.subject_key(root, rv.event.get("cleaned_hash"), rv.span, self._subj_cache)
        cands = candidate_ids(self._idx, ev_sh, ev_subj, k_rare=self.k_rare, rare_min=self.rare_min,
                              facet_df_max=self.facet_df_max)
        scored: list[tuple[float, str]] = []
        for cid in cands:
            if frozenset((rv.id, cid)) in self._blocked:
                continue                               # a reject-merge'd pair never re-forms (§2.2)
            c = self._pool_by_id.get(cid)
            if c is None:
                continue
            if min(ev_ent, c["stmt_entropy"]) < self.h_min:
                continue                               # triviality gate: may SEED, never MERGE (§3.1 step 0)
            s = sig.jaccard(ev_sh, c["stmt_shingles"])
            if s >= self.j_maybe:
                scored.append((s, cid))
        scored.sort(key=lambda t: (-t[0], t[1]))
        residue = scored[:self.k_residue]
        if not residue:                                # the $0 mass: zero candidates or all NON-MATCH
            self._mint(rv, summary, ev_sh, ev_ent, ev_subj, root=root, run_id=run_id)
            return 1, 0.0
        if self.max_usd is not None and self._spent >= self.max_usd:
            self.n_deferred += 1                       # deferred, not broken: no verdict, no marker (§7.2)
            raise Deferred(f"residue call for {rv.id} deferred: spent ${self._spent:.4f} "
                           f">= cap ${self.max_usd:.4f}")
        shown = [self._pool_by_id[cid] for _, cid in residue]
        verdict, k, cost = adjudicate(rv, shown, self.complete_resolve)
        self._spent += cost
        self.n_residue_calls += 1
        if verdict == "none":                          # the stated default → mint
            self._mint(rv, summary, ev_sh, ev_ent, ev_subj, root=root, run_id=run_id)
            return 1, cost
        target = shown[k - 1]
        match = {"stmt_sim": round(residue[k - 1][0], 6), "subj": round(_subj_score(ev_subj, target), 4),
                 "by": "llm", "candidates_shown": [c["id"] for c in shown],
                 "prompt_version": PROMPT_VERSION, "model": self.model}
        verb = "corroborates" if verdict == "same-as" else "contradicts"
        write_edge(rv.id, verb, target["id"], session_id=rv.session_id, match=match,
                   root=root, run_id=run_id)                                        # edge FIRST
        if verb == "corroborates":
            self._fold_corroboration(target, rv, summary, ev_sh, ev_ent, ev_subj)
            self.n_corroborated += 1
        else:
            self._fold_contradiction(target, rv)
            self.n_contradicted += 1
        self.claims.append(target)
        _write_consolidated(rv.id, target["id"], verb, run_id=run_id, root=root)    # decision LAST
        return 1, cost

    def _mint(self, rv: dream.ResolvedEvent, summary: str, ev_sh: frozenset, ev_ent: float,
              ev_subj: dict, *, root: Path, run_id: str) -> dict:
        """MINT a claim NOW (§3.1 step 3): deterministic id (crash-retry re-mints the SAME id — a
        no-op-ish TimeMap version, never an orphan), title = the event summary, why = null (synthesize
        fills prose at maturity, §7.3), plus the seed corroborates edge. Commit order: claim → edge →
        consolidated LAST; then fold into the on-instance pool + indexes (read-your-writes)."""
        cid = dream.mint_takeaway_id(rv.id)
        content = {"id": cid, "title": summary, "why": None,
                   "relation": {"kind": "new", "concept_id": None, "note": ""},
                   "seed_event": rv.id, "born": config.now()}
        blobstore.ingest(blobstore.canonical_json(content), source_kind=CLAIM_KIND, source_id=cid,
                         origin_ref={"stage": "resolve", "model": self.model,
                                     "prompt_version": PROMPT_VERSION, "run_id": run_id},
                         root=root)                                                 # claim FIRST
        write_edge(rv.id, "corroborates", cid, session_id=rv.session_id,
                   match={"stmt_sim": None, "subj": None, "by": "seed", "candidates_shown": [],
                          "prompt_version": PROMPT_VERSION, "model": None},
                   root=root, run_id=run_id)                                        # edge SECOND
        view = {
            "id": cid, "title": summary, "why": None,
            "why_fingerprint": None, "why_stale": False,   # nothing synthesized yet (shape parity with the fold)
            "relation": content["relation"], "seed_event": rv.id, "born": content["born"],
            "cites": [rv.id], "evidence": [dream.evidence_entry(rv)],
            "support": {"events": 1, "sessions": 1 if rv.session_id else 0},
            "sessions_seen": [rv.session_id] if rv.session_id else [],
            "contradicted_by": [], "contradiction_evidence": [],
            "contradictions": {"events": 0, "sessions": 0},
            "markers": dream._event_markers(rv.event),
            "confidence": completer.clean_score(rv.event.get("confidence"), 0.5),
            "subject": {"repos": [ev_subj["repo"]] if ev_subj.get("repo") else [],
                        "files": sorted(ev_subj.get("files") or ())},
            "scope": "local", "stmt_shingles": frozenset(ev_sh) | sig.char_shingles(summary),
            "stmt_entropy": ev_ent, "_subj_keys": [ev_subj],
        }
        self._pool_by_id[cid] = view                   # the in-batch fold: next event reads this claim
        index_claim(self._idx, view)
        self.n_minted += 1
        self.claims.append(view)
        _write_consolidated(rv.id, cid, "seed", run_id=run_id, root=root)           # decision LAST
        return view

    def _fold_corroboration(self, view: dict, rv: dream.ResolvedEvent, summary: str,
                            ev_sh: frozenset, ev_ent: float, ev_subj: dict) -> None:
        """Fold a fresh corroboration into the ON-INSTANCE view + indexes — the same derivation
        `_fold_claim` would produce from the store, applied incrementally so the next event in the
        tick reads the updated signature/subject (read-your-writes)."""
        if rv.id not in view["cites"]:
            view["cites"].append(rv.id)
            view["evidence"].append(dream.evidence_entry(rv))
        if rv.session_id and rv.session_id not in view["sessions_seen"]:
            view["sessions_seen"].append(rv.session_id)
        view["support"] = {"events": len(set(view["cites"])), "sessions": len(set(view["sessions_seen"]))}
        new_sh = frozenset(ev_sh) - view["stmt_shingles"]
        view["stmt_shingles"] = view["stmt_shingles"] | new_sh
        for s in new_sh:
            self._idx["shingles"].setdefault(s, set()).add(view["id"])
        view["stmt_entropy"] = max(view["stmt_entropy"], ev_ent)
        view["_subj_keys"].append(ev_subj)
        view["subject"] = {"repos": sorted({k["repo"] for k in view["_subj_keys"] if k.get("repo")}),
                           "files": sorted({f for k in view["_subj_keys"] for f in (k.get("files") or ())})}
        view["scope"] = scope_of(view["_subj_keys"])
        for f in _claim_facets(view):
            self._idx["facets"].setdefault(f, set()).add(view["id"])
        em = dream._event_markers(rv.event)
        view["markers"] = {k: max(view["markers"][k], em[k]) for k in MARKER_KINDS}
        view["confidence"] = max(view["confidence"], completer.clean_score(rv.event.get("confidence"), 0.5))

    def _fold_contradiction(self, view: dict, rv: dream.ResolvedEvent) -> None:
        """Fold a contradiction into the on-instance view: the symmetric side only — support, title,
        signature ride untouched (ADR-0012; the negation cue never widens the merge basin)."""
        if rv.id in view["contradicted_by"]:
            return
        view["contradicted_by"].append(rv.id)
        entry = dream.evidence_entry(rv)
        entry["session_id"] = rv.session_id
        view["contradiction_evidence"].append(entry)
        view["contradictions"] = dream._contradiction_stats(view)

    def finalize(self, *, root: Path, run_id: str) -> None:
        if self.forget_on:
            self.staled = forget(root, tau=self.forget_tau, salience_floor=self.forget_floor,
                                 min_days=self.forget_min_days, run_id=run_id)


# --- run: the thin shim + Report wrapper (dream's pattern) -----------------------------------------

class ResolveReport(block.ProxyReport):
    """`resolve.run`'s report — the uniform driver Report plus resolve's cascade tallies, read
    THROUGH the block instance (never copied). NOTE: a budget DEFERRAL surfaces under the driver's
    `errored` (no marker → retried next tick, exactly the deferral contract) AND under `n_deferred`,
    which names it honestly."""

    @property
    def n_events(self) -> int:
        return self._blk.n_events
    @property
    def n_minted(self) -> int:
        return self._blk.n_minted
    @property
    def n_corroborated(self) -> int:
        return self._blk.n_corroborated
    @property
    def n_contradicted(self) -> int:
        return self._blk.n_contradicted
    @property
    def n_residue_calls(self) -> int:
        return self._blk.n_residue_calls
    @property
    def n_deferred(self) -> int:
        return self._blk.n_deferred
    @property
    def claims(self) -> list[dict]:
        return self._blk.claims
    @property
    def staled(self) -> list[str]:
        return self._blk.staled


def run(complete_resolve: Completer, *, model: str = RESOLVE_MODEL, min_confidence: float = 0.0,
        topic: str | None = None, maturity: float = dream.MATURITY_WEIGHT,
        j_maybe: float | None = None, h_min: float | None = None, k_residue: int = K_RESIDUE,
        k_rare: int = K_RARE, rare_min: int = RARE_MIN, facet_df_max: float = FACET_DF_MAX,
        active_floor: float = ACTIVE_FLOOR, active_days: float = ACTIVE_DAYS,
        forget: bool = True, max_usd: float | None = None, limit: int | None = None,
        priority: block.PriorityStrategy | None = None,
        progress: block.Progress | None = None, root: Path | None = None) -> ResolveReport:
    """Resolve the working set incrementally — a thin shim over `block.run(ResolveBlock(...))`.
    `max_usd` goes to the BLOCK (residue deferral, §7.2), never to the driver's break-on-budget:
    the tick always runs the full list, paying only what the cap allows."""
    blk = ResolveBlock(complete_resolve, model=model, min_confidence=min_confidence, topic=topic,
                       maturity=maturity, j_maybe=j_maybe, h_min=h_min, k_residue=k_residue,
                       k_rare=k_rare, rare_min=rare_min, facet_df_max=facet_df_max,
                       active_floor=active_floor, active_days=active_days, max_usd=max_usd,
                       forget=forget)
    report = block.run(blk, limit=limit, root=root, priority=priority, progress=progress)
    return ResolveReport(report, blk)


# --- CLI -------------------------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="resolve",
        description="dream v3 entity resolution: $0 rejection below J_MAYBE, ONE comparative-with-none "
                    "Haiku call on the residue, immediate mint of claim(why=null)+edge.")
    ap.add_argument("--model", default=RESOLVE_MODEL, help=f"residue adjudicator model (default: {RESOLVE_MODEL})")
    ap.add_argument("--min-confidence", type=float, default=0.0, help="ignore events below this glean confidence")
    ap.add_argument("--topic", help="PROCESSING FOCUS: resolve only events from a PROJECT whose name "
                    "contains this substring (case-insensitive; ADR-0022)")
    ap.add_argument("--maturity", type=float, default=dream.MATURITY_WEIGHT,
                    help="recency-weighted net-entrenchment bar a claim must cross to reach review "
                         "(the single bar, dream.MATURITY_WEIGHT — the reviewer's knob, ADR-0027)")
    ap.add_argument("--j-maybe", type=float, default=sig.J_MAYBE,
                    help=f"residue-band floor (default {sig.J_MAYBE}; below it a pair is non-match at $0)")
    ap.add_argument("--h-min", type=float, default=sig.H_MIN,
                    help=f"entropy floor — below it a statement seeds but never merges (default {sig.H_MIN})")
    ap.add_argument("--k-residue", type=int, default=K_RESIDUE,
                    help=f"candidates shown to the one residue call (default {K_RESIDUE}; overflow is "
                         f"simply not shown — abstention preserved)")
    ap.add_argument("--k-rare", type=int, default=K_RARE,
                    help=f"rarest pool-present shingles the statement channel queries (default {K_RARE})")
    ap.add_argument("--rare-min", type=int, default=RARE_MIN,
                    help=f"shared rare shingles that make a claim a candidate (default {RARE_MIN})")
    ap.add_argument("--facet-df-max", type=float, default=FACET_DF_MAX,
                    help=f"subject-facet devaluation: a facet on more than this fraction of active "
                         f"claims contributes nothing (default {FACET_DF_MAX})")
    ap.add_argument("--active-floor", type=float, default=ACTIVE_FLOOR,
                    help=f"ACTIVE-view entrenchment floor (default {ACTIVE_FLOOR})")
    ap.add_argument("--active-days", type=float, default=ACTIVE_DAYS,
                    help=f"ACTIVE-view recency arm in days (default {ACTIVE_DAYS})")
    ap.add_argument("--max-usd", type=float, help="residue-spend cap: paid calls DEFER past it "
                    "(retried next tick); $0 events always complete (§7.2)")
    ap.add_argument("--limit", type=int, help="cap events examined this run (the salience-ordered top)")
    ap.add_argument("--no-forget", action="store_true",
                    help="skip the straggler-eviction pass (the v2-reset drain runs with this ON "
                         "until pending hits 0, §7.3)")
    ap.add_argument("--reset-v2", action="store_true",
                    help="append-only migration: retire every live v2 takeaway + reopen every "
                         "consolidated event (idempotent; combine with --dry-run to preview)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the priority-ordered working set + pool sizes; no LLM calls, no writes")
    ap.add_argument("--show", action="store_true", help="print each minted/corroborated claim")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-event progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy over the working set (default: greedy = highest-salience first)")
    args = ap.parse_args(argv)

    if args.reset_v2:
        root = config.ensure_layout()
        retired, reopened = reset_v2(root, dry_run=args.dry_run)
        mode = "would retire" if args.dry_run else "retired"
        print(f"reset-v2: {mode} {len(retired)} v2 takeaway(s), "
              f"{'would reopen' if args.dry_run else 'reopened'} {len(reopened)} consolidated event(s)")
        if not args.dry_run and reopened:
            print("  drain the reopened backlog with `resolve --no-forget` until pending hits 0 (§7.3)")
        return

    if args.dry_run:                                   # eyeball the queue + pool before spending
        root = config.ensure_layout()
        ws = dream.working_set(root, min_confidence=args.min_confidence)
        if args.topic is not None:
            ws = dream.filter_by_topic(ws, args.topic, root)
        born = dream._event_born_map(root) if args.priority == "aging" else {}
        ws = block.priority_strategy(args.priority).order(
            ws, lambda rv: dream.salience(rv.event),
            lambda rv: config.age_days(born.get(rv.id)))
        pool = claim_pool(root)
        now = config.now()
        valid_times = dream._session_valid_times(root)
        active = [c for c in pool if is_active(c, now=now, valid_times=valid_times,
                                               floor=args.active_floor, days=args.active_days)]
        mature = [c for c in pool
                  if dream.net_entrenchment(c, now, valid_times=valid_times) >= args.maturity]
        print(f"{len(ws)} un-consolidated events ({args.priority}-ordered) · claim pool {len(pool)} "
              f"({len(active)} active, {len(mature)} mature):")
        for rv in ws[:40]:
            sample = rv.quote.strip().replace("\n", " ")
            print(f"  [{dream.salience(rv.event):.3f}] {rv.id[:12]}  {sample[:72]!r}")
        return

    complete_resolve = completer.make_cli_completer(args.model)
    progress = None if args.quiet else block.Progress(
        "resolve", cap=args.max_usd, params={"prompt_version": PROMPT_VERSION, "model": args.model},
        out_noun=OUT_NOUN, verbose=args.verbose)
    report = run(complete_resolve, model=args.model, min_confidence=args.min_confidence,
                 topic=args.topic, maturity=args.maturity, j_maybe=args.j_maybe, h_min=args.h_min,
                 k_residue=args.k_residue, k_rare=args.k_rare, rare_min=args.rare_min,
                 facet_df_max=args.facet_df_max, active_floor=args.active_floor,
                 active_days=args.active_days, forget=not args.no_forget, max_usd=args.max_usd,
                 limit=args.limit, priority=block.priority_strategy(args.priority),
                 progress=progress)
    if args.show:
        for c in report.claims:
            sup = c["support"]
            print(f"\n  • {c['title']}  [{c['scope']}, {sup['events']}ev/{sup['sessions']}sess]")
            if c.get("why"):
                print(f"    {c['why']}")
    errs = report.errored - report.n_deferred
    tail = f", {report.n_deferred} deferred" if report.n_deferred else ""
    tail += f", {errs} errored" if errs else ""
    print(f"\nresolve-{report.run_id}: {report.n_events} events → {report.n_minted} minted, "
          f"{report.n_corroborated} corroborated, {report.n_contradicted} contradicted, "
          f"{report.skipped} skipped, {len(report.staled)} staled{tail} · "
          f"{report.n_residue_calls} residue call(s), ${report.cost_usd:.4f}")


if __name__ == "__main__":
    main()
