"""concepts — the concept-graph VIEW: inter-concept structure derived from PROVENANCE FACETS, no LLM
(ADR-0013).

A concept (review's most-trusted artifact) is a flat versioned blob — `{id, title, statement,
evidence, source_takeaway}` — and today the concept layer is a FLAT bag: nothing relates one concept
to another. This module adds the missing structure WITHOUT touching what a concept asserts and WITHOUT
any text similarity. The signal is metadata ratchet already has: each piece of a concept's `evidence`
points at a cleaned blob; one `derived_from` hop reaches its RAW transcript (ground truth, kept
forever), whose `origin_ref` carries project/session/session-time and whose body RE-PARSES to the
session's files-edited/tools (`session_facts` over the active path). UNION those across a concept's
cited sessions and a concept gets FACETS — `{repos, files, tools, sessions, time_range}`. Nothing is
stored: facets are RECOMPUTED on read from the immutable raw, never stamped onto a sidecar — so an old
cleaned blob gets its facets for free, no migration (ADR-0013). Two concepts that touched the same
file, repo, or tool, or that happened close in time, are RELATED; the strength is a weighted SET
OVERLAP, not a cosine over short quotes (the metric that sank dream v1 — ADR-0010 §Context).

This module also OWNS the valid concept set itself (ADR-0032 moved the loaders home from dream): the
decision folds (`concept_kinds`/`concept_scopes`), `load_concepts`/`valid_concept_ids`, the closed
vocabularies (kinds, scopes, lifecycle verbs), and the claim→concept field schema (the prose caps +
defensive coercers every producer shares). The concept layer's one module, so no consumer transits a
superseded stage to read it.

Everything here is a REBUILDABLE VIEW — computed on read from the blobs, never stored, exactly like
`dream.current_takeaways` / `load_concepts`. `concept_graph` returns `{nodes, edges, clusters}`:
  - nodes  — each valid concept with its facets.
  - edges  — DERIVED edges (`shares-repo` / `shares-file` / `shares-tool` / `shares-tag` /
             `temporal-proximity`), one per non-empty facet overlap between a pair. A pure view
             (recomputed each call).
  - clusters — leader clustering over the facet-overlap SCORE: a single deterministic pass in
             sorted-id order — a concept joins the FIRST existing leader it scores past the
             threshold against, else it seeds a new cluster.

The provenance facets are 3a (ADR-0013); the gardener's MANAGED TAGS (3b/ADR-0014) thread in as the
second grouping axis — a cheap-AI semantic signal `garden.py` produces, folded once in `_facet_index`
and overlapped as `shares-tag`. Purely additive: an untagged concept carries no `tags` facet, so the 3a
graph stays byte-identical. The gardener's structural ops (split/merge/supersede of concepts AND of tags)
are deferred to 3c — nothing here mutates a blob.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from . import blobstore, config
from .weave import active_path, parse

# The verbs whose latest decision drops a concept from the valid set — the concept-side mirror of
# `catalog`'s merge/retire/reject filter. `retire` is review's human gate (ADR-0008); `supersede`/`split`
# are the gardener's structural ops (3c/ADR-0015) — a merge-loser is superseded INTO its winner, a split
# original is dissolved INTO its parts. All three are invalidate-don't-delete: the blob + history stay,
# only the latest-decision FOLD takes the concept out of `load_concepts`/`valid_concepts`.
#
# SINGLE-SOURCED CONTRACT: the gardener WRITES these decision verbs (`garden.merge`/`split` reference
# `VERB_SUPERSEDE`/`VERB_SPLIT`) and this fold READS them — one spelling, so a typo can't silently leave a
# merge-loser in the valid set (a trust-corruption). The constants live HERE, beside the fold that reads
# them; garden imports them (garden→concepts holds, no cycle).
VERB_RETIRE = "retire"
VERB_SUPERSEDE = "supersede"
VERB_SPLIT = "split"
CONCEPT_INVALID_VERBS = (VERB_RETIRE, VERB_SUPERSEDE, VERB_SPLIT)

# The concept TYPOLOGY (ADR-0029): faithfulness and generation-usefulness are ORTHOGONAL. A claim can
# be true, worth keeping, and still not belong in CLAUDE.md — "ultracode = env-set effort level" is a
# fact you'd look up; "verify the fix is in the exact artifact before the risky action" shapes conduct.
# So every concept carries a KIND: `behavioral` (shapes conduct — generate projects it) or `reference`
# (a mechanism/fact — kept, queried, never projected by default). The LLM PROPOSES a kind at
# synthesize; the reviewer's decision is authoritative (recorded on the accept, re-kindable later via
# review's `set_kind` — the fold below). Coercion is RECALL-FIRST: an unknown/absent kind reads
# `behavioral`, because a wrongly-behavioral rule is caught at the review/diff gates while a
# wrongly-reference lesson silently vanishes from generation.
KIND_BEHAVIORAL = "behavioral"
KIND_REFERENCE = "reference"
CONCEPT_KINDS = (KIND_BEHAVIORAL, KIND_REFERENCE)
VERB_SET_KIND = "set_kind"     # review's re-kinding decision verb — written there, folded here


def clean_kind(v) -> str:
    """Coerce a proposed kind to the closed vocabulary — unknown/absent → `behavioral` (recall-first,
    see CONCEPT_KINDS above). The coercion idiom of `clean_relation`/`clean_score`, for the typology."""
    return v if v in CONCEPT_KINDS else KIND_BEHAVIORAL


# The concept SCOPE (ADR-0030) — the kind's mirror axis: WHERE a lesson applies, not what shape it
# has. A lesson about one repo's harness belongs in THAT repo's CLAUDE.md, not the global one. Unlike
# kind, no LLM proposes: the proposal DERIVES deterministically from the claim's live evidence's
# subject keys (`resolve.scope_repo_of` — every quote in one repo → that repo's label; 2+ repos or
# none → `global`, because a multi-repo lesson is de facto general and pinning narrower is what the
# reviewer's override is for). The same trust boundary as kind: the derivation is a default, the
# accept records the confirmed scope (`--scope` overrides), and review's `set_scope` re-scopes later,
# outranking the accept in the fold below. The vocabulary is OPEN — any repo label the reviewer
# names — so the only guard is emptiness, never membership.
SCOPE_GLOBAL = "global"
VERB_SET_SCOPE = "set_scope"   # review's re-scoping decision verb — written there, folded here


def clean_scope(v) -> str:
    """Coerce a scope to the open vocabulary's one invariant — a non-empty label. Absent/blank →
    `global` (a lesson with no stated home applies everywhere; narrowing it is the reviewer's
    explicit call). `clean_kind`'s sibling, minus the closed-set check: repo labels are free text."""
    s = v.strip() if isinstance(v, str) else ""
    return s or SCOPE_GLOBAL


