"""status — the operator's read-only pipeline census: where does the backlog sit?

    tap → weave → chunk → glean → resolve → review → garden → generate

The pipeline is hand-run (cadence is documentation, not a typed API — design doc §8), so the
operator's scheduling signal is a QUESTION, not a scheduler: which stage has un-drained work?
This command answers it in one read-only pass — NO LLM, no writes, every number a derived fold
over the same views the stages themselves read (never a parallel bookkeeping structure that
could desync):

  SOURCES   transcripts tapped (raw `transcript` sources) vs available (tap's own `discover`
            sweep — same self-skip default, ADR-0025).
  PREP      cleaned blobs woven, chunksets materialized, and the glean done-split: chunks with a
            `processed` marker under the CURRENT prompt_version (`block.done_index` — a prompt
            bump honestly re-opens the backlog) vs pending.
  EVENTS    total extracted vs still in `dream.working_set` (un-consolidated → resolve's queue).
  CLAIMS    the L1 pool (`resolve.claim_pool`) split by the derived predicates: active/dormant
            (`is_active`), mature (net entrenchment >= the bar), matured-awaiting-synthesize
            (mature AND why=null — the §7.3 "why pending" population, counted explicitly),
            accepted (a binding accept/edit decision), contested (live contradicts edges); plus
            the live edge census (llm-adjudicated vs total — how much of the graph the matcher
            built vs $0 seeds).
  REVIEW    tier-1 pending + incubating, tier-2 open structural-op proposals.
  CONCEPTS  the valid set, split by kind (behavioral — the generate surface — vs reference, ADR-0029).
  GENERATE  would the projected region be non-empty (generate's own `project`, never written).

Every section DEGRADES to zeros: a stage with no data (or a mid-edit module) prints a zero-line,
never a traceback — a census must always answer. The maturity bar is the reviewer's knob here
too (`--maturity`, ADR-0027), so the mature count agrees with whatever bar review runs at.

    python -m ratchet.status            # the census, one line per stage
    python -m ratchet.status --json     # the same numbers as one object
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import blobstore, block, chunk, config, dream, glean, resolve, tap, weave

# The zero shape of every section — what a data-less (or failing) stage reports. One source of
# truth for both the graceful degrade and the --json schema (a consumer can rely on the keys).
ZEROS: dict[str, dict] = {
    "sources": {"tapped": 0, "available": 0},
    "prep": {"woven": 0, "chunksets": 0, "chunks": 0, "chunks_gleaned": 0, "chunks_pending": 0},
    "events": {"total": 0, "awaiting_resolve": 0},
    "claims": {"total": 0, "active": 0, "dormant": 0, "mature": 0, "awaiting_synthesize": 0,
               "accepted": 0, "contested": 0, "edges": 0, "llm_edges": 0},
    "review": {"pending": 0, "incubating": 0, "proposals": 0},
    "concepts": {"valid": 0, "behavioral": 0, "reference": 0},
    "generate": {"region_nonempty": False, "rules": 0},
}


def _section(fn, zeros: dict) -> dict:
    """Run one section's fold; ANY failure degrades to its zero shape (missing keys filled too).
    The census must always answer — an empty store, a TTL-reclaimed blob, or a sibling module
    mid-refactor yields a zero-line, never a traceback."""
    try:
        out = fn()
    except Exception:
        return dict(zeros)
    return {**zeros, **out}


# --- the sections: each a derived fold over the views the stages themselves read -------------------

def _sources(root: Path, datastore: Path) -> dict:
    """Tapped = distinct raw `transcript` sources in the store; available = tap's own discover sweep
    over the datastore (same self-skip default — ratchet's own completer runs don't count as backlog,
    ADR-0025). Available < tapped is normal: fixtures and deleted transcripts stay tapped forever."""
    tapped = len(blobstore.latest_by_kind("transcript", root))
    available = sum(1 for _ in tap.discover(datastore, skip_self=config.data_root()))
    return {"tapped": tapped, "available": available}


def _prep(root: Path) -> dict:
    """The deterministic prep ledger + glean's done-split. A chunk counts as GLEANED when a
    `processed` marker exists for its key under the CURRENT `glean.PROMPT_VERSION` (any model) —
    the same done-index the driver skips on, so a prompt bump honestly re-opens the backlog."""
    woven = sum(1 for m in blobstore.iter_meta(root)
                if m.get("kind") == "derived" and m.get("format") == weave.RENDER_FORMAT)
    chunksets = glean.all_chunksets(root)
    keys: set[str] = set()
    for cs in chunksets:
        try:
            keys.update(glean.chunk_key(ch) for ch in chunk.load(cs, root))
        except (OSError, json.JSONDecodeError, KeyError):
            continue                               # a reclaimed/malformed chunkset is not a crash
    done = {k[0] for k in block.done_index("glean", root)
            if len(k) >= 2 and k[1] == glean.PROMPT_VERSION}
    gleaned = sum(1 for k in keys if k in done)
    return {"woven": woven, "chunksets": len(chunksets), "chunks": len(keys),
            "chunks_gleaned": gleaned, "chunks_pending": len(keys) - gleaned}


def _events(root: Path) -> dict:
    return {"total": len(blobstore.latest_by_kind("event", root)),
            "awaiting_resolve": len(dream.working_set(root))}


def _claims(root: Path, maturity: float) -> dict:
    """The claim pool through its derived predicates — nothing here is a stored status (§2.1):
    active/mature/contested all recompute from live edges + valid-times, exactly as resolve reads
    them. `awaiting_synthesize` = mature AND why=null — the §7.3 deferred-synthesize queue, bounded
    by the graduation rate; it is also review's "why pending" badge population (§6)."""
    now = config.now()
    valid_times = dream._session_valid_times(root)
    decisions = blobstore.latest_decisions(root)
    pool = resolve.claim_pool(root)
    active = sum(1 for c in pool
                 if resolve.is_active(c, now=now, valid_times=valid_times))
    mature = [c for c in pool
              if dream.net_entrenchment(c, now, valid_times=valid_times) >= maturity]
    awaiting = sum(1 for c in mature if c.get("why") is None)
    accepted = 0
    for c in pool:
        d = decisions.get(c["id"])
        if d and d.get("verb") in resolve.ACCEPT_VERBS and resolve._decision_binds(d, c):
            accepted += 1
    contested = sum(1 for c in pool if c.get("contradicted_by"))
    edges = [e for es in resolve._live_edges(root, resolve._reject_merge_facts(root)).values()
             for e in es]
    llm = sum(1 for e in edges if (e.get("match") or {}).get("by") == "llm")
    return {"total": len(pool), "active": active, "dormant": len(pool) - active,
            "mature": len(mature), "awaiting_synthesize": awaiting,
            "accepted": accepted, "contested": contested,
            "edges": len(edges), "llm_edges": llm}


