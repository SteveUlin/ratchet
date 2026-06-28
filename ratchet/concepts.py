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

Everything here is a REBUILDABLE VIEW — computed on read from the blobs, never stored, exactly like
`dream.current_takeaways` / `load_concepts`. `concept_graph` returns `{nodes, edges, clusters}`:
  - nodes  — each valid concept with its facets.
  - edges  — DERIVED edges (`shares-repo` / `shares-file` / `shares-tool` / `temporal-proximity`),
             one per non-empty facet overlap between a pair. A pure view (recomputed each call).
  - clusters — leader clustering over the facet-overlap SCORE: a single deterministic pass, the most
             distinctive concept seeds a cluster, later ones join the first leader within threshold.

This is the additive READ-side substrate only. The gardener's ASSERTED edges, managed tags, and the
structural ops (split/merge/supersede) are deferred to 3b/3c (ADR-0013) — nothing here mutates a blob.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from . import blobstore, config, dream
from .weave import active_path, parse

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
TEMPORAL_WINDOW_SECONDS = 7 * 24 * 3600  # "close in time" = within a week (sessions on one task cluster by days)

EDGE_KINDS = ("shares-file", "shares-repo", "shares-tool", "temporal-proximity")


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
                "repo": origin.get("project"),
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


def concept_facets(concept: dict, root: Path | None = None, *, cache: dict | None = None) -> dict:
    """UNION the facets of every cleaned blob a concept cites → `{repos, files, tools, sessions,
    time_range}`. A concept can span MULTIPLE sessions (each strengthen/refine appends another event's
    evidence — ADR-0010), so this folds them all. Sets come back as SORTED lists (stable, JSON-ready);
    `time_range` is `[earliest, latest]` ISO over the cited sessions, or None when no session has a
    time. The empty concept (no resolvable evidence) yields all-empty facets — it simply never shares."""
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
    return {
        "repos": sorted(repos),
        "files": sorted(files),
        "tools": sorted(tools),
        "sessions": sorted(sessions),
        # min/max over ISO STRINGS reads chronological only because every mtime is tz-aware UTC ISO (tap).
        "time_range": [min(times), max(times)] if times else None,
    }


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
        "temporal-proximity": _temporal_proximate(fa, fb),
    }


def facet_score(fa: dict, fb: dict) -> float:
    """A weighted SET-OVERLAP score (NOT a similarity metric): shared-file count × W_FILE + repo ×
    W_REPO + tool × W_TOOL, plus the temporal bonus. Symmetric; rises with corroboration (more shared
    files). This is what the leader clustering thresholds on — deterministic, no embeddings, no tf-idf."""
    ov = facet_overlap(fa, fb)
    score = (W_FILE * len(ov["shares-file"])
             + W_REPO * len(ov["shares-repo"])
             + W_TOOL * len(ov["shares-tool"]))
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
    its sidecars once. Valid concepts = `dream.load_concepts` (latest version per id, minus retired)."""
    concepts = sorted(dream.load_concepts(root), key=lambda c: c["id"])
    cache: dict = {}
    facets = {c["id"]: concept_facets(c, root, cache=cache) for c in concepts}
    return concepts, [c["id"] for c in concepts], facets


def concept_graph(root: Path | None = None) -> dict:
    """The rebuildable concept graph — `{nodes, edges, clusters}` — computed from provenance facets,
    no LLM, never stored. Order-stable (concepts sorted by id throughout)."""
    root = root or config.data_root()
    concepts, ids, facets = _facet_index(root)
    nodes = [{"id": c["id"], "title": c.get("title", ""), "facets": facets[c["id"]]} for c in concepts]
    return {"nodes": nodes,
            "edges": derived_edges(ids, facets),
            "clusters": leader_clusters(ids, facets)}


def concept_clusters(root: Path | None = None) -> list[dict]:
    """Just the cluster view — the facet-overlap leader clusters of the valid concepts."""
    root = root or config.data_root()
    _, ids, facets = _facet_index(root)
    return leader_clusters(ids, facets)


# --- CLI: dump the graph for spot-checking (mirrors the other stages' read-only inspectors) -------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="concepts",
                                 description="Dump the rebuildable concept-facet graph (no LLM).")
    ap.add_argument("--clusters", action="store_true", help="print only the facet-overlap clusters")
    ap.add_argument("--json", action="store_true", help="emit the full graph as JSON (default: a summary)")
    args = ap.parse_args(argv)

    if args.clusters:
        print(json.dumps(concept_clusters(), ensure_ascii=False, indent=2))
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
            tail = f" {e['shared']}" if e["shared"] else ""
            print(f"    {e['source']} —{e['kind']}→ {e['target']}{tail}")
    print("\n  clusters:")
    for cl in graph["clusters"]:
        print(f"    [{cl['leader']}] {cl['members']}")


if __name__ == "__main__":
    main()