# --- the claim→concept field schema: prose caps + defensive coercers ------------------------------
# These bound and scrub the fields that flow TOWARD a concept — a takeaway/claim's title/why/relation
# — so every producer (dream's legacy synth, synthesize's deferred prose) shares one spelling. Homed
# here (ADR-0032) because the schema is the concept layer's: `clean_relation` validates against the
# valid concept set, and `clip` caps prose whose destination is the concept STATEMENT.

TITLE_MAX = 80
WHY_MAX = 280
NOTE_MAX = 160

_RELATION_KINDS = ("new", "strengthens", "refines", "contradicts")


def clip(text: str, limit: int = WHY_MAX) -> str:
    """Length-cap a synthesized field WITHOUT chopping mid-word. A hard `[:limit]` once shipped a rule
    ending `…evaluates the s` — the why becomes the concept STATEMENT becomes the CLAUDE.md rule, so a
    guillotined sentence lands verbatim in a human's config. Prefer the last SENTENCE boundary that fits
    (a complete thought, no ellipsis); else the last WORD boundary + `…` (clearly abbreviated, never
    broken). Stays within `limit`."""
    t = text.strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:                          # a real sentence break, not a stray early period
        return cut[:end + 1].rstrip()
    sp = cut.rfind(" ")
    base = cut[:sp] if sp >= limit // 2 else cut[:limit - 1]
    return base.rstrip() + "…"


def clean_relation(v, known_ids: set[str]) -> dict:
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


# --- the valid concept set: the decision folds + loaders ------------------------------------------

def _facet_fold(root: Path | None, *, set_verb: str, clean) -> dict[str, str]:
    """The ONE decision fold behind `concept_kinds`/`concept_scopes` — the two facets share the exact
    precedence shape, so the mechanics live once: `clean(decision)` reads the facet value off a
    decision (None = facet-less/blank/foreign — nothing to fold), and the latest `set_verb` decision
    targeting the concept wins over the facet the latest accept/edit decision recorded. One scan; same
    (fetched_at, content_hash) recency tie-break as `latest_decisions`. ("accept", "edit") are
    review's promote verbs — resolve.ACCEPT_VERBS, spelled here to keep concepts import-cycle-free."""
    root = root or config.data_root()
    set_best: dict[str, tuple[tuple[str, str], str]] = {}
    acc_best: dict[str, tuple[tuple[str, str], str]] = {}
    for d in blobstore.decisions_for(None, root):
        v = clean(d)
        if v is None:
            continue
        key = (d.get("fetched_at", ""), d.get("content_hash", ""))
        verb = d.get("verb")
        if verb == set_verb and isinstance(d.get("target"), str):
            t = d["target"]
            if t not in set_best or key > set_best[t][0]:
                set_best[t] = (key, v)
        elif verb in ("accept", "edit") and isinstance(d.get("concept"), str):
            c = d["concept"]
            if c not in acc_best or key > acc_best[c][0]:
                acc_best[c] = (key, v)
    out = {c: k for c, (_, k) in acc_best.items()}
    out.update({c: k for c, (_, k) in set_best.items()})   # set_* > accept — the later, deliberate call
    return out


def concept_kinds(root: Path | None = None) -> dict[str, str]:
    """concept id → its reviewer-confirmed KIND, folded from DECISIONS only (ADR-0029): the latest
    `set_kind` decision targeting the concept wins over the `kind` the latest accept/edit decision
    recorded (the accept references its minted concept via its `concept` field); a concept in NEITHER
    map is absent here and defaults `behavioral` at the caller (legacy concepts predate the facet).
    The kind lives on decisions, never the concept blob, so a garden op re-versioning the blob
    (merge's evidence union) cannot silently drop it — and the reviewer's `set_kind` outranks the
    accept because re-kinding is a LATER, deliberate call on the same trust boundary."""
    return _facet_fold(root, set_verb=VERB_SET_KIND,
                       clean=lambda d: d["kind"] if d.get("kind") in CONCEPT_KINDS else None)


