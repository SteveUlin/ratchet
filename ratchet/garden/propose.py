"""garden.propose — the gardener, phase 2: the STRUCTURAL-OP PROPOSER (3c-ii, ADR-0016) + the
deterministic DECAY/STALENESS pass (3c-iii, ADR-0024).

`garden.ops` is the deterministic op machinery. This is the gardener's PHASE 2: a sharper model
(Sonnet) reads each high-tension concept CLUSTER (the 3a facet + 3b tag substrate) and PROPOSES
structural ops — merge / split / abstract / reparent / retire (+ tag merge/retire). This is the cascade
ONE LEVEL UP from dream's route/synth: the cheap pre-gate (3a/3b clustering) narrows the field to the
clusters worth a sharp look, then ONE sharp call per cluster proposes the edits — never a sharp call per
concept pair, never an embedding index.

The untrusted proposal is DEFENSIVELY COERCED (mirror dream's `_clean_route`): an op of an unknown kind,
citing a concept NOT in THIS cluster / not in the valid set, or malformed in shape, is DROPPED — never
acted on. The surviving ops route on `op_stakes` (the 3c-i fuzzy gradient): LOW-stakes edge/tag/reparent
ops AUTO-APPLY (call the 3c-i fn directly), HIGH-stakes (and the fuzzy MIDDLE — recall-first) ops are
QUEUED as append-only `garden_proposal` blobs for the 3d human gate. The op's RATIONALE is UNTRUSTED
(like dream's `why`): it rides into the proposal as the human-facing justification, never as a fact the
machine trusts — the faithfulness check belongs to 3d, not the proposer.

The DECAY/STALENESS sub-pass (3c-iii) rides alongside: a DETERMINISTIC, no-LLM scan that surfaces QUIET
concepts (un-corroborated past a disuse horizon) as `retire` proposals into the SAME tier-2 queue —
recall-first, never an auto-retire.
"""
from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path

from .. import blobstore, block, completer, concepts, config, dream, review
from ..completer import Completer
from . import slugify
from .ops import (
    AUTO_APPLY_MAX_STAKES, PROPOSE_PROMPT_VERSION, RESOLVE_VERBS, abstract, assert_edge, asserted_edges,
    merge, merge_tags, mint_proposal_id, op_stakes, open_proposals, queue_proposal, reparent, retire,
    retire_tag,
)

PROPOSE_MODEL = "sonnet"                  # the proposer is SHARP + RARE (one call per high-tension cluster)
PROPOSE_OUT_NOUN = "ops"                  # the per-cluster output noun the Progress bar/line shows

RATIONALE_MAX = 240                 # the proposer's one-line justification (UNTRUSTED — surfaced to 3d)
QUOTES_PER_CONCEPT = 2              # a FEW verified evidence quotes per concept ground the proposer's call
QUOTE_MAX = 160
OPS_PER_CLUSTER_MAX = 6             # a sharp call proposes a HANDFUL of ops per cluster; more is noise, capped
OP_TITLE_MAX = 80
OP_STATEMENT_MAX = 400

# tension weights — UNTUNED named constants (like the facet/tag weights, pending a gold set). Concept-level
# CONTRADICTION signals live on TAKEAWAYS (ADR-0012), not concepts, so the cluster cannot read them directly;
# tension proxies on the cheap structural signals the cluster DOES carry — DENSITY (size + how strongly the
# members relate) and the AMOUNT of related evidence.
W_TENSION_SIZE = 1.0       # density: more concepts in one cluster = more candidate merges/splits/abstractions
W_TENSION_COHESION = 0.25  # how strongly the members already relate (the facet-overlap mass that grouped them)
W_TENSION_EVIDENCE = 0.05  # the AMOUNT of related evidence backing the cluster (material the gardener acts on)

# the op vocabulary the proposer may emit → its `op_stakes` key (only `relate` differs: a relates-to edge is
# `assert_edge` on the gradient; every other op name IS its stakes key). An op outside this set is dropped.
PROPOSE_OPS = ("merge", "split", "abstract", "reparent", "retire", "relate", "merge_tags", "retire_tag")
_STAKES_KEY = {"relate": "assert_edge"}

