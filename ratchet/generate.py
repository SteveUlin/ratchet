"""generate — the loop-closer: project the VALID concepts into a marked CLAUDE.md region (ADR-0020).

    … review → concepts → [GENERATE: a mechanical projection] → a marked CLAUDE.md region

This is the LAST functional stage, and the one that closes the transcript → … → concept → CLAUDE.md
loop. The concept layer is ratchet's curated knowledge — the source of truth, what survives human
review. CLAUDE.md is NOT the store: it is a LEAN, GENERATED PROJECTION of the concepts, a downstream
communication channel. So generate is a MECHANICAL, deterministic, re-runnable TRANSFORM over the valid
concepts — NO LLM (the `statement` text is already human-reviewed; re-polishing it would re-introduce
an untrusted hop). It is not a Block (ADR-0009): like `review`'s gate it is a GLOBAL projection over the
whole valid set, not a per-driver pass over items.

Three properties make the projection safe to run against a real CLAUDE.md:

  THE MARKED REGION — generate owns ONLY a delimited `<!-- ratchet:generated START … -->` … `<!-- …
    END -->` span. Everything OUTSIDE the markers is human-owned and never touched. `--apply` REPLACES
    that span in place (refresh-in-place: the region is bounded, history lives in the concept blobs, not
    the file) and creates it at the end when absent. Human content above and below is byte-preserved.

  RETRACTION FOR FREE — the projection's input is `review.valid_concepts`, where a retired/superseded
    concept is simply ABSENT (its latest decision folds it out). So the next `project`/`--apply` DROPS
    its rule — a rejected belief UNMAKES its downstream config, it doesn't merely stop being added.

  THE DIFF IS THE SECOND GATE — `--diff` shows the unified diff of the proposed region vs the target's
    current one. The concept gate (review, ADR-0008) decides what is TRUE; this generation gate decides
    what lands in a real CLAUDE.md. The default `--target` is a STAGED path under the data root, so the
    default NEVER clobbers a real CLAUDE.md — the human points `--target` at one deliberately.

Deferred (v1 is one marked CLAUDE.md region): skills / `.claude/rules/*`, repo-SCOPING (which CLAUDE.md
a concept belongs to), and any LLM polish of the rule text — all deliberate follow-ups (ADR-0020).
"""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path

from . import concepts, config

# The region delimiters. generate owns ONLY the span between them; the START marker self-documents that
# the region is machine-managed so a human who opens the file knows their edits inside it are transient.
START = ("<!-- ratchet:generated START — managed by `ratchet generate --apply`; "
         "edits here are overwritten -->")
END = "<!-- ratchet:generated END -->"

# The empty projection: a well-formed region that MEANS "every rule retracted / nothing reviewed yet",
# never an absent region. So an empty concept set still refreshes deterministically (idempotent) and a
# reader sees the projection IS empty rather than wondering whether generate ran.
EMPTY_BODY = "<!-- no valid concepts yet — this projection is empty (every rule retracted) -->"

GENERATED_SUBDIR = "generated"      # the staged target lives under the data root, never beside real code
GENERATED_FILENAME = "CLAUDE.md"

# The trailing bucket for concepts with no managed tag — every concept lands in EXACTLY one group, so an
# untagged one needs a home; `general` is it (always rendered LAST). A real `general` tag folds here too.
GENERAL_GROUP = "general"


def default_target(root: Path | None = None) -> Path:
    """The SAFE default `--target`: a staged `$RATCHET_DATA_DIR/generated/CLAUDE.md`. The default must
    NEVER write a real CLAUDE.md — a projection is a downstream channel, and clobbering a human's config
    by default would make the safe path the dangerous one. The human copies the region (or points
    `--target` here) deliberately when they want it live."""
    return (root or config.data_root()) / GENERATED_SUBDIR / GENERATED_FILENAME


# --- the projection: valid concepts → tag-led, provenance-marked rules ----------------------------

def _trigger(repos: list[str]) -> str:
    """The WHERE prefix for a rule — "When working in `<repo>`: " (AutoGuide state-conditioning): a rule
    fires more usefully when the reader knows WHERE it applies. The THEME now lives in the group HEADING
    (the tag), so the trigger carries ONLY the repo — the strongest "where am I" provenance facet — no
    longer the tag (that would just echo the heading). A concept spanning several repos takes the first
    (sorted, deterministic); a path-shaped repo is basenamed so no `/home/sulin/…` leaks into a rule.
    Returns a prefix ending in ": " (the verbatim statement follows), or "" when the concept has no repo —
    an unconditional rule then renders as just its statement."""
    if not repos:
        return ""
    return f"When working in `{Path(repos[0]).name}`: "