def concept_scopes(root: Path | None = None) -> dict[str, str]:
    """concept id → its reviewer-confirmed SCOPE, folded from DECISIONS only (ADR-0030) —
    `concept_kinds`' mirror on the scope axis, same precedence for the same reason: the latest
    `set_scope` decision targeting the concept outranks the `scope` the latest accept/edit recorded,
    because re-scoping is a LATER, deliberate call on the same trust boundary. A concept in neither
    map is absent here and defaults `global` at the caller (legacy concepts predate the axis). The
    vocabulary is open, so the fold keeps any non-blank label; blank/foreign decisions are skipped."""
    def clean(d):
        s = d.get("scope")
        return s.strip() if isinstance(s, str) and s.strip() else None
    return _facet_fold(root, set_verb=VERB_SET_SCOPE, clean=clean)


def load_concepts(root: Path | None = None) -> list[dict]:
    """The current VALID concept set — the human-reviewed source of truth dream judges belief-change
    against (ADR-0006/0007). A concept is a versioned blob the `review` stage ingests; "valid" is
    derived: the latest version of each concept source, minus any whose latest decision carries a
    `CONCEPT_INVALID_VERBS` verb (retire / supersede / split — each takes the concept out of the valid set).
    This is the loop closing — review's accepts become the concepts dream reads next run. Empty until
    review runs, so every takeaway is `new`. Malformed/absent → skipped, never fatal.

    Each returned concept carries a derived `kind` (ADR-0029): latest set_kind decision > the accept
    decision's kind > `behavioral` (the legacy default — concepts predating the facet shape conduct
    until the reviewer says otherwise) — and a derived `scope` (ADR-0030, the same fold shape on the
    open vocabulary): latest set_scope > the accept's scope > `global`. Attached here, on the ONE
    valid-concept view, so every consumer (generate's projection filters, status's census, review's
    listing) reads one derivation."""
    root = root or config.data_root()
    decisions = blobstore.latest_decisions(root)
    kinds = concept_kinds(root)
    scopes = concept_scopes(root)
    out: list[dict] = []
    for sid, h in blobstore.latest_by_kind("concept", root).items():
        d = decisions.get(sid)
        if d and d.get("verb") in CONCEPT_INVALID_VERBS:   # retired/superseded/split out of the valid set
            continue                                        # (ADR-0007 §4; the gardener's ops, ADR-0015)
        try:
            obj = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str) and obj["id"]:
            obj["kind"] = kinds.get(obj["id"], KIND_BEHAVIORAL)
            obj["scope"] = scopes.get(obj["id"], SCOPE_GLOBAL)
            out.append(obj)
    return out


def valid_concept_ids(root: Path | None = None) -> set[str]:
    """The IDS of the current valid concept set — `{c["id"] for c in load_concepts(root)}`. The membership
    test the gardener's structural ops (an op never targets a dead concept) and the concept-graph folds
    gate on; single-sourced HERE, next to `load_concepts` (its source), so the call sites never re-spell
    the comprehension."""
    return {c["id"] for c in load_concepts(root)}


# Facet-overlap weights — a shared FILE is the strongest co-location signal (two concepts that wrote
# the same file are almost certainly about the same code), a shared REPO weaker (a repo holds many
# unrelated lessons), a shared TOOL weaker still (everyone runs Bash). Temporal proximity is only a
# small tie-breaking BONUS, never structure on its own (two unrelated lessons can land in one evening).
# Counts of shared members scale the weight, so corroboration across many shared files outranks one.
# untuned starting values — tune against a gold set (ADR-0013).
W_FILE = 3.0
W_REPO = 1.0
W_TOOL = 0.5
W_TEMPORAL_BONUS = 0.25
CLUSTER_THRESHOLD = 3.0          # leader-join bar — CLUSTER_THRESHOLD == W_FILE couples it to "one shared file clears it" (a shared repo alone does not)
# A shared MANAGED TAG (3b/ADR-0014) is a SEMANTIC grouping — its whole purpose is to relate concepts that
# share a THEME without sharing a file. A single tag still fires the `shares-tag` EDGE (the thematic
# relation stays VISIBLE), but a managed tag is UNCURATED + auto-applied (no merge/retire until 3c), so one
# tag alone must NOT force a cluster: W_TAG < CLUSTER_THRESHOLD by design. At 2.0 a single shared tag (2.0)
# scores BELOW the 3.0 bar, while CORROBORATION clears it — two shared tags (4.0), or a shared tag + a
# shared file (2.0+3.0) or repo (2.0+1.0). This protects 3c's per-cluster LLM passes from garbage,
# over-broad clusters (a `general` tag smeared across 50 concepts) before the vocab is trustworthy. Staging:
# once 3c's tag merge/retire makes the vocab curated, W_TAG is raised toward/above CLUSTER_THRESHOLD.
# Untuned, like the rest — pending a gold set.
W_TAG = 2.0
TEMPORAL_WINDOW_SECONDS = 7 * 24 * 3600  # "close in time" = within a week (sessions on one task cluster by days)

EDGE_KINDS = ("shares-file", "shares-repo", "shares-tool", "shares-tag", "temporal-proximity")


# --- one cleaned blob's facets: recompute from the raw ground truth (one derived_from hop) ---------

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})  # tool calls that WRITE a path (a Read does not)