PROPOSE_SYSTEM = (
    "You are the GARDENER for a developer's long-term memory. Each CONCEPT is a durable, human-reviewed "
    "lesson. You are shown ONE CLUSTER of concepts the cheap layer grouped as related (they share files / "
    "repos / tools, or a theme tag), each with its title, statement, provenance, tags, a few VERIFIED "
    "evidence quotes, and any relations already asserted among them. Propose STRUCTURAL EDITS that make the "
    "concept layer cleaner:\n"
    "  - merge: two+ concepts state the SAME lesson — fold the losers into one winner.\n"
    "  - split: one concept conflates TWO distinct lessons — divide it into parts.\n"
    "  - abstract: several concepts share a more GENERAL parent idea — name it.\n"
    "  - reparent: a concept belongs under a different generalization parent.\n"
    "  - retire: a concept is stale, wrong, or fully subsumed — take it out of the valid set.\n"
    "  - relate: two concepts are associated (a relates-to link), short of a merge.\n"
    "  - merge_tags / retire_tag: two theme tags duplicate, or one is dead — fold/drop it.\n"
    "Propose ONLY edits you are confident about, each with a ONE-LINE rationale and the concept ids it "
    "touches. Cite ONLY ids shown in THIS cluster. A clean cluster may need NO edits — return an empty list.\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{"ops": [\n'
    '  {"op": "merge", "winner_id": "c-…", "loser_ids": ["c-…"], "rationale": "…"},\n'
    '  {"op": "split", "concept_id": "c-…", "parts": [{"title": "…", "statement": "…"}, …], "rationale": "…"},\n'
    '  {"op": "abstract", "child_ids": ["c-…", "c-…"], "title": "…", "statement": "…", "rationale": "…"},\n'
    '  {"op": "reparent", "concept_id": "c-…", "parent_id": "c-…", "rationale": "…"},\n'
    '  {"op": "retire", "concept_id": "c-…", "rationale": "…"},\n'
    '  {"op": "relate", "src": "c-…", "dst": "c-…", "rationale": "…"},\n'
    '  {"op": "merge_tags", "loser_slug": "…", "winner_slug": "…", "rationale": "…"},\n'
    '  {"op": "retire_tag", "slug": "…", "rationale": "…"}\n'
    "]}"
)


# --- the cluster's tension: the priority SIGNAL (highest-tension first, ADR-0011) ------------------

def cluster_tension(cluster: dict, blob_by_id: dict, facets_by_id: dict) -> float:
    """The priority SIGNAL — how much a sharp pass over this cluster buys us (ADR-0011's modular knob;
    `GardenProposeBlock.priority` delegates here, the driver's Greedy policy sorts highest-first). A DENSE
    cluster of well-related concepts backed by lots of evidence is the most worth gardening: it holds the
    most candidate merges/splits/abstractions. Pure + deterministic — size, the members' pairwise
    facet-overlap mass (the same `facet_score` that grouped them), and the total cited-evidence count.
    UNTUNED, like the facet/tag weights."""
    members = [m for m in cluster["members"] if m in blob_by_id]
    size = len(members)
    cohesion = sum(concepts.facet_score(facets_by_id.get(a, {}), facets_by_id.get(b, {}))
                   for a, b in combinations(members, 2))
    evidence_mass = sum(len(blob_by_id[m].get("evidence") or []) for m in members)
    return W_TENSION_SIZE * size + W_TENSION_COHESION * cohesion + W_TENSION_EVIDENCE * evidence_mass


# --- the sharp call: render the cluster → propose ops → DEFENSIVELY coerce -------------------------

def _facet_ctx(f: dict | None) -> str:
    f = f or {}
    return (f"repos={f.get('repos', [])} files={f.get('files', [])} "
            f"tools={f.get('tools', [])} tags={f.get('tags', [])}")


def _verified_quotes(blob: dict, root: Path, *, limit: int = QUOTES_PER_CONCEPT,
                     maxlen: int = QUOTE_MAX) -> list[str]:
    """A FEW VERIFIED evidence quotes for a concept — `review.resolve_evidence` re-validates each span at the
    read boundary, so the proposer reads the trusted verbatim bytes (not the takeaway's claimed quote). The
    proposer's grounding, the same trust anchor review serves the human."""
    out: list[str] = []
    for e in review.resolve_evidence({"evidence": blob.get("evidence") or []}, root):
        q = " ".join(str(e.get("quote", "")).split())[:maxlen]
        if q:
            out.append(q)
        if len(out) >= limit:
            break
    return out


def _propose_user(members: list[str], blob_by_id: dict, facets_by_id: dict, quotes: dict,
                  among: list[dict], cluster_tags: set[str]) -> str:
    lines = ["CLUSTER (related concepts):"]
    for cid in members:
        b = blob_by_id[cid]
        lines.append(f"- id {cid}: {str(b.get('title', '')).strip()!r}")
        lines.append(f"    statement: {str(b.get('statement', '')).strip()[:OP_STATEMENT_MAX]!r}")
        lines.append(f"    provenance: {_facet_ctx(facets_by_id.get(cid))}")
        qs = quotes.get(cid) or []
        if qs:
            lines.append("    evidence: " + " | ".join(f"{q!r}" for q in qs))
    if among:
        lines.append("\nASSERTED RELATIONS among these concepts:")
        for e in among:
            lines.append(f"- {e['src']} —{e['kind']}→ {e['dst']}"
                         + (f"  ({e['note']})" if e.get("note") else ""))
    if cluster_tags:
        lines.append(f"\nTHEME TAGS in this cluster: {sorted(cluster_tags)}")
    return "\n".join(lines)