def _review(root: Path, maturity: float) -> dict:
    """Both review tiers, counted from the same derived queues the gate serves. `review`/`garden`
    import lazily so a sibling module mid-edit degrades this section, not the whole census; each
    count guards independently (tier-2 must not vanish because tier-1 raised)."""
    out = dict(ZEROS["review"])
    try:
        from . import review
        try:
            out["pending"] = len(review.pending(root, context_bytes=0, maturity=maturity))
        except TypeError:                          # a signature drift — fall back to the defaults
            out["pending"] = len(review.pending(root))
        out["incubating"] = len(review.incubating(root, maturity=maturity))
    except Exception:
        pass
    try:
        from . import garden
        out["proposals"] = len(garden.open_proposals(root))
    except Exception:
        pass
    return out


def _concepts(root: Path) -> dict:
    """The valid set, split by KIND (ADR-0029) — behavioral is what generate projects by default;
    reference is kept lookup material. Same derivation the projection filters on (`load_concepts`
    attaches each concept's decision-folded kind), so the split and the region always agree."""
    cs = dream.load_concepts(root)
    ref = sum(1 for c in cs if c.get("kind") == dream.KIND_REFERENCE)
    return {"valid": len(cs), "behavioral": len(cs) - ref, "reference": ref}


def _generate(root: Path) -> dict:
    """Would the projection land anything? Reuses generate's OWN `project` (the mechanical render,
    no write, no target touched), so this answer and a later `--apply` can never disagree. `rules`
    counts the region's bullet lines — one per projected concept."""
    from . import generate
    region = generate.project(root)
    nonempty = generate.EMPTY_BODY not in region
    rules = sum(1 for ln in region.splitlines() if ln.startswith("- "))
    return {"region_nonempty": nonempty, "rules": rules}