def session_facts(spine: list[dict]) -> dict:
    """Session-level PROVENANCE distilled from the ACTIVE PATH: which files were written and which tools
    ran. `_cleaned_facets` re-derives this from the raw transcript on every read (never stored), and the
    facet substrate unions it across the sessions a concept cites to find repo/file/tool overlap — with
    NO text similarity. Sets come back as SORTED lists for stable JSON bytes. `files_edited` keys on the
    written PATH of an Edit/Write/MultiEdit/NotebookEdit (a Read views, it does not write — and a
    NotebookEdit names its target `notebook_path`, not `file_path`); `tools` is every tool name invoked.
    Off-spine (abandoned/sidechain) calls are excluded — the cleaned blob is the active path, so its
    provenance is too."""
    files: set[str] = set()
    tools: set[str] = set()
    for r in spine:
        if r.get("type") != "assistant":
            continue
        c = r.get("message", {}).get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name")
            if isinstance(name, str) and name:
                tools.add(name)
            if name in EDIT_TOOLS:
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                fp = inp.get("notebook_path") if name == "NotebookEdit" else inp.get("file_path")
                if isinstance(fp, str) and fp.strip():
                    files.add(fp.strip())
    return {"files_edited": sorted(files), "tools": sorted(tools)}


def repo_label(origin: dict) -> str | None:
    """The repo facet — a CLEAN repo name. Prefer the transcript's `cwd` basename (`ratchet` from
    `/home/sulin/ratchet`): unambiguous, and what a human calls the repo. Fall back to the datastore
    project-dir slug (`-home-sulin-ratchet`) only when no cwd was recorded — that slug is a lossy
    `/`→`-` encoding that drags the home path into a rule's `When working in …:` trigger. The value is
    the same for every blob in one repo, so clustering (shares-repo) is unchanged; only the label reads
    better (and `tap` already captures cwd, so no backfill — old blobs just keep the slug fallback)."""
    cwd = origin.get("cwd")
    if cwd:
        name = Path(cwd).name
        if name:
            return name
    return origin.get("project")


def _cleaned_facets(cleaned_hash: str, root: Path, cache: dict | None = None) -> dict | None:
    """The provenance facets of ONE cleaned blob — `{repo, session, files, tools, time}` — RECOMPUTED
    from the raw ground truth, NEVER read from a stored sidecar. One `derived_from` hop reaches the raw
    transcript (kept forever): its `origin_ref` carries repo/session/session-time, and re-parsing its
    body → `active_path` → `session_facts` re-derives files/tools. Because nothing is stored, an old
    cleaned blob gets facets for FREE (no backfill — the migration vanishes by construction); the
    accepted cost is one raw re-parse per call, fine on this COLD path (ADR-0013). All hops are
    content-addressed, so the facets are reproducible from the hash like every other ratchet view.

    The per-blob inner loop of `concept_facets` (analogous to dream's private `_resolve_event`), so it
    is private. A gone/broken cleaned or raw blob → None (skipped upstream, never fatal)."""
    if cache is not None and cleaned_hash in cache:
        return cache[cleaned_hash]
    facets = None
    try:
        raw = blobstore.get_meta(cleaned_hash, root).get("derived_from")
        if raw:
            m = blobstore.get_meta(raw, root)
            origin = m.get("origin_ref") or {}
            sf = session_facts(active_path(parse(blobstore.get(raw, root))))
            facets = {
                "repo": repo_label(origin),
                "session": origin.get("session_id") or m.get("source_id"),
                "files": set(sf["files_edited"]),
                "tools": set(sf["tools"]),
                "time": origin.get("mtime"),
            }
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        facets = None
    if cache is not None:
        cache[cleaned_hash] = facets
    return facets


def concept_facets(concept: dict, root: Path | None = None, *, cache: dict | None = None,
                   tags: list[str] | None = None) -> dict:
    """UNION the facets of every cleaned blob a concept cites → `{repos, files, tools, sessions,
    time_range}`. A concept can span MULTIPLE sessions (each strengthen/refine appends another event's
    evidence — ADR-0010), so this folds them all. Sets come back as SORTED lists (stable, JSON-ready);
    `time_range` is `[earliest, latest]` ISO over the cited sessions, or None when no session has a
    time. The empty concept (no resolvable evidence) yields all-empty facets — it simply never shares.

    `tags` (3b/ADR-0014) is the SECOND facet source — the gardener's managed tags for this concept, a
    SEMANTIC grouping axis the provenance facets lack. It is threaded in (the graph folds the assignments
    once and passes the per-concept list), NOT recomputed here. A `tags` key is added ONLY when non-empty:
    an untagged concept's facet bytes stay IDENTICAL to 3a, so the 3a graph (and its golden) is unchanged
    and `shares-tag` never fires until a tag exists."""
    root = root or config.data_root()
    repos: set[str] = set()
    files: set[str] = set()
    tools: set[str] = set()
    sessions: set[str] = set()
    times: list[str] = []
    for ev in concept.get("evidence") or []:
        ch = ev.get("cleaned_hash") if isinstance(ev, dict) else None
        if not ch:
            continue
        cf = _cleaned_facets(ch, root, cache)
        if cf is None:
            continue
        if cf["repo"]:
            repos.add(cf["repo"])
        if cf["session"]:
            sessions.add(cf["session"])
        files |= cf["files"]
        tools |= cf["tools"]
        if cf["time"]:
            times.append(cf["time"])
    out = {
        "repos": sorted(repos),
        "files": sorted(files),
        "tools": sorted(tools),
        "sessions": sorted(sessions),
        # min/max over ISO STRINGS reads chronological only because every mtime is tz-aware UTC ISO (tap).
        "time_range": [min(times), max(times)] if times else None,
    }
    if tags:                                    # only when present — keeps the untagged facet bytes == 3a
        out["tags"] = sorted(set(tags))
    return out