def _clean_op(raw, *, member_ids: set[str], valid_ids: set[str], cluster_tags: set[str]) -> dict | None:
    """Coerce ONE untrusted proposed op (mirror `_clean_route`/`_clean_relation`): DROP it unless its kind is
    known, its shape is well-formed, and EVERY concept id it cites is BOTH in this cluster AND valid — never
    act on an id the model invented out of nothing, nor one outside the cluster it was actually shown. Tag
    ops validate their slugs against THIS cluster's tag set. The rationale is coerced to a short string,
    UNTRUSTED — carried to 3d as provenance, never trusted here. Returns {op, params, concept_ids, rationale}
    or None (dropped)."""
    if not isinstance(raw, dict):
        return None
    op = raw.get("op")
    if op not in PROPOSE_OPS:
        return None
    rationale = str(raw.get("rationale", "")).strip()[:RATIONALE_MAX]

    def cid(x):                              # a cited concept id is kept ONLY if in-cluster AND valid
        return x if isinstance(x, str) and x in member_ids and x in valid_ids else None

    if op == "merge":
        winner = cid(raw.get("winner_id"))
        losers: list[str] = []
        for c in raw.get("loser_ids") or []:
            c = cid(c)
            if c and c != winner and c not in losers:
                losers.append(c)
        if not winner or not losers:
            return None
        params = {"winner_id": winner, "loser_ids": sorted(losers)}
        concept_ids = sorted({winner, *losers})
    elif op == "split":
        target = cid(raw.get("concept_id"))
        parts: list[dict] = []
        for p in raw.get("parts") or []:
            if isinstance(p, dict) and str(p.get("title", "")).strip():
                parts.append({"title": str(p.get("title", "")).strip()[:OP_TITLE_MAX],
                              "statement": str(p.get("statement", "")).strip()[:OP_STATEMENT_MAX]})
        if not target or len(parts) < 2:     # a split needs a target + at least two parts (else it's a no-op)
            return None
        params = {"concept_id": target, "parts": parts}
        concept_ids = [target]
    elif op == "abstract":
        children: list[str] = []
        for c in raw.get("child_ids") or []:
            c = cid(c)
            if c and c not in children:
                children.append(c)
        title = str(raw.get("title", "")).strip()
        if len(children) < 2 or not title:   # a generalization spans at least two children + names itself
            return None
        params = {"child_ids": sorted(children), "title": title[:OP_TITLE_MAX],
                  "statement": str(raw.get("statement", "")).strip()[:OP_STATEMENT_MAX]}
        concept_ids = sorted(children)
    elif op == "reparent":
        child = cid(raw.get("concept_id"))
        parent = cid(raw.get("parent_id"))
        if not child or not parent or child == parent:
            return None
        params = {"concept_id": child, "parent_id": parent}
        concept_ids = sorted({child, parent})
    elif op == "retire":
        target = cid(raw.get("concept_id"))
        if not target:
            return None
        params = {"concept_id": target}
        concept_ids = [target]
    elif op == "relate":
        a, b = cid(raw.get("src")), cid(raw.get("dst"))
        if not a or not b or a == b:
            return None
        params = {"src": a, "dst": b}
        concept_ids = sorted({a, b})
    elif op == "merge_tags":
        loser, winner = slugify(raw.get("loser_slug")), slugify(raw.get("winner_slug"))
        if (not loser or not winner or loser == winner
                or loser not in cluster_tags or winner not in cluster_tags):
            return None
        params = {"loser_slug": loser, "winner_slug": winner}
        concept_ids = []
    else:                                    # retire_tag
        slug = slugify(raw.get("slug"))
        if not slug or slug not in cluster_tags:
            return None
        params = {"slug": slug}
        concept_ids = []
    return {"op": op, "params": params, "concept_ids": concept_ids, "rationale": rationale}


def propose_ops(cluster: dict, propose: Completer, *, blob_by_id: dict, facets_by_id: dict,
                asserted: list[dict], valid_ids: set[str], root: Path) -> tuple[list[dict], float]:
    """ONE sharp call over the cluster (id + title + statement + facets + tags + a few VERIFIED quotes + the
    asserted edges among them) → the coerced, deduped, capped list of surviving op descriptors + cost. A
    raised proposer propagates — the driver isolates the cluster as errored (no marker → retried next run),
    exactly like dream's route. Pure-injectable: offline-tested with a fake that echoes scripted ops."""
    members = [m for m in cluster["members"] if m in blob_by_id]
    member_ids = set(members)
    cluster_tags: set[str] = set()
    for m in members:
        cluster_tags |= set(facets_by_id.get(m, {}).get("tags", []))
    quotes = {m: _verified_quotes(blob_by_id[m], root) for m in members}
    among = [e for e in asserted if e["src"] in member_ids and e["dst"] in member_ids]
    comp = propose(PROPOSE_SYSTEM, _propose_user(members, blob_by_id, facets_by_id, quotes, among, cluster_tags))
    cost = completer.cost_of(comp)
    parsed = completer.parse_json_object(comp.text) or {}
    raw_ops = parsed.get("ops")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_ops if isinstance(raw_ops, list) else []:
        desc = _clean_op(raw, member_ids=member_ids, valid_ids=valid_ids, cluster_tags=cluster_tags)
        if desc is None:
            continue
        pid = mint_proposal_id(desc)
        if pid in seen:                      # the SAME structural edit proposed twice in one call → once
            continue
        seen.add(pid)
        out.append(desc)
        if len(out) >= OPS_PER_CLUSTER_MAX:
            break
    return out, cost


# --- route on stakes: auto-apply LOW, queue HIGH as an append-only proposal ------------------------