def _render_body(ctx: dict) -> str:
    """Render the valid concepts as TAG-LED groups — the region BODY (no markers). Concepts are GROUPED BY
    their PRIMARY tag (`facets["tags"][0]`, the gardener's managed theme — 3b/ADR-0014; the facets ride
    sorted, so "first" is deterministic), each group a markdown `## <tag>` section — THEME-shaped, the way a
    human-written CLAUDE.md reads, not the old provenance cluster. An UNTAGGED concept falls to a trailing
    `## general` bucket, so every concept lands in EXACTLY ONE group (a clean partition). The heading carries
    the theme; each bullet carries only the WHERE — its repo trigger + its `statement` VERBATIM (already
    human-reviewed — re-wording it would re-open an untrusted hop) + a trailing `<!-- c-id -->` provenance
    marker. That marker is the trust chain reaching the projection: a reader greps the id back to its concept
    → evidence → raw transcript. An HTML comment, not visible noise, so the rule reads clean while staying
    traceable in source.

    DETERMINISTIC + order-stable: groups by descending size then tag name (the `general` bucket always last),
    members by entrenchment (distinct cited sessions desc) then id — same valid set → byte-identical body,
    which is what makes `--apply` idempotent."""
    by_node, blobs = ctx["by_node"], ctx["blobs"]
    if not by_node:
        return EMPTY_BODY

    # PARTITION every concept onto its primary tag; untagged → the `general` bucket. One group per concept.
    groups: dict[str, list[str]] = {}
    for cid in by_node:
        tags = by_node[cid]["facets"].get("tags") or []
        groups.setdefault(tags[0] if tags else GENERAL_GROUP, []).append(cid)

    def entrench(cid: str) -> int:                   # distinct cited sessions — the corroboration depth
        return len(by_node[cid]["facets"].get("sessions") or ())

    # GROUP order: `general` always LAST (the catch-all reads last); the rest by descending size, then name.
    def group_key(tag: str) -> tuple:
        return (1, 0, "") if tag == GENERAL_GROUP else (0, -len(groups[tag]), tag)

    lines: list[str] = []
    for tag in sorted(groups, key=group_key):
        if lines:
            lines.append("")                         # a blank line separates groups
        lines.append(f"## {tag}")
        for cid in sorted(groups[tag], key=lambda c: (-entrench(c), c)):
            facets = by_node[cid]["facets"]
            statement = str(blobs.get(cid, {}).get("statement", "")).strip()
            lines.append(f"- {_trigger(facets.get('repos') or [])}{statement} <!-- {cid} -->")
    return "\n".join(lines)


def project(root: Path | None = None) -> str:
    """The MECHANICAL projection — the full marked region (START + tag-led rule body + END) for the current
    VALID concept set. Built from `concepts.digest_context` (ONE facet pass — the gardener's managed tags
    ride on each node's facets, so the primary-tag grouping reads straight off it), so retraction is
    automatic: a concept absent from the valid set is absent here. Deterministic — same store → byte-identical
    region — so `--apply` with unchanged concepts is a no-op. NO LLM."""
    ctx = concepts.digest_context(root or config.data_root())
    return f"{START}\n{_render_body(ctx)}\n{END}"


# --- the faithfulness context: each projected concept's statement + verified evidence -------------

def projected_concepts(root: Path | None = None) -> list[dict]:
    """The FAITHFULNESS context for the projection — each VALID concept (the ones the region renders),
    paired with the verified EVIDENCE behind its statement. Read-only, NO LLM: `valid_concepts` enriched
    with the `concepts.digest_context` facets (`repos`/`tags` — the grouping + repo-trigger inputs) and
    `review.resolve_evidence` (spans RE-VALIDATED, a stale one dropped — the same verified quotes the
    review gate is served). The `/ratchet-generate` skill reads this to keep a re-worded rule TRUE to its
    source: the region's `<!-- c-id -->` marker maps to an `id` here, whose `statement` is the verbatim
    text the mechanical render uses and whose `evidence` quotes are the trust chain reaching the
    projection. Sorted by id, so the dump is order-stable like every other ratchet view.

    `review` is imported FUNCTION-LOCALLY — review doesn't import generate (no cycle), but the lazy import
    matches the convention used for the other cross-module reads and keeps the module's import graph flat."""
    from . import review                         # function-local: review serves the canonical evidence resolver
    root = root or config.data_root()
    by_node = concepts.digest_context(root)["by_node"]
    out: list[dict] = []
    for c in sorted(review.valid_concepts(root), key=lambda c: c["id"]):
        facets = by_node.get(c["id"], {}).get("facets", {})
        out.append({
            "id": c["id"],
            "title": c.get("title", ""),
            "statement": c.get("statement", ""),
            "repos": facets.get("repos") or [],
            "tags": facets.get("tags") or [],
            "evidence": review.resolve_evidence(c, root),   # pointers → re-validated verbatim quotes
        })
    return out


# --- the marked region: locate, splice (refresh-in-place), diff -----------------------------------

