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

Repo-SCOPING landed (ADR-0030): each concept carries a reviewer-confirmed scope, the default
projection takes `global` only, and `--repo X` projects that repo's concepts for ITS CLAUDE.md
(`--target ~/X/CLAUDE.md`). Still deferred (ADR-0020): skills / `.claude/rules/*`, LLM polish.

The REFERENCE SHEET (`--reference`, the ADR-0029 follow-up surface): kind filtering solved WHERE
reference concepts DON'T go (CLAUDE.md), which left them with no surface at all — kept + queryable,
never rendered. `--reference` projects EXACTLY the kind==reference concepts (every scope, grouped
by scope with global first) into the same marked-region machinery, as a LOOKUP SHEET, not rules:
each entry a compact fact line (title, the why beneath when present, the id for provenance). Its
staged default target sits beside the rules one (`generated/reference.md`), and `--diff`/`--apply`
work unchanged. It refuses `--kinds`/`--repo`: the sheet's filter IS "reference, every scope", so
those flags would contradict it (ADR-0027 — refuse loudly, never silently reinterpret).
"""
from __future__ import annotations

import argparse
import difflib
import json
from collections import Counter
from pathlib import Path

from . import concepts, config, dream

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
REFERENCE_FILENAME = "reference.md"  # the reference sheet's staged sibling (see project_reference)

# The reference sheet's header note — a reader landing on the sheet learns what it is and where the
# OTHER surface lives, the mirror of the rules region's kinds note pointing here.
REFERENCE_NOTE = "<!-- reference facts — lookup material; behavioral rules live in CLAUDE.md -->"

# The reference sheet's empty-body sentinel — same principle as EMPTY_BODY: an empty sheet MEANS
# "no reference concepts", never an absent region, so --apply stays idempotent from day zero.
EMPTY_REFERENCE_BODY = "<!-- no reference concepts yet — this sheet is empty -->"

# The trailing bucket for concepts with no managed tag — every concept lands in EXACTLY one group, so an
# untagged one needs a home; `general` is it (always rendered LAST). A real `general` tag folds here too.
GENERAL_GROUP = "general"

# The projection's KIND filter (ADR-0029): `behavioral` ONLY by default. The rules budget — a reader's
# attention inside CLAUDE.md — is behavioral surface: rules that shape conduct. A `reference` concept
# (a mechanism/fact you'd look up) is just as true and stays in the concept layer, kept + queryable,
# but projecting it would spend rule-attention on lookup material. `--kinds` widens deliberately; the
# region's header comment states the filter, so a CLAUDE.md reader knows what is (not) shown.
DEFAULT_KINDS = (dream.KIND_BEHAVIORAL,)


def _normalize_kinds(kinds) -> tuple[str, ...]:
    """Validate + canonicalize a kind selection: unknown kinds (or an empty selection) are REFUSED —
    the flag is a reviewer-facing knob, and a typo silently projecting nothing would be the exact
    hidden-rule failure ADR-0027 forbids — and the survivors take CONCEPT_KINDS order, so
    `--kinds reference,behavioral` and `--kinds behavioral,reference` render byte-identically
    (idempotent `--apply`)."""
    picked = set(kinds)
    unknown = picked - set(dream.CONCEPT_KINDS)
    if unknown or not picked:
        raise ValueError(f"--kinds takes a non-empty subset of {','.join(dream.CONCEPT_KINDS)}; "
                         f"got {','.join(sorted(picked)) or '(nothing)'!r}")
    return tuple(k for k in dream.CONCEPT_KINDS if k in picked)


def _kinds_note(kinds: tuple[str, ...], excluded: Counter) -> str:
    """The region's second header line: WHICH kinds this projection carries and what it left out — so a
    CLAUDE.md reader knows the region is a filtered view, not the whole concept layer. Deterministic
    (counts derive from the store)."""
    shown = ", ".join(kinds)
    if not excluded:
        return f"<!-- kinds: {shown} -->"
    ex = " · ".join(f"{excluded[k]} {k}" for k in dream.CONCEPT_KINDS if excluded.get(k))
    return (f"<!-- kinds: {shown} — {ex} concept(s) excluded: lookup material, not conduct "
            f"(`--reference` renders them as a lookup sheet; `--kinds` widens) -->")


# The projection's SCOPE filter (ADR-0030), the kind filter's mirror on the WHERE axis: `global`
# only by default — the global CLAUDE.md gets only what applies everywhere. A repo-scoped concept
# (its reviewer-confirmed scope names a repo) is just as valid; it belongs in THAT repo's CLAUDE.md,
# so `--repo X` projects behavioral ∧ scope=X instead (point `--target` at ~/X/CLAUDE.md). The
# region's header states the filter beside the kinds note.

def _normalize_scope(scope) -> str:
    """Validate a scope selection: a blank scope is REFUSED (the flag is reviewer-facing; a typo
    silently projecting nothing is the hidden-rule failure ADR-0027 forbids). The vocabulary is
    open — the membership check against the store rides in `project` where the valid set is known."""
    s = scope.strip() if isinstance(scope, str) else ""
    if not s:
        raise ValueError("--repo takes a repo label (a concept scope) — got an empty string")
    return s


def _scopes_present(scope_by_id: dict[str, str]) -> str:
    """The helpful listing a refused `--repo` gets: every scope the valid set actually carries, with
    counts — so the operator sees what to type instead of guessing (ADR-0027)."""
    counts = Counter(scope_by_id.values())
    order = sorted(counts, key=lambda s: (s != dream.SCOPE_GLOBAL, s))   # global first, then names
    return ", ".join(f"{s}×{counts[s]}" for s in order) or "(no valid concepts)"


def _scope_note(scope: str, excluded: Counter) -> str:
    """The region's scope line, beside the kinds note: WHICH scope this projection carries and how
    many concepts live elsewhere — a reader of the global CLAUDE.md learns that repo-local rules
    exist without seeing them, and a repo's region names itself. Deterministic like `_kinds_note`."""
    if not excluded:
        return f"<!-- scope: {scope} -->"
    order = sorted(excluded, key=lambda s: (s != dream.SCOPE_GLOBAL, s))
    ex = " · ".join(f"{s}×{excluded[s]}" for s in order)
    if scope == dream.SCOPE_GLOBAL:
        return (f"<!-- scope: global — {sum(excluded.values())} concept(s) scoped to a repo ({ex}): "
                f"repo-local lessons; `--repo <name>` projects them into that repo's CLAUDE.md -->")
    return (f"<!-- scope: {scope} — this repo's view; {sum(excluded.values())} concept(s) "
            f"elsewhere ({ex}) -->")