def _op_stakes_of(desc: dict) -> float:
    """The op's stakes on the 3c-i `op_stakes` gradient — keyed by the descriptor's stakes-name (`relate` →
    `assert_edge`), with the breadth fields passed through so a wider op (more losers/children/parts) scores
    a touch higher. ONE source for the routing decision — `op_stakes` (ADR-0015), never a re-derived policy."""
    probe: dict = {"op": _STAKES_KEY.get(desc["op"], desc["op"])}
    p = desc.get("params") or {}
    for k in ("loser_ids", "child_ids", "parts"):
        if isinstance(p.get(k), list):
            probe[k] = p[k]
    return op_stakes(probe)


# ==================================================================================================
# DECAY / STALENESS (3c-iii, ADR-0024): a DETERMINISTIC pass that surfaces QUIET concepts for review
#
# WEAKEN (ADR-0012) handles a concept you MOVED ON from — a contradiction demotes it. But a concept that
# simply goes QUIET — a topic untouched for months, never contradicted, just un-corroborated — has no
# signal demoting it today. Yet the signal EXISTS: `now − the most-recent valid-time among its evidence`.
# A concept whose newest backing conversation is many months old has gone stale by DISUSE, not by conflict.
#
# RECALL-FIRST, the non-negotiable: staleness only ever PROPOSES a retire — it NEVER auto-retires. A quiet
# concept is QUEUED as a `retire` proposal for the SAME tier-2 gate the LLM proposer feeds (ADR-0016/0017),
# and the human decides: reject the proposal → kept + SUPPRESSED (won't re-nag — `queue_proposal` remembers
# the verdict), accept → the trusted 3c-i `retire` fires. Nothing fades on its own for going quiet.
#
# The SELF-CLEARING property (free, ADR-0023's ethos): a still-true preference keeps being RE-LIVED — every
# fresh session re-corroborates it, advancing its last-corroborated valid-time, so it never goes stale. Only
# a genuinely-untouched concept ages past the horizon. No timeless-vs-changing classification is needed;
# disuse sorts itself, exactly as it does for the recency-weighted takeaway gate.
#
# RECOMPUTE-ON-READ (the facet/recency ethos, ADR-0013/0023): last-corroborated is NEVER stored on the
# concept — it recomputes from the evidence's sessions' valid-times on every read, exactly as
# `net_entrenchment` recomputes its weights from session ids. So a concept re-corroborated tomorrow is no
# longer stale with NO field to desync.
#
# The third of the TEMPORAL TRIO (ADR-0023 named the gap): Aging (ADR-0021) ages the BACKLOG up so a starved
# item still runs (fairness of attention); recency-trust (ADR-0023) WEIGHTS evidence down by valid-time so a
# stale backfill can't re-entrench (trust in a belief); this flags a QUIET concept down for re-confirmation
# (liveness of a concept). They share the `age_days` primitive and nothing else.
# ==================================================================================================

STALENESS_DAYS = 270   # UNTUNED — the disuse horizon: a concept un-corroborated for longer than this surfaces
                       # for re-confirmation. A months scale (~9 months) — long enough that an active preference,
                       # RE-LIVED across sessions, never trips it, yet short enough that a genuinely-abandoned
                       # topic surfaces within a year. Wants a gold set like every weight here
                       # (RECENCY_HALF_LIFE_DAYS, the maturity bar); ONE edit retunes. The recall-first gate
                       # (PROPOSE, never auto-retire) makes a too-LOW value cost only review attention, never a
                       # lost concept — so it errs generous rather than aggressive.

STALE_MODEL = "deterministic"   # the staleness pass calls NO model; recorded in the proposal's origin_ref so
                                # provenance stays HONEST — a `retire` proposal sourced from DISUSE, not a Sonnet
                                # judgment over a cluster (a human reading the queue can tell the two apart).


def concept_last_corroborated(concept: dict, valid_times: dict, root: Path | None = None, *,
                              sessions: dict | None = None, now: str | None = None) -> str | None:
    """The MOST-RECENT valid-time among a concept's evidence's sessions — when this concept was last RE-LIVED
    (ADR-0024). The hop is the trust chain's, read-side: each evidence pointer's `cleaned_hash` →
    `blobstore.session_of` (cleaned → raw → session id) → `valid_times[sid]` (the session's date, via
    `dream._session_valid_times`, recompute-on-read). Returns the newest such valid-time, or None when NO
    evidence dates — recall-safe: an undateable concept is treated as FRESH downstream (never proposed for
    retire on a date we cannot read), mirroring `recency_weight`'s missing-date → 1.0. `now` only pins the
    ordering reference (the RESULT is now-invariant — a constant offset cancels); threaded for one-clock
    consistency with the staleness gate. `sessions` is an optional `session_of` cache shared across concepts."""
    root = root or config.data_root()
    if sessions is None:
        sessions = {}
    best_vt: str | None = None
    best_age: float | None = None
    for ev in concept.get("evidence") or []:
        ch = ev.get("cleaned_hash") if isinstance(ev, dict) else None
        if not ch:
            continue
        vt = valid_times.get(blobstore.session_of(ch, root, sessions))
        if not vt:
            continue
        age = config.age_days(vt, now=now)              # smaller age = more recent; keep the newest
        if best_age is None or age < best_age:
            best_age, best_vt = age, vt
    return best_vt