def chunk_facets(cleaned_hash: str, root: Path | None = None, *, cache: dict | None = None) -> dict:
    """The provenance facets of a single CHUNK's cleaned blob — `{repos, files, tools, sessions,
    time_range}`, in the SAME shape `concept_facets` yields, so `facet_score`/`facet_overlap` can score a
    chunk AGAINST a concept. A chunk IS a concept with one piece of evidence (its own cleaned blob), so
    this folds straight through `concept_facets` — no second copy of the raw re-parse. glean (4b/ADR-0019)
    passes the result as `concept_digest`'s `relevant_to` to order the concepts most likely to already
    cover THIS chunk ahead of the truncation cut — the false-novelty fix. `cache` is the shared
    cleaned-blob facet cache (re-used across a run's chunks)."""
    return concept_facets({"evidence": [{"cleaned_hash": cleaned_hash}]}, root, cache=cache)


# --- the facet-overlap relation: shared sets + temporal nearness, then a weighted score -----------

def _temporal_proximate(fa: dict, fb: dict) -> bool:
    """True iff the two concepts' session time_ranges sit within `TEMPORAL_WINDOW_SECONDS` — overlap
    is gap 0; otherwise the gap is the distance between the nearer endpoints. An unparseable/missing
    range is never proximate (no false edge from bad data)."""
    ia, ib = _interval(fa), _interval(fb)  # the datetime math below is sound only because every mtime is tz-aware UTC ISO
    if ia is None or ib is None:
        return False
    if ia[1] < ib[0]:
        gap = (ib[0] - ia[1]).total_seconds()
    elif ib[1] < ia[0]:
        gap = (ia[0] - ib[1]).total_seconds()
    else:
        gap = 0.0
    return gap <= TEMPORAL_WINDOW_SECONDS


def _interval(f: dict) -> tuple[datetime, datetime] | None:
    tr = f.get("time_range")
    if not tr or len(tr) != 2:
        return None
    try:
        return datetime.fromisoformat(tr[0]), datetime.fromisoformat(tr[1])
    except (TypeError, ValueError):
        return None


def facet_overlap(fa: dict, fb: dict) -> dict:
    """The raw shared facets between two concepts — sorted shared file/repo/tool lists + the temporal
    flag. The single source both the edges and the score read, so they never disagree."""
    return {
        "shares-file": sorted(set(fa["files"]) & set(fb["files"])),
        "shares-repo": sorted(set(fa["repos"]) & set(fb["repos"])),
        "shares-tool": sorted(set(fa["tools"]) & set(fb["tools"])),
        # tags read DEFENSIVELY: an untagged concept's facets carry no `tags` key (3b keeps the 3a facet
        # bytes identical when no tags exist), so `.get` returns () → no shared tag → no `shares-tag` edge.
        "shares-tag": sorted(set(fa.get("tags", ())) & set(fb.get("tags", ()))),
        "temporal-proximity": _temporal_proximate(fa, fb),
    }


def facet_score(fa: dict, fb: dict) -> float:
    """A weighted SET-OVERLAP score (NOT a similarity metric): shared-file count × W_FILE + repo ×
    W_REPO + tool × W_TOOL, plus the temporal bonus. Symmetric; rises with corroboration (more shared
    files). This is what the leader clustering thresholds on — deterministic, no embeddings, no tf-idf."""
    ov = facet_overlap(fa, fb)
    score = (W_FILE * len(ov["shares-file"])
             + W_REPO * len(ov["shares-repo"])
             + W_TOOL * len(ov["shares-tool"])
             + W_TAG * len(ov["shares-tag"]))      # a shared tag adds W_TAG — below the bar ALONE, it clusters
                                                   # only WITH corroboration (a 2nd tag or a file/repo; ADR-0014)
    if ov["temporal-proximity"]:
        score += W_TEMPORAL_BONUS
    return score


# --- the two rebuildable views: derived edges + leader clusters -----------------------------------

def derived_edges(ids: list[str], facets: dict[str, dict]) -> list[dict]:
    """Every non-empty facet overlap as an edge `{source, target, kind, shared}`, over each concept
    PAIR. `ids` is sorted, so source < target and the whole list is order-stable; a pair with no shared
    facet yields nothing (disjoint concepts → no edge). `shared` is the sorted shared members ([] for a
    temporal edge, which carries no member set)."""
    edges: list[dict] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            ov = facet_overlap(facets[a], facets[b])
            for kind in EDGE_KINDS:
                if kind == "temporal-proximity":
                    if ov[kind]:
                        edges.append({"source": a, "target": b, "kind": kind, "shared": []})
                elif ov[kind]:
                    edges.append({"source": a, "target": b, "kind": kind, "shared": ov[kind]})
    return edges


def leader_clusters(ids: list[str], facets: dict[str, dict]) -> list[dict]:
    """Leader (sequential) clustering over the facet-overlap score (Hartigan; ADR-0010 §8 chose it for
    its order-stable, single-pass determinism). Walk concepts in sorted-id order: a concept joins the
    FIRST existing leader it scores >= CLUSTER_THRESHOLD against, else it becomes a new leader. One
    deterministic pass, every concept in exactly one cluster. Returns `[{leader, members}]` with members
    sorted, leaders in creation order."""
    leaders: list[str] = []
    members: dict[str, list[str]] = {}
    for cid in ids:
        joined = None
        for lead in leaders:
            if facet_score(facets[cid], facets[lead]) >= CLUSTER_THRESHOLD:
                joined = lead
                break
        if joined is None:
            leaders.append(cid)
            members[cid] = [cid]
        else:
            members[joined].append(cid)
    return [{"leader": lead, "members": sorted(members[lead])} for lead in leaders]