def _region_span(text: str) -> tuple[int, int] | None:
    """The [start, end) byte span of the existing ratchet region in `text` — from the START marker to just
    past the END marker — or None when absent. A legitimately generated region has EXACTLY one START and one
    END, in order (`project` emits one each). Anything else — a LONE, DUPLICATED, or out-of-order marker — is
    a CORRUPTED or AMBIGUOUS region (a stale second region, or a file that documents the markers in a code
    block), and `find`-the-first would clobber the WRONG span: so the splice caller RAISES rather than guess.
    Refusing on a count mismatch is what makes "human content is never clobbered" structural, not probable."""
    sc, ec = text.count(START), text.count(END)
    if sc == 0 and ec == 0:
        return None
    if sc != 1 or ec != 1 or text.find(END) < text.find(START):
        raise ValueError("target has an ambiguous or malformed ratchet:generated region (a lone, duplicated, "
                         "or out-of-order marker) — refusing to overwrite; fix or remove the markers by hand")
    return text.find(START), text.find(END) + len(END)


def current_region(text: str) -> str:
    """The region as it stands in `text` (START..END inclusive), or "" when absent — what `--diff` compares
    the proposal against. A read-only locate; the malformed-marker guard rides in `_region_span`."""
    span = _region_span(text)
    return text[span[0]:span[1]] if span else ""


def _splice(text: str, region: str) -> tuple[str, str]:
    """REFRESH-IN-PLACE: return (new_text, action). When the region exists, replace exactly its span —
    everything before START and after END is preserved BYTE-FOR-BYTE (human content is never touched). When
    absent, APPEND it at the end, separated by a blank line from any existing content. The spacing is chosen
    so a second splice finds the same span and produces byte-identical output (idempotent)."""
    span = _region_span(text)
    if span is not None:
        return text[:span[0]] + region + text[span[1]:], "replaced"
    if text == "":
        return region + "\n", "created"             # brand-new file: just the region + a final newline
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + region + "\n", "appended"


def apply(root: Path | None = None, *, target: Path | str | None = None) -> dict:
    """Write the projection into `target`'s marked region (refresh-in-place). Reads the current file (or ""
    when absent), splices in `project(root)`, writes only when the bytes CHANGE (so re-apply with unchanged
    concepts touches nothing). The staged default target is created on demand; a real CLAUDE.md is written
    only because the human pointed `--target` at it. Returns {target, action, changed}."""
    root = root or config.data_root()
    tgt = Path(target) if target is not None else default_target(root)
    old = tgt.read_text() if tgt.exists() else ""
    new, action = _splice(old, project(root))
    tgt.parent.mkdir(parents=True, exist_ok=True)
    changed = new != old
    if changed:
        tgt.write_text(new)
    return {"target": str(tgt), "action": action, "changed": changed}


def diff(root: Path | None = None, *, target: Path | str | None = None) -> str:
    """A unified diff of the PROPOSED region vs the target's CURRENT one — what `--apply` would change, for
    the human to review BEFORE applying (the second review tier). Empty string when they already match."""
    root = root or config.data_root()
    tgt = Path(target) if target is not None else default_target(root)
    old = tgt.read_text() if tgt.exists() else ""
    proposed = project(root)
    lines = difflib.unified_diff(current_region(old).split("\n"), proposed.split("\n"),
                                 fromfile=f"{tgt} (current region)", tofile=f"{tgt} (proposed region)",
                                 lineterm="")
    return "\n".join(lines)


# --- CLI: stage / diff / apply — the generation gate ----------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="generate",
        description="Project the valid concepts into a marked CLAUDE.md region (mechanical, no LLM).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="print the projected region to stdout (default; no write)")
    g.add_argument("--diff", action="store_true",
                   help="unified diff of the proposed region vs the target's current one (review before --apply)")
    g.add_argument("--apply", action="store_true",
                   help="write the projection into the target's marked region (refresh-in-place; human content preserved)")
    g.add_argument("--concepts", action="store_true",
                   help="JSON: each projected concept's statement + re-validated evidence (the faithfulness "
                        "context the /ratchet-generate skill reads; read-only, no LLM)")
    ap.add_argument("--target",
                    help="the file whose ratchet:generated region to manage (default: a STAGED "
                         "$RATCHET_DATA_DIR/generated/CLAUDE.md that never clobbers a real CLAUDE.md)")
    args = ap.parse_args(argv)
    root = config.data_root()
    target = args.target            # None → the staged default, resolved inside apply/diff

    try:                                # a corrupted/ambiguous target region raises — surface it cleanly,
        if args.apply:                  # not as a traceback; the file is never written on the raise path
            res = apply(root, target=target)
            print(f"{res['action']} ratchet region in {res['target']}"
                  + ("" if res["changed"] else " (no change — byte-identical)"))
        elif args.concepts:
            print(json.dumps(projected_concepts(root), ensure_ascii=False, indent=2))
        elif args.diff:
            d = diff(root, target=target)
            tgt = target if target is not None else default_target(root)
            print(d if d.strip() else f"no changes — {tgt}'s region already matches the projection")
        else:
            print(project(root))
    except ValueError as e:
        raise SystemExit(f"generate: {e}")


if __name__ == "__main__":
    main()