def stale_concepts(root: Path | None = None, *, days: float = STALENESS_DAYS,
                   now: str | None = None) -> list[dict]:
    """The QUIET concepts — valid concepts whose last-corroborated valid-time is more than `days` old
    (ADR-0024). RECOMPUTE-ON-READ: the session valid-times are folded ONCE (`dream._session_valid_times`) and
    `now` is pinned ONCE, so every concept ages against the same clock (deterministic when a test injects
    `now`). A concept with NO datable evidence is SKIPPED (treated fresh — recall-safe). Returns
    [{concept, last_corroborated, age_days}] — the substrate `propose_stale` queues retire proposals from."""
    root = root or config.data_root()
    valid_times = dream._session_valid_times(root)
    sessions: dict = {}
    out: list[dict] = []
    for concept in dream.load_concepts(root):
        last = concept_last_corroborated(concept, valid_times, root, sessions=sessions, now=now)
        if last is None:
            continue                                    # undateable → fresh; never flag what we cannot date
        age = config.age_days(last, now=now)
        if age > days:
            out.append({"concept": concept, "last_corroborated": last, "age_days": age})
    return out


def _stale_retire_desc(concept_id: str, last_corroborated: str, *, days: float) -> dict:
    """The deterministic `retire`-stale op descriptor for one quiet concept — the SAME shape `_clean_op` emits,
    so it rides `queue_proposal`/`op_stakes`/the tier-2 gate UNCHANGED. The rationale is anchored on the STABLE
    last-corroborated DATE + the threshold (NOT the live age), so a re-run while the proposal is still open is a
    byte-identical no-op: the age is recompute-on-read, never frozen into the blob (the ADR-0023/0024 ethos — an
    age in the bytes would churn a fresh version every day the concept stays quiet)."""
    since = str(last_corroborated)[:10]                 # the date portion — the stable, byte-identical fact
    rationale = (f"stale: untouched since {since} — no corroboration in over {int(days)}d. "
                 f"Re-confirm or retire?")[:RATIONALE_MAX]
    return {"op": "retire", "params": {"concept_id": concept_id},
            "concept_ids": [concept_id], "rationale": rationale}


def propose_stale(root: Path | None = None, *, days: float = STALENESS_DAYS, now: str | None = None,
                  run_id: str | None = None, model: str = STALE_MODEL) -> list[dict]:
    """The DETERMINISTIC staleness pass — NO LLM (ADR-0024). For each stale concept, QUEUE a `retire`-stale
    `garden_proposal` riding the EXISTING tier-2 flow: `op_stakes("retire")` is HIGH (0.80), so it always
    QUEUES for the 3d human gate, never auto-applies (recall-first — a quiet concept is only ever PROPOSED).
    `mint_proposal_id` keys on the op IDENTITY ({retire, concept_id}) — so a re-run re-versions the SAME
    proposal (no duplicate; and it UNIFIES with an LLM-proposed retire of the same concept), and
    `queue_proposal`'s reject-SUPPRESSION holds: once the human rejects (or accepts) it, a re-run leaves it
    standing — won't re-nag. A concept later RE-CORROBORATED simply drops out of `stale_concepts`
    (self-clearing), so nothing is queued for it at all. Deterministic + no-LLM → cheap enough to run every
    `garden propose`. Returns one record per stale concept: {concept_id, proposal_id, last_corroborated,
    age_days, suppressed} (suppressed = already resolved → left standing, not re-queued)."""
    root = root or config.data_root()
    run_id = run_id or config.run_id()
    out: list[dict] = []
    for s in stale_concepts(root, days=days, now=now):
        cid = s["concept"]["id"]
        desc = _stale_retire_desc(cid, s["last_corroborated"], days=days)
        pid = mint_proposal_id(desc)
        decided = blobstore.latest_decision(pid, root)           # the 3d verdict if any (reject/accept)
        suppressed = bool(decided and decided.get("verb") in RESOLVE_VERBS)
        queue_proposal(desc, cluster_leader=cid, stakes=op_stakes("retire"),      # a no-op when suppressed
                       root=root, run_id=run_id, model=model)                     # (it reads the same decision)
        out.append({"concept_id": cid, "proposal_id": pid, "last_corroborated": s["last_corroborated"],
                    "age_days": round(s["age_days"], 1), "suppressed": suppressed})
    return out


# --- route on stakes: auto-apply LOW, queue HIGH for 3d -------------------------------------------

# the ops `_apply_op` can auto-apply directly — every op the proposer emits EXCEPT `split` (whose per-part
# EVIDENCE PARTITION is the 3d/human's to choose, never the machine's to guess). `process` auto-applies an op
# ONLY when it is BOTH low-stakes AND in this set; anything else QUEUES unconditionally, so a non-auto-applicable
# op — split, or a future kind `_apply_op` does not handle — at a deliberately-raised `--auto-max-stakes` is
# QUEUED for 3d, never stranded on `_apply_op`'s raise (N1). Keep in lockstep with `_apply_op`'s branches.
AUTO_APPLICABLE_OPS = frozenset({"merge", "abstract", "reparent", "retire", "relate", "merge_tags", "retire_tag"})