# --- the public surface: the whole graph, or just its clusters ------------------------------------

def _facet_index(root: Path) -> tuple[list[dict], list[str], dict[str, dict]]:
    """The shared spine of every view: the valid concepts (sorted by id), their ids, and their facets —
    computed ONCE per call with a per-call cleaned-blob cache so a concept cited by many edges resolves
    its sidecars once. Valid concepts = `load_concepts` (latest version per id, minus retired). The
    gardener's managed tags (3b/ADR-0014) are folded ONCE here and threaded into each concept's facets as
    the second grouping axis. The `garden` import is LAZY (function-local): garden imports `concept_facets`
    from this module, so a top-level import here would be a cycle — the tag-FOLD readers carry no Block, so
    a runtime import breaks it cleanly."""
    from . import garden                          # lazy: avoids the garden <-> concepts import cycle
    concepts = sorted(load_concepts(root), key=lambda c: c["id"])
    tag_map = garden.all_concept_tags(root)        # concept_id -> current tags, one scan
    cache: dict = {}
    facets = {c["id"]: concept_facets(c, root, cache=cache, tags=tag_map.get(c["id"]))
              for c in concepts}
    return concepts, [c["id"] for c in concepts], facets


def _asserted_graph_edges(root: Path, valid_ids: set[str]) -> list[dict]:
    """The gardener's ASSERTED edges (3c/ADR-0015), folded in alongside the derived ones — but ONLY between
    two VALID concepts (both endpoints are graph nodes), so a `supersedes` loser→winner edge naturally drops
    out of the live graph once the loser is invalidated (the lineage still lives in the asserted-edge blob
    store, readable via `garden.asserted_edges`). Marked `asserted: True` to distinguish them from the
    recomputed DERIVED edges (ADR-0013 §B); EMPTY when nothing has been asserted, so an un-edited graph (and
    the 3a golden) is byte-IDENTICAL. The `garden` import is LAZY (the garden <-> concepts cycle, as in
    `_facet_index`)."""
    from . import garden
    out: list[dict] = []
    for e in garden.asserted_edges(root):
        if e["src"] in valid_ids and e["dst"] in valid_ids:
            out.append({"source": e["src"], "target": e["dst"], "kind": e["kind"],
                        "note": e["note"], "asserted": True})
    out.sort(key=lambda x: (x["source"], x["kind"], x["target"]))
    return out


def concept_hierarchy(root: Path | None = None) -> dict:
    """The generalization SPINE — `{parent_id: sorted[child_ids]}` over the active `generalizes` asserted
    edges between valid concepts (3c/ADR-0015). The tree the gardener's `abstract`/`reparent` maintain ON
    TOP of the flat facet graph; kept a SEPARATE view (not a `concept_graph` key) so the 3a graph bytes —
    and its golden — stay unchanged. Empty until an `abstract`/`reparent` asserts a `generalizes` edge."""
    root = root or config.data_root()
    from . import garden
    valid = valid_concept_ids(root)
    children: dict[str, list[str]] = {}
    for e in garden.asserted_edges(root):
        if e["kind"] == "generalizes" and e["src"] in valid and e["dst"] in valid:
            children.setdefault(e["src"], []).append(e["dst"])
    return {p: sorted(cs) for p, cs in children.items()}


def concept_graph(root: Path | None = None) -> dict:
    """The rebuildable concept graph — `{nodes, edges, clusters}` — computed from provenance facets,
    no LLM, never stored. Order-stable (concepts sorted by id throughout). `edges` = the DERIVED facet
    overlaps (3a/ADR-0013) PLUS the gardener's ASSERTED edges (3c/ADR-0015, marked `asserted: True`); the
    asserted set is empty until an op runs, so an un-edited graph is byte-identical to 3a."""
    root = root or config.data_root()
    concepts, ids, facets = _facet_index(root)
    nodes = [{"id": c["id"], "title": c.get("title", ""), "facets": facets[c["id"]]} for c in concepts]
    return {"nodes": nodes,
            "edges": derived_edges(ids, facets) + _asserted_graph_edges(root, set(ids)),
            "clusters": leader_clusters(ids, facets)}


def concept_clusters(root: Path | None = None) -> list[dict]:
    """Just the cluster view — the facet-overlap leader clusters of the valid concepts."""
    root = root or config.data_root()
    _, ids, facets = _facet_index(root)
    return leader_clusters(ids, facets)


# --- the concept DIGEST: a BOUNDED, STRUCTURED "what we already know" read-view for prompts --------
# The flat list dream injected (`- id X: title — statement`) hid every relation the gardener built. The
# digest is the structured replacement the upstream LLM stages read to gauge Bayesian surprise against the
# store — concepts GROUPED by their facet cluster, each carrying its managed tags + asserted relations. It
# is rendered IN-PROMPT (no embeddings, no queryable index — ADR-0018), so it must stay BOUNDED: the
# maturity gate + the gardener's consolidation keep the live set small, and `budget` is the backstop that
# drops the long tail (least-corroborated first) when it does not.