def census(root: Path | None = None, *, datastore: Path = tap.DEFAULT_DATASTORE,
           maturity: float = dream.MATURITY_WEIGHT) -> dict:
    """The whole census as ONE object (the --json payload; the text render reads this same dict —
    one derivation, two presentations). Read-only, no LLM; every section degrades to `ZEROS`."""
    root = root or config.data_root()
    return {
        "sources": _section(lambda: _sources(root, datastore), ZEROS["sources"]),
        "prep": _section(lambda: _prep(root), ZEROS["prep"]),
        "events": _section(lambda: _events(root), ZEROS["events"]),
        "claims": _section(lambda: _claims(root, maturity), ZEROS["claims"]),
        "review": _section(lambda: _review(root, maturity), ZEROS["review"]),
        "concepts": _section(lambda: _concepts(root), ZEROS["concepts"]),
        "generate": _section(lambda: _generate(root), ZEROS["generate"]),
    }


# --- the text render: one line per stage, backlog numbers highlighted where the operator acts ------

def render(c: dict, *, datastore: Path, maturity: float) -> str:
    s, p, e, cl, rv, co, g = (c["sources"], c["prep"], c["events"], c["claims"],
                              c["review"], c["concepts"], c["generate"])
    lines = [
        f"SOURCES   {s['tapped']} tapped · {s['available']} available under {datastore}",
        f"PREP      {p['woven']} woven · {p['chunksets']} chunkset(s) · "
        f"chunks: {p['chunks_gleaned']}/{p['chunks']} gleaned, {p['chunks_pending']} pending",
        f"EVENTS    {e['total']} total · {e['awaiting_resolve']} awaiting resolve",
        f"CLAIMS    {cl['total']} total · {cl['active']} active / {cl['dormant']} dormant · "
        f"{cl['mature']} mature (bar {maturity:g}) · {cl['awaiting_synthesize']} awaiting synthesize "
        f"(why=null) · {cl['accepted']} accepted · {cl['contested']} contested",
        f"          edges: {cl['edges']} live · {cl['llm_edges']} by llm",
        f"REVIEW    tier-1: {rv['pending']} pending · {rv['incubating']} incubating · "
        f"tier-2: {rv['proposals']} proposal(s)",
        f"CONCEPTS  {co['valid']} valid ({co['behavioral']} behavioral · {co['reference']} reference)",
        f"GENERATE  region would be {'NON-EMPTY' if g['region_nonempty'] else 'empty'}"
        + (f" ({g['rules']} rule(s))" if g["region_nonempty"] else ""),
    ]
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="status",
        description="Read-only pipeline census: where the backlog sits, stage by stage. No LLM, no writes.")
    ap.add_argument("--json", action="store_true", help="emit the census as one JSON object")
    ap.add_argument("--datastore", type=Path, default=tap.DEFAULT_DATASTORE,
                    help=f"transcript root the SOURCES line sweeps (default: {tap.DEFAULT_DATASTORE})")
    ap.add_argument("--maturity", type=float, default=dream.MATURITY_WEIGHT, metavar="BAR",
                    help=f"the maturity bar the mature/pending counts use (default "
                         f"{dream.MATURITY_WEIGHT} — the reviewer's knob, ADR-0027; keep it in step "
                         f"with the bar you review at so the census and the queue agree)")
    args = ap.parse_args(argv)
    c = census(datastore=args.datastore, maturity=args.maturity)
    if args.json:
        print(json.dumps(c, ensure_ascii=False, indent=2))
    else:
        print(render(c, datastore=args.datastore, maturity=args.maturity))


if __name__ == "__main__":
    main()