def _apply_op(desc: dict, *, root: Path, run_id: str) -> None:
    """AUTO-APPLY one op by calling its 3c-i fn directly (the trusted, append-only machinery). `process` routes
    here ONLY ops in `AUTO_APPLICABLE_OPS`, and only when low-stakes — so under the default threshold just the
    edge/tag/reparent ops land, while a deliberately-raised threshold lets the concept-altering branches
    (merge/abstract/retire) fire too (all still wired). An auto-applied edge/curation carries the rationale as
    its NOTE (provenance)."""
    op, p, note = desc["op"], desc["params"], desc.get("rationale", "")
    if op == "relate":
        assert_edge(p["src"], "relates-to", p["dst"], note=note, root=root, run_id=run_id, op="propose")
    elif op == "reparent":
        reparent(p["concept_id"], p["parent_id"], root=root, run_id=run_id)
    elif op == "merge_tags":
        merge_tags(p["loser_slug"], p["winner_slug"], note=note, root=root, run_id=run_id)
    elif op == "retire_tag":
        retire_tag(p["slug"], note=note, root=root, run_id=run_id)
    elif op == "merge":
        merge(p["loser_ids"], p["winner_id"], root=root, run_id=run_id)
    elif op == "abstract":
        abstract(p["child_ids"], p["title"], p["statement"], root=root, run_id=run_id)
    elif op == "retire":
        retire(p["concept_id"], reason=note, root=root)
    else:                                    # UNREACHABLE: `process` routes split (and any non-auto-applicable
        # op) straight to the 3d queue, so this guards the AUTO_APPLICABLE_OPS invariant rather than handling a
        # live case — a split's per-part evidence partition is the human's, never auto-guessed here.
        raise AssertionError(f"op {op!r} reached _apply_op but is not auto-applicable — process must queue it")


# --- the Block: ONE sharp proposer call per high-tension cluster, per-cluster commit ---------------

class GardenProposeBlock:
    """The gardener's structural-ops phase as a `block.Block` (ADR-0009): a SHARP proposer over the
    high-tension concept clusters, committing PER CLUSTER. `items()` loads the cluster view + the per-concept
    facets/blobs/edges ONCE (one `concept_graph` pass), then yields the clusters of size >= `min_cluster`;
    the driver sorts them by `priority` (cluster TENSION) and caps by --limit. `process()` runs ONE proposer
    call, coerces the proposal, and routes each surviving op on `op_stakes`: LOW → auto-apply (a 3c-i fn),
    HIGH / fuzzy-middle → QUEUE a `garden_proposal` for 3d. The driver writes the per-cluster `processed`
    marker LAST (resumable, fail-in-the-middle); a cluster already gardened against the CURRENT prompt/model is
    done-skipped — `params` carries the idempotency key, exactly like the tagger Block.

    The done-skip keys on the cluster LEADER (`key`), which is a RECALL TRADE, not just a cost saver: a concept
    that JOINS an existing-leader cluster is not re-proposed against its new neighbours until a prompt bump or a
    leader change. A deliberate low-churn choice — one Sonnet call per cluster is expensive, unlike 3b's Haiku
    tagging — and re-gardening is anyway SAFE to skip (an auto-applied op is idempotent, a queued proposal
    re-versions its deterministic id, so no duplicate or corruption). The stronger-recall alternative — key the
    marker on a sorted-members fingerprint — is deferred until recall proves insufficient (ADR-0016)."""

    name = "garden_ops"
    commits_per_item = True
    finalize = block.no_finalize
    marker_extra = block.no_marker_extra
    age = block.no_age                       # garden's backlog is bounded-SMALL (dozens–hundreds of
                                             # concepts/clusters, not a months-long transcript backlog), so
                                             # Greedy drains it before starvation bites — aging deferred, a
                                             # one-line `def age` opt-in if it ever does (ADR-0021)

    def __init__(self, propose: Completer, *, model: str = PROPOSE_MODEL,
                 auto_max_stakes: float = AUTO_APPLY_MAX_STAKES, min_cluster: int = 2,
                 root: Path | None = None) -> None:
        self.propose = propose
        self.model = model
        self.auto_max_stakes = auto_max_stakes
        self.min_cluster = min_cluster
        self.root = root or config.data_root()
        # the done-key suffix: a cluster is done for (leader, PROMPT_VERSION, model).
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROPOSE_PROMPT_VERSION), ("model", model))
        # the index loaded ONCE in items() (the run's constant view), read by priority + process.
        self._blob_by_id: dict[str, dict] = {}
        self._facets_by_id: dict[str, dict] = {}
        self._valid: set[str] = set()
        self._asserted: list[dict] = []
        # run-total tallies (instance-scoped; the uniform Report stays stage-agnostic).
        self.n_clusters = 0
        self.n_proposed = 0                             # surviving ops (auto-applied + queued)
        self.n_applied = 0                              # auto-applied (low-stakes) ops
        self.n_queued = 0                               # proposals queued for the 3d gate
        self.applied: list[dict] = []                   # [{op, params, concept_ids, rationale, stakes}]
        self.proposals: list[dict] = []                 # the queued proposal contents (for --show / tests)

    def items(self, root: Path, *, source_id: str | None = None):
        """The concept CLUSTERS (3a facets + 3b tags), frozen at run start — `concept_graph` gives the
        clusters AND the per-node facets in one pass; `load_concepts` the full blobs (statement/evidence).
        `source_id` is ignored (the gardener is a global pass). Only clusters of >= `min_cluster` concepts are
        worth a sharp call (a singleton has nothing to merge/relate)."""
        graph = concepts.concept_graph(root)
        self._blob_by_id = {c["id"]: c for c in dream.load_concepts(root)}
        self._valid = set(self._blob_by_id)
        self._facets_by_id = {n["id"]: n["facets"] for n in graph["nodes"]}
        self._asserted = asserted_edges(root)
        clusters = [cl for cl in graph["clusters"] if len(cl["members"]) >= self.min_cluster]
        self.n_clusters = len(clusters)
        return clusters

    def key(self, cluster: dict) -> str:
        # the cluster's stable id (a valid concept id). Keying the done-skip on the LEADER is a RECALL trade: a
        # cluster that gains a member without changing leader is not re-proposed until a prompt/leader change.
        return cluster["leader"]

    def priority(self, cluster: dict) -> float:
        """Highest-TENSION cluster first (ADR-0011's modular signal) — see `cluster_tension`."""
        return cluster_tension(cluster, self._blob_by_id, self._facets_by_id)

    def process(self, cluster: dict, *, root: Path, run_id: str) -> tuple[int, float]:
        """ONE proposer call → coerce → route each surviving op on `op_stakes`. An op AUTO-APPLIES (a 3c-i fn)
        ONLY when it is BOTH low-stakes AND auto-applicable; everything else — a HIGH / fuzzy-middle op, OR a
        non-auto-applicable one (`split`) even at a raised threshold — QUEUES a `garden_proposal` for 3d
        (recall-first). Returns (n_outputs, cost) for the driver's budget gate; the marker is written LAST."""
        ops, cost = propose_ops(cluster, self.propose, blob_by_id=self._blob_by_id,
                                facets_by_id=self._facets_by_id, asserted=self._asserted,
                                valid_ids=self._valid, root=root)
        n_out = 0
        for desc in ops:
            stakes = _op_stakes_of(desc)
            # AUTO-APPLY needs BOTH gates: low-stakes AND a kind `_apply_op` handles. A `split` (or any future
            # non-auto-applicable op) thus QUEUES regardless of --auto-max-stakes — never routed to `_apply_op`'s
            # raise, so it can't be stranded (neither applied nor queued) at a manually-raised threshold (N1).
            if stakes <= self.auto_max_stakes and desc["op"] in AUTO_APPLICABLE_OPS:
                _apply_op(desc, root=root, run_id=run_id)
                self.n_applied += 1
                self.applied.append({**desc, "stakes": round(stakes, 4)})
            else:                                        # HIGH / fuzzy-middle / non-auto-applicable → QUEUE for 3d
                content, _ = queue_proposal(desc, cluster_leader=cluster["leader"], stakes=stakes,
                                            root=root, run_id=run_id, model=self.model)
                self.n_queued += 1
                self.proposals.append(content)
            n_out += 1
        self.n_proposed += n_out
        return n_out, cost