DIGEST_BUDGET = 80          # default CONCEPT cap — beyond it, only the most-entrenched render (rest → +N more)
DIGEST_STATEMENT_MAX = 140  # truncate each statement so per-concept size is bounded, not just the count
DIGEST_EMPTY = "(no concepts yet — treat everything as new)"   # the sentinel: never relate against nothing


def _digest_entrench(node: dict, evidence_count: int) -> tuple[int, int]:
    """The ENTRENCHMENT key a concept is ranked by when the digest must truncate — distinct cited SESSIONS
    first (the same corroboration signal dream's maturity gate trusts: a belief seen across more sessions is
    more durable), evidence-pointer count as the tie-break. So a partial digest keeps the load-bearing
    beliefs and sheds the thin, single-session tail."""
    return (len(node["facets"].get("sessions", [])), evidence_count)


def _digest_relations(edges: list[dict]) -> dict[str, list[str]]:
    """Per-concept OUTGOING asserted relations as compact strings (`<kind> → <dst> (note)`) — the gardener's
    `generalizes`/`supersedes`/`relates-to` edges (3c/ADR-0015), the STRUCTURE the flat list dropped. Showing
    a concept's outgoing `generalizes` surfaces the hierarchy spine right on the parent's line, so the tree
    needs no separate render. The DERIVED facet-overlap edges are deliberately NOT shown — they ARE the
    clustering (already the grouping axis); only the gardener's DELIBERATE relations add signal here."""
    rel: dict[str, list[str]] = {}
    for e in edges:
        if not e.get("asserted"):
            continue
        note = f" ({e['note']})" if e.get("note") else ""
        rel.setdefault(e["source"], []).append(f"{e['kind']} → {e['target']}{note}")
    return rel


def _digest_shared(members: list[str], by_node: dict) -> str:
    """The facet a cluster's members hold in COMMON — the legible BASIS of the grouping, so the model sees
    WHAT a cluster shares (`shares file: foo.py`) instead of an opaque leader id. Intersect members' facets,
    most-salient axis first — the facet weights' true order (W_FILE > W_TAG > W_REPO > W_TOOL). Tags are an
    axis here because two shared tags alone (2×W_TAG) clear CLUSTER_THRESHOLD — a tag-formed cluster must
    show its basis like any other. A transitively-joined cluster with no globally-common facet → no
    annotation (each concept's own line still carries its facets)."""
    for axis, label in (("files", "file"), ("tags", "tag"), ("repos", "repo"), ("tools", "tool")):
        common = set.intersection(*(set(by_node[m]["facets"].get(axis) or ()) for m in members))
        if common:
            shown = sorted(common)[:3]
            more = f" +{len(common) - len(shown)}" if len(common) > len(shown) else ""
            return f" · shares {label}: {', '.join(shown)}{more}"
    return ""


def digest_context(root: Path | None = None) -> dict:
    """The run-invariant inputs to `concept_digest` — the facet GRAPH (the one expensive raw re-parse,
    ADR-0013) plus each concept's statement / evidence-count / outgoing asserted relations — everything the
    render needs EXCEPT the `budget` and the `relevant_to` ordering. Split out so a PER-CHUNK caller
    (glean's relevance check, 4b/ADR-0019) pays the facet pass ONCE per run and re-renders per chunk with
    that chunk's `relevant_to`; dream's single global call folds it straight back into `concept_digest`."""
    root = root or config.data_root()
    g = concept_graph(root)
    by_node = {n["id"]: n for n in g["nodes"]}
    blobs = {c["id"]: c for c in load_concepts(root)}     # statements + evidence count live on the blob
    evid = {cid: len(blobs.get(cid, {}).get("evidence") or []) for cid in by_node}
    return {"clusters": g["clusters"], "by_node": by_node, "blobs": blobs, "evid": evid,
            "relations": _digest_relations(g["edges"])}