def default_target(root: Path | None = None) -> Path:
    """The SAFE default `--target`: a staged `$RATCHET_DATA_DIR/generated/CLAUDE.md`. The default must
    NEVER write a real CLAUDE.md — a projection is a downstream channel, and clobbering a human's config
    by default would make the safe path the dangerous one. The human copies the region (or points
    `--target` here) deliberately when they want it live."""
    return (root or config.data_root()) / GENERATED_SUBDIR / GENERATED_FILENAME


def default_reference_target(root: Path | None = None) -> Path:
    """The reference sheet's SAFE default `--target` — the staged `generated/reference.md`, the rules
    projection's sibling, for the same reason: the default never writes a real config file."""
    return (root or config.data_root()) / GENERATED_SUBDIR / REFERENCE_FILENAME


# --- the projection: valid concepts → tag-led, provenance-marked rules ----------------------------

def _trigger(repos: list[str]) -> str:
    """The WHERE prefix for a rule — "When working in `<repo>`: " (AutoGuide state-conditioning): a rule
    fires more usefully when the reader knows WHERE it applies. The THEME now lives in the group HEADING
    (the tag), so the trigger carries ONLY the repo — the strongest "where am I" provenance facet — no
    longer the tag (that would just echo the heading). A concept spanning several repos takes the first
    (sorted, deterministic); a path-shaped repo is basenamed so no `/home/<user>/…` leaks into a rule.
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


def project(root: Path | None = None, *, kinds: tuple[str, ...] = DEFAULT_KINDS,
            scope: str = dream.SCOPE_GLOBAL) -> str:
    """The MECHANICAL projection — the full marked region (START + kinds note + scope note + tag-led
    rule body + END) for the current VALID concept set of the selected `kinds` (behavioral-only by
    default — see DEFAULT_KINDS) and `scope` (global by default — see the scope-filter note above;
    both header lines state their filter and what it excluded). Built from `concepts.digest_context`
    (ONE facet pass — the gardener's managed tags ride on each node's facets, so the primary-tag
    grouping reads straight off it), so retraction is automatic: a concept absent from the valid set
    is absent here, and so is one the reviewer re-kinds `reference` (`--set-kind`) or re-scopes to a
    repo (`--set-scope`). A `scope` naming NO concept's scope is REFUSED with the scopes present —
    a typo'd --repo silently projecting nothing would be a hidden rule (ADR-0027). Deterministic —
    same store → byte-identical region — so `--apply` with unchanged concepts is a no-op. NO LLM."""
    kinds = _normalize_kinds(kinds)
    scope = _normalize_scope(scope)
    ctx = concepts.digest_context(root or config.data_root())
    # each node's derived kind/scope rides ctx["blobs"] (dream.load_concepts attaches them); a node
    # whose blob is gone reads behavioral/global — the recall-safe defaults (rendered, caught at the
    # diff gate). SCOPE filters first (where does it apply), kinds second (does it shape conduct),
    # so each note counts exactly its own exclusions — the two partition the drop.
    kind_of = {cid: dream.clean_kind(ctx["blobs"].get(cid, {}).get("kind")) for cid in ctx["by_node"]}
    scope_of = {cid: dream.clean_scope(ctx["blobs"].get(cid, {}).get("scope")) for cid in ctx["by_node"]}
    if scope != dream.SCOPE_GLOBAL and scope not in scope_of.values():
        raise ValueError(f"--repo {scope!r} matches no concept's scope — scopes present: "
                         f"{_scopes_present(scope_of)}")
    scope_excluded = Counter(s for s in scope_of.values() if s != scope)
    in_scope = {cid for cid, s in scope_of.items() if s == scope}
    kind_excluded = Counter(kind_of[cid] for cid in in_scope if kind_of[cid] not in kinds)
    ctx = {**ctx, "by_node": {cid: n for cid, n in ctx["by_node"].items()
                              if cid in in_scope and kind_of[cid] in kinds}}
    return (f"{START}\n{_kinds_note(kinds, kind_excluded)}\n{_scope_note(scope, scope_excluded)}\n"
            f"{_render_body(ctx)}\n{END}")


def project_reference(root: Path | None = None) -> str:
    """The REFERENCE SHEET — the marked region for EXACTLY the kind==reference concepts, EVERY scope
    (the ADR-0029 follow-up: the kind filter kept them out of CLAUDE.md; this gives them their own
    surface). A LOOKUP sheet, not rules, so the shape differs from `_render_body`'s: grouped by
    SCOPE (`## global` first, then repo names sorted — where a fact applies is the lookup axis; the
    sheet is one file, unlike the per-repo rules regions), each entry a compact fact line — the
    bolded TITLE (the lookup key), the `statement` (the concept's why) beneath when present, and the
    `<!-- c-id -->` provenance marker reaching back to evidence. Same trust boundary as `project`:
    both texts are the reviewer's verbatim words, NO LLM. Same determinism (entrenchment desc, then
    id, within a group → byte-identical re-render → idempotent `--apply`), same retraction-for-free
    (a retired or re-kinded-behavioral concept simply stops appearing). A blob-less node reads
    `behavioral` (dream's recall-first coercion) and lands in the RULES region, not here — the two
    projections partition the valid set the same way status counts it."""
    ctx = concepts.digest_context(root or config.data_root())
    by_node, blobs = ctx["by_node"], ctx["blobs"]
    refs = [cid for cid in by_node
            if dream.clean_kind(blobs.get(cid, {}).get("kind")) == dream.KIND_REFERENCE]
    if not refs:
        return f"{START}\n{REFERENCE_NOTE}\n{EMPTY_REFERENCE_BODY}\n{END}"

    groups: dict[str, list[str]] = {}
    for cid in refs:
        groups.setdefault(dream.clean_scope(blobs.get(cid, {}).get("scope")), []).append(cid)

    def entrench(cid: str) -> int:
        return len(by_node[cid]["facets"].get("sessions") or ())

    lines: list[str] = []
    for scope in sorted(groups, key=lambda s: (s != dream.SCOPE_GLOBAL, s)):   # global first
        if lines:
            lines.append("")
        lines.append(f"## {scope}")
        for cid in sorted(groups[scope], key=lambda c: (-entrench(c), c)):
            blob = blobs.get(cid, {})
            title = str(blob.get("title", "")).strip() or cid   # a title-less blob still gets a line
            why = str(blob.get("statement", "")).strip()
            lines.append(f"- **{title}** <!-- {cid} -->")
            if why:
                lines.append(f"  {why}")
    return f"{START}\n{REFERENCE_NOTE}\n" + "\n".join(lines) + f"\n{END}"


# --- the faithfulness context: each projected concept's statement + verified evidence -------------

def projected_concepts(root: Path | None = None, *,
                       kinds: tuple[str, ...] = DEFAULT_KINDS,
                       scope: str = dream.SCOPE_GLOBAL,
                       reference: bool = False) -> list[dict]:
    """The FAITHFULNESS context for the projection — each VALID concept (the ones the region renders:
    same `kinds` + `scope` filters as `project`, behavioral ∧ global by default, same unknown-scope
    refusal), paired with the verified EVIDENCE
    behind its statement. Read-only, NO LLM: `valid_concepts` enriched
    with the `concepts.digest_context` facets (`repos`/`tags` — the grouping + repo-trigger inputs) and
    `review.resolve_evidence` (spans RE-VALIDATED, a stale one dropped — the same verified quotes the
    review gate is served). The `/ratchet-generate` skill reads this to keep a re-worded rule TRUE to its
    source: the region's `<!-- c-id -->` marker maps to an `id` here, whose `statement` is the verbatim
    text the mechanical render uses and whose `evidence` quotes are the trust chain reaching the
    projection. Sorted by id, so the dump is order-stable like every other ratchet view.

    `reference=True` serves the REFERENCE SHEET's context instead — the same rows for exactly the
    concepts `project_reference` renders (kind==reference, EVERY scope, so the scope filter and its
    unknown-scope refusal don't participate).

    `review` is imported FUNCTION-LOCALLY — review doesn't import generate (no cycle), but the lazy import
    matches the convention used for the other cross-module reads and keeps the module's import graph flat."""
    from . import review                         # function-local: review serves the canonical evidence resolver
    kinds = _normalize_kinds(kinds)
    scope = _normalize_scope(scope)
    root = root or config.data_root()
    by_node = concepts.digest_context(root)["by_node"]
    valid = sorted(review.valid_concepts(root), key=lambda c: c["id"])
    scope_of = {c["id"]: dream.clean_scope(c.get("scope")) for c in valid}
    if not reference and scope != dream.SCOPE_GLOBAL and scope not in scope_of.values():
        raise ValueError(f"--repo {scope!r} matches no concept's scope — scopes present: "
                         f"{_scopes_present(scope_of)}")
    out: list[dict] = []
    for c in valid:
        if reference:
            if dream.clean_kind(c.get("kind")) != dream.KIND_REFERENCE:
                continue                         # the sheet renders reference only — any scope
        elif scope_of[c["id"]] != scope or dream.clean_kind(c.get("kind")) not in kinds:
            continue                             # the region doesn't render it → nothing to be faithful to
        facets = by_node.get(c["id"], {}).get("facets", {})
        out.append({
            "id": c["id"],
            "title": c.get("title", ""),
            "statement": c.get("statement", ""),
            "kind": c["kind"],
            "scope": scope_of[c["id"]],
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


def _proposal(root: Path, target: Path | str | None, kinds: tuple[str, ...], scope: str,
              reference: bool) -> tuple[str, Path]:
    """Pick WHICH projection (rules region vs reference sheet) and WHICH default target apply/diff
    work on — one switch, so the pair can never route to different files. A `reference=True` call
    carrying a non-default kinds/scope is REFUSED: the sheet's filter IS "reference, every scope",
    and silently ignoring the caller's filters would be the hidden reinterpretation ADR-0027
    forbids (the CLI refuses the flag combination earlier, with the operator-facing message)."""
    if reference:
        if tuple(kinds) != DEFAULT_KINDS or scope != dream.SCOPE_GLOBAL:
            raise ValueError("the reference sheet is its own projection (kind==reference, every "
                             "scope) — kinds/scope don't apply to it")
        return project_reference(root), Path(target) if target is not None else default_reference_target(root)
    return (project(root, kinds=kinds, scope=scope),
            Path(target) if target is not None else default_target(root))


def apply(root: Path | None = None, *, target: Path | str | None = None,
          kinds: tuple[str, ...] = DEFAULT_KINDS, scope: str = dream.SCOPE_GLOBAL,
          reference: bool = False) -> dict:
    """Write the projection into `target`'s marked region (refresh-in-place). Reads the current file (or ""
    when absent), splices in `project(root)` — or `project_reference(root)` into the staged reference.md
    when `reference` — writes only when the bytes CHANGE (so re-apply with unchanged
    concepts touches nothing). The staged default target is created on demand; a real CLAUDE.md is written
    only because the human pointed `--target` at it. Returns {target, action, changed}."""
    root = root or config.data_root()
    region, tgt = _proposal(root, target, kinds, scope, reference)
    old = tgt.read_text() if tgt.exists() else ""
    new, action = _splice(old, region)
    tgt.parent.mkdir(parents=True, exist_ok=True)
    changed = new != old
    if changed:
        tgt.write_text(new)
    return {"target": str(tgt), "action": action, "changed": changed}


def diff(root: Path | None = None, *, target: Path | str | None = None,
         kinds: tuple[str, ...] = DEFAULT_KINDS, scope: str = dream.SCOPE_GLOBAL,
         reference: bool = False) -> str:
    """A unified diff of the PROPOSED region (rules, or the reference sheet when `reference`) vs the
    target's CURRENT one — what `--apply` would change, for
    the human to review BEFORE applying (the second review tier). Empty string when they already match."""
    root = root or config.data_root()
    proposed, tgt = _proposal(root, target, kinds, scope, reference)
    old = tgt.read_text() if tgt.exists() else ""
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
    ap.add_argument("--kinds", default=None, metavar="K[,K]",
                    help=f"which concept kinds to project (default: {','.join(DEFAULT_KINDS)}). "
                         f"reference concepts are EXCLUDED by default because the rules budget is "
                         f"behavioral surface — rules that shape conduct — while a reference fact is "
                         f"lookup material; it stays valid and queryable (review --concepts), and "
                         f"`--reference` projects it into its own lookup sheet. `--kinds "
                         f"{','.join(dream.CONCEPT_KINDS)}` widens (ADR-0029)")
    ap.add_argument("--repo", metavar="REPO",
                    help=f"project ONLY concepts scoped to this repo (default: {dream.SCOPE_GLOBAL} "
                         f"— the global CLAUDE.md gets only what applies everywhere). A repo-scoped "
                         f"concept belongs in that repo's own CLAUDE.md: pair `--repo X` with "
                         f"`--target ~/X/CLAUDE.md`. A repo no concept is scoped to is refused, "
                         f"with the scopes present (ADR-0030)")
    ap.add_argument("--reference", action="store_true",
                    help=f"project the REFERENCE SHEET instead of rules: exactly the kind==reference "
                         f"concepts, EVERY scope, grouped by scope (global first) — the lookup "
                         f"surface the kind filter's exclusion left them without (ADR-0029). Default "
                         f"--target: the staged {GENERATED_SUBDIR}/{REFERENCE_FILENAME} beside the "
                         f"rules one. Mutually exclusive with --kinds/--repo (the sheet's filter IS "
                         f"'reference, every scope')")
    args = ap.parse_args(argv)
    root = config.data_root()
    target = args.target            # None → the staged default, resolved inside apply/diff

    try:                                # a corrupted/ambiguous target region, a bad --kinds, an
        if args.reference and (args.kinds is not None or args.repo is not None):
            raise ValueError("--reference is its own projection — exactly the kind==reference "
                             "concepts, every scope, grouped by scope — so --kinds/--repo would "
                             "contradict it; use them on the rules projection (drop --reference)")
        kinds = (tuple(s.strip() for s in args.kinds.split(",") if s.strip())
                 if args.kinds is not None else DEFAULT_KINDS)
        scope = args.repo if args.repo is not None else dream.SCOPE_GLOBAL
        ref = args.reference
        if args.apply:                  # unmatched --repo, or a --reference flag combo raises —
            res = apply(root, target=target, kinds=kinds, scope=scope, reference=ref)   # surface it
            print(f"{res['action']} ratchet region in {res['target']}"    # cleanly, not as a
                  + ("" if res["changed"] else " (no change — byte-identical)"))   # traceback; the
        elif args.concepts:                                     # file is never written on the raise path
            print(json.dumps(projected_concepts(root, kinds=kinds, scope=scope, reference=ref),
                             ensure_ascii=False, indent=2))
        elif args.diff:
            d = diff(root, target=target, kinds=kinds, scope=scope, reference=ref)
            tgt = target if target is not None else (
                default_reference_target(root) if ref else default_target(root))
            print(d if d.strip() else f"no changes — {tgt}'s region already matches the projection")
        else:
            print(project_reference(root) if ref else project(root, kinds=kinds, scope=scope))
    except ValueError as e:
        raise SystemExit(f"generate: {e}")


if __name__ == "__main__":
    main()