# --- run: a thin compat shim over the block driver (mirrors GardenBlock/dream) ---------------------

class _OpsReport(block.ProxyReport):
    """The shape `run_propose` returns — a thin WRAPPER over the uniform `block.Report` the driver populated
    plus the GardenProposeBlock instance, exposing every field by reading THROUGH them (no copy → no desync,
    like GardenBlock's `_ShimReport`). The uniform fields proxy the Report (the `block.ProxyReport` base)."""
    @property
    def n_clusters(self) -> int:
        return self._blk.n_clusters
    @property
    def n_proposed(self) -> int:
        return self._blk.n_proposed
    @property
    def n_applied(self) -> int:
        return self._blk.n_applied
    @property
    def n_queued(self) -> int:
        return self._blk.n_queued
    @property
    def applied(self) -> list[dict]:
        return self._blk.applied
    @property
    def proposals(self) -> list[dict]:
        return self._blk.proposals


def run_propose(propose: Completer, *, model: str = PROPOSE_MODEL,
                auto_max_stakes: float = AUTO_APPLY_MAX_STAKES, max_usd: float | None = None,
                limit: int | None = None, priority: block.PriorityStrategy | None = None,
                progress: block.Progress | None = None, root: Path | None = None) -> _OpsReport:
    """Propose structural ops over the high-tension clusters — a thin shim over `block.run(GardenProposeBlock(...))`
    (mirrors GardenBlock/dream). The root is resolved ONCE and handed to BOTH the block and the driver. The
    sharp `propose` Completer is injected (Sonnet by default), offline-testable with a fake. `progress`
    defaults to None (silent)."""
    root = config.ensure_layout(root)
    blk = GardenProposeBlock(propose, model=model, auto_max_stakes=auto_max_stakes, root=root)
    report = block.run(blk, max_usd=max_usd, limit=limit, root=root, priority=priority, progress=progress)
    return _OpsReport(report, blk)


# --- CLI: the gardener's phase-2 surface (mirrors GardenBlock.main / dream) ------------------------