def concept_digest(root: Path | None = None, *, budget: int = DIGEST_BUDGET,
                   relevant_to: dict | None = None, context: dict | None = None) -> str:
    """A BOUNDED, STRUCTURED rendering of the current valid concept layer for prompt injection — the "what
    we already know" read-back the upstream LLM stages judge novelty/belief-change against (ADR-0018,
    replacing dream's flat `_render_concepts`). Built from `concept_graph` in ONE facet pass: concepts
    GROUPED BY their facet CLUSTER (the complete partition — every concept lands in exactly one), each line
    its id + title + truncated statement + its managed TAGS (3b/ADR-0014) + its outgoing asserted RELATIONS
    (generalizes/supersedes/relates-to — the hierarchy spine, 3c/ADR-0015). A rebuildable read-view, never
    stored, like the rest of this module.

    `relevant_to` is the PROVENANCE-RELEVANT ordering knob (4b/ADR-0019): a chunk's facet set (its
    `{repos, files, tools}`, via `chunk_facets`). When given, concepts are kept/ordered by facet-OVERLAP
    with it (`facet_score`) FIRST, so the concepts most likely to already cover THIS chunk survive the
    `budget` cut — fixing the false-novelty risk where a near-duplicate would match a thin, single-session
    concept that global-entrenchment truncation drops. When None (dream's global use), the ranking is pure
    ENTRENCHMENT (unchanged). `context` (from `digest_context`) lets a per-chunk caller reuse one facet pass
    across many `relevant_to` renders; None recomputes it (the standalone path dream/CLI/tests use).

    BOUNDED by `budget` (a CONCEPT cap): past it, keep the top-ranked (relevance-then-entrenchment, or pure
    entrenchment — most distinct cited sessions, then most evidence, `_digest_entrench`) and drop the long
    tail, emitting a `…(+N more)` marker so the model KNOWS the view is partial — those concepts EXIST, they
    are just not shown — rather than treating a dropped lesson as new. The empty set yields a clear sentinel
    so a stage is never asked to relate against nothing. budget <= 0 disables the cap (render all)."""
    ctx = context if context is not None else digest_context(root or config.data_root())
    by_node = ctx["by_node"]
    if not by_node:
        return DIGEST_EMPTY
    blobs, evid, relations = ctx["blobs"], ctx["evid"], ctx["relations"]

    # RANK the concepts; the top `budget` survive, the long tail drops. `rank` drives both member order
    # (best first) and cluster order (each cluster follows its best member). With `relevant_to`, facet-
    # OVERLAP with this chunk leads (the concepts likely to cover it beat the cut — false-novelty fix);
    # entrenchment (sessions, then evidence) + id break ties — deterministic either way.
    def sortkey(cid: str) -> tuple:
        s, e = _digest_entrench(by_node[cid], evid[cid])
        if relevant_to is not None:
            return (-facet_score(by_node[cid]["facets"], relevant_to), -s, -e, cid)
        return (-s, -e, cid)
    ordered = sorted(by_node, key=sortkey)
    kept = set(ordered[:budget]) if budget > 0 else set(ordered)
    rank = {cid: i for i, cid in enumerate(ordered)}
    dropped = len(ordered) - len(kept)

    rendered: list[tuple[int, str, list[str]]] = []     # (best-member rank, leader, surviving members)
    for cl in ctx["clusters"]:
        members = sorted((m for m in cl["members"] if m in kept), key=lambda m: rank[m])
        if members:
            rendered.append((rank[members[0]], cl["leader"], members))
    rendered.sort(key=lambda t: t[0])

    # the header states the ORDERING RULE the consumer (an LLM) should trust — it must track the
    # actual sortkey: relevance-first under `relevant_to`, pure entrenchment otherwise.
    order_note = "most-relevant first" if relevant_to is not None else "most-entrenched first"
    lines = [f"KNOWN CONCEPTS — what the memory already holds, grouped by facet cluster ({order_note}):"]
    for _, leader, members in rendered:
        lines.append("")
        shares = _digest_shared(members, by_node) if len(members) >= 2 else ""
        lines.append(f"[{leader}] cluster ({len(members)}){shares}:")
        for cid in members:
            node = by_node[cid]
            title = str(node.get("title", "")).strip()
            _st = str(blobs.get(cid, {}).get("statement", "")).strip()
            statement = (_st[:DIGEST_STATEMENT_MAX - 1] + "…") if len(_st) > DIGEST_STATEMENT_MAX else _st
            tags = node["facets"].get("tags") or []
            tagstr = f"  · tags: {', '.join(tags)}" if tags else ""
            lines.append(f"  - {cid}: {title} — {statement}{tagstr}")
            rels = relations.get(cid)
            if rels:
                lines.append(f"      {'; '.join(rels)}")
    if dropped:
        lines.append("")
        lines.append(f"…(+{dropped} more, dropped as least-corroborated)")
    return "\n".join(lines)


# --- CLI: dump the graph for spot-checking (mirrors the other stages' read-only inspectors) -------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="concepts",
                                 description="Dump the rebuildable concept-facet graph (no LLM).")
    ap.add_argument("--clusters", action="store_true", help="print only the facet-overlap clusters")
    ap.add_argument("--hierarchy", action="store_true",
                    help="print only the generalization spine (the gardener's `generalizes` edges)")
    ap.add_argument("--digest", action="store_true",
                    help="print the bounded, structured concept digest dream injects (4a)")
    ap.add_argument("--budget", type=int, default=DIGEST_BUDGET,
                    help=f"the digest's concept cap (default: {DIGEST_BUDGET})")
    ap.add_argument("--json", action="store_true", help="emit the full graph as JSON (default: a summary)")
    args = ap.parse_args(argv)

    if args.clusters:
        print(json.dumps(concept_clusters(), ensure_ascii=False, indent=2))
        return
    if args.hierarchy:
        print(json.dumps(concept_hierarchy(), ensure_ascii=False, indent=2))
        return
    if args.digest:
        print(concept_digest(budget=args.budget))
        return
    graph = concept_graph()
    if args.json:
        print(json.dumps(graph, ensure_ascii=False, indent=2))
        return
    # the default human summary: each node + its facets, then the edges, then the clusters.
    print(f"{len(graph['nodes'])} concept(s), {len(graph['edges'])} edge(s), "
          f"{len(graph['clusters'])} cluster(s)\n")
    for n in graph["nodes"]:
        f = n["facets"]
        print(f"  {n['id']}  {n['title']!r}")
        print(f"      repos={f['repos']} files={f['files']} tools={f['tools']} "
              f"sessions={len(f['sessions'])} time={f['time_range']}")
    if graph["edges"]:
        print("\n  edges:")
        for e in graph["edges"]:
            # an asserted edge (3c) carries a `note`, not the derived edges' `shared` member list.
            tail = f" {e['shared']}" if e.get("shared") else (" *asserted*" if e.get("asserted") else "")
            print(f"    {e['source']} —{e['kind']}→ {e['target']}{tail}")
    print("\n  clusters:")
    for cl in graph["clusters"]:
        print(f"    [{cl['leader']}] {cl['members']}")


if __name__ == "__main__":
    main()