def propose_main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="garden propose",
        description="Propose structural ops over high-tension concept clusters: a sharp model proposes "
                    "merge/split/abstract/reparent/retire (+ tag curation); low-stakes auto-apply, "
                    "high-stakes queue for the 3d human gate (LLM). A DETERMINISTIC staleness sub-pass "
                    "(no LLM) rides alongside: quiet, un-corroborated concepts surface a retire proposal.")
    ap.add_argument("--model", default=PROPOSE_MODEL,
                    help=f"claude model for the proposer (default: {PROPOSE_MODEL})")
    ap.add_argument("--limit", type=int, help="cap clusters examined this run (the tension-ordered top)")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this (between clusters)")
    ap.add_argument("--auto-max-stakes", type=float, default=AUTO_APPLY_MAX_STAKES,
                    help="op_stakes at/below which an op auto-applies; above it QUEUES for 3d "
                         f"(default {AUTO_APPLY_MAX_STAKES}; recall-first)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the clusters that would be gardened (skips done); no LLM calls")
    ap.add_argument("--show", action="store_true", help="print each applied op + each queued proposal")
    ap.add_argument("--proposals", action="store_true",
                    help="print the pending proposal queue (the open 3d backlog) and exit")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-cluster progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy over the clusters (default: greedy = highest-tension first)")
    # the DETERMINISTIC decay/staleness sub-pass (ADR-0024) — quiet concepts → retire proposals, no LLM.
    ap.add_argument("--no-stale", action="store_true",
                    help="skip the deterministic staleness sub-pass (run only the LLM cluster proposer)")
    ap.add_argument("--stale-only", action="store_true",
                    help="run ONLY the staleness sub-pass — quiet concepts → retire proposals (no LLM, no key)")
    ap.add_argument("--stale-days", type=int, default=STALENESS_DAYS,
                    help=f"the disuse horizon: a concept un-corroborated longer than this surfaces a retire "
                         f"proposal (default {STALENESS_DAYS}; untuned, recall-first)")
    args = ap.parse_args(argv)

    if args.proposals:                                  # just inspect the open 3d queue
        q = open_proposals()
        print(f"{len(q)} pending proposal(s) queued for the 3d gate:")
        for p in q:
            print(f"  {p['proposal_id']}  [{p['op']} · stakes {p['stakes']:.2f}]  {p['concept_ids']}")
            print(f"      {p['rationale']}")
        return

    if args.stale_only:                                 # the deterministic pass ALONE — no completer needed
        if args.dry_run:                                # preview the quiet concepts; queue nothing
            sc = stale_concepts(days=args.stale_days)
            print(f"staleness (dry-run): {len(sc)} quiet concept(s) past {args.stale_days}d would surface a "
                  f"retire proposal.")
            for s in sc:
                print(f"  {s['concept']['id']}  last corroborated {str(s['last_corroborated'])[:10]}")
            return
        _print_stale(propose_stale(days=args.stale_days), days=args.stale_days)
        return

    propose = completer.make_cli_completer(args.model)
    blk = GardenProposeBlock(propose, model=args.model, auto_max_stakes=args.auto_max_stakes)
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, cap=args.max_usd, params=dict(blk.params), out_noun=PROPOSE_OUT_NOUN, verbose=args.verbose)
    report = block.run(blk, max_usd=args.max_usd, limit=args.limit, dry_run=args.dry_run,
                       priority=block.priority_strategy(args.priority), progress=progress)

    if args.dry_run:
        print(f"\ngarden-ops-{report.run_id}: {report.would_process} cluster(s) would garden "
              f"({report.skipped} already done for {PROPOSE_PROMPT_VERSION}/{args.model}).")
        return
    if args.show:
        for d in blk.applied:
            print(f"  applied  [{d['op']} · stakes {d['stakes']:.2f}]  {d['concept_ids']}  {d['rationale']!r}")
        for p in blk.proposals:
            print(f"  queued   [{p['op']} · stakes {p['stakes']:.2f}]  {p['concept_ids']}  {p['rationale']!r}")
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\ngarden-ops-{report.run_id}: {report.examined} clusters, {report.processed} gardened, "
          f"{report.skipped} skipped, {blk.n_applied} auto-applied, {blk.n_queued} queued{errs}, "
          f"${report.cost_usd:.4f}{tail}")

    if not args.no_stale:                               # the DETERMINISTIC staleness sub-pass rides alongside
        _print_stale(propose_stale(days=args.stale_days), days=args.stale_days)


def _print_stale(stale: list[dict], *, days: float) -> None:
    """The decay/staleness summary — the quiet concepts surfaced for re-confirmation (ADR-0024). A proposal
    already RESOLVED (the human decided earlier) is left STANDING, not re-queued — shown so the count is
    honest, never as a fresh nag."""
    if not stale:
        print(f"staleness: no quiet concepts past {int(days)}d — every concept has recent corroboration.")
        return
    queued = [s for s in stale if not s["suppressed"]]
    print(f"staleness: {len(stale)} quiet concept(s) past {int(days)}d "
          f"({len(queued)} queued to re-confirm/retire, {len(stale) - len(queued)} already decided):")
    for s in stale:
        mark = "  (already decided — left standing)" if s["suppressed"] else ""
        print(f"  {s['concept_id']}  last corroborated {str(s['last_corroborated'])[:10]} "
              f"(~{s['age_days']:.0f}d ago){mark}")
