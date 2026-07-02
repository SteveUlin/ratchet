"""subject — WHERE a lesson lives: the repo+files scope key of one event (dream-v3 §2.1).

An event's subject_key answers "which code is this lesson about?" with the only two facets that
discriminate projects: the REPO (the origin cwd's basename, via `concepts._repo_label`) and the
FILES WRITTEN near the lesson's evidence. `tools` is deliberately absent from the key: in a Claude
Code transcript, tool names (Edit/Bash/Read/Grep) are near-identical across every project — they
name the instrument, never the subject — so tool overlap relates everything to everything (the
v2-design's `tools={zig}` example mistook Bash *arguments* for tool names).

Narrowing (§3.0): `concepts.session_facts` unions every file written in the WHOLE session, so a
busy 15-file refactor stamps all 15 onto every event and two unrelated lessons from one session
get an identical subject. Instead, keep only the files whose write-tool line — the form weave
renders (`→ Edit: /path`) — lands within ±SPAN_WINDOW bytes of the event's evidence span: the
lesson's OWN co-located writes. When no write is co-located (the lesson is discussed far from
where the code changed), fall back to the whole-session union — recall-safe, never silently
narrower than the session. A no-edit, no-cwd session yields the EMPTY key; `is_empty` names the
resolve cascade's seed-only gate for it (§3.1: empty∩empty is not evidence of "same subject").

Everything recomputes on read from immutable blobs (ADR-0013): repo from the cleaned blob's
lineage to the raw transcript's origin_ref, writes from the cleaned text itself, the fallback
union from a raw re-parse. Nothing is stored here; glean stamps the result onto the event blob.
`concepts` is imported LAZILY (function-local, the glean idiom): glean grows an import of this
module, and concepts already reaches glean through dream — a top-level import would close the
cycle.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import blobstore
from .weave import active_path, parse

# The narrowing window: bytes either side of the event's evidence span within which a write-tool
# line counts as CO-LOCATED (§3.0). The principle: a lesson's subject is its OWN nearby writes,
# not everything the session touched — the window is the "same working beat" radius. ±4000 bytes
# is one-to-two rendered turns of cleaned text (weave caps a tool result at ~1.3k, a text block at
# ~5k), so an edit made while the lesson was being learned is in; the unrelated refactor three
# episodes earlier is out. UNTUNED starting value — widen if real events' co-located edits render
# further from their quotes, shrink if narrowing still smears (gold set, like the concepts weights).
SPAN_WINDOW = 4000

_ARROW = "→ ".encode("utf-8")   # weave's tool-use line prefix, always at column 0 of its line


# --- the write-line scan: parse weave's rendered tool-use lines back into written paths -----------

def _path_of(name: str, arg: str) -> str | None:
    """The written path inside one rendered write-tool line. Edit/Write/MultiEdit carry their
    `file_path` as weave's scalar arg (`→ Edit: /path`); NotebookEdit's `notebook_path` is not in
    weave's scalar-arg keys, so its line carries a JSON dump of the input — parse it and take
    `notebook_path`. Anything unparseable yields None (the fallback union still covers the file)."""
    if name == "NotebookEdit":
        try:
            inp = json.loads(arg)
        except json.JSONDecodeError:
            return None
        p = inp.get("notebook_path") if isinstance(inp, dict) else None
        return p.strip() if isinstance(p, str) and p.strip() else None
    return arg or None


def _write_lines(data: bytes) -> list[tuple[int, int, str]]:
    """Every write-tool line in the cleaned blob as `(byte_start, byte_end, path)`, in one pass.
    Offsets are BYTE offsets into the blob — the same coordinate system as evidence spans
    (`get(cleaned_hash)[start:end]`), so window intersection is plain integer math. Which tools
    count as writes is `concepts.EDIT_TOOLS` (Edit/Write/MultiEdit/NotebookEdit — a Read views).
    A line weave truncated (`…[` elision marker) carries a mangled path and is dropped — the
    fallback covers it rather than a garbage path entering the key."""
    from . import concepts                         # lazy: the concepts→dream→glean→subject cycle
    out: list[tuple[int, int, str]] = []
    pos = 0
    for raw_line in data.split(b"\n"):
        end = pos + len(raw_line)
        if raw_line.startswith(_ARROW):
            name, sep, arg = raw_line.decode("utf-8")[2:].partition(": ")
            if sep and name in concepts.EDIT_TOOLS and "…[" not in arg:
                path = _path_of(name, arg.strip())
                if path:
                    out.append((pos, end, path))
        pos = end + 1
    return out


# --- one cleaned blob's subject material, parsed once (the `cache` unit) --------------------------

def _blob_entry(cleaned_hash: str, root: Path) -> dict:
    """Everything subject_key needs from ONE cleaned blob, parsed once: the repo (a lineage meta
    hop to the raw's origin_ref, named via `concepts._repo_label`), the write-line index (a content
    scan), the blob's byte length (span validation on a cache hit, without re-reading), and the raw
    hash the fallback union re-parses. `union` starts None and fills lazily — a session whose every
    event narrows never pays the raw re-parse. Never fatal: a reclaimed blob or broken lineage
    degrades to an empty entry, so the event carries an empty subject (seed-only, §3.1)."""
    from . import concepts                         # lazy: see module docstring
    repo, raw = None, None
    try:
        raw = blobstore.get_meta(cleaned_hash, root).get("derived_from")
        if raw:
            origin = blobstore.get_meta(raw, root).get("origin_ref") or {}
            repo = concepts._repo_label(origin)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        raw = None
    writes: list[tuple[int, int, str]] = []
    nbytes = 0
    try:
        data = blobstore.get(cleaned_hash, root).encode("utf-8")
        writes, nbytes = _write_lines(data), len(data)
    except (FileNotFoundError, OSError):
        pass
    return {"repo": repo, "raw": raw, "writes": writes, "nbytes": nbytes, "union": None}


def _session_union(entry: dict, root: Path) -> list[str]:
    """The whole-session files union — `concepts.session_facts` over the raw re-parse, the §3.0
    FALLBACK. Computed once per blob (memoized on the entry) and only when some event needs it."""
    if entry["union"] is None:
        from . import concepts                     # lazy: see module docstring
        files: list[str] = []
        if entry["raw"]:
            try:
                files = concepts.session_facts(
                    active_path(parse(blobstore.get(entry["raw"], root))))["files_edited"]
            except (FileNotFoundError, OSError):
                files = []
        entry["union"] = files
    return entry["union"]


def _valid_span(span, nbytes: int) -> tuple[int, int] | None:
    """`blobstore.validate_span`'s read-side check against a stored LENGTH — the cache keeps only
    the parse, never the blob bytes, so the in-bounds test runs on `nbytes`. Same discipline:
    plain ints only (bool rejected), 0 <= start < end <= len; anything else is None."""
    if not (isinstance(span, (tuple, list)) and len(span) == 2):
        return None
    s, e = span
    if isinstance(s, bool) or isinstance(e, bool):
        return None
    if not (isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= nbytes):
        return None
    return s, e


# --- the public surface ----------------------------------------------------------------------------

def subject_key(root: Path, cleaned_hash: str, span, cache: dict | None = None) -> dict:
    """The subject scope of one event — `{"repo": str | None, "files": [str, ...]}` (§2.1).

    `span` is the event's evidence span: a `(byte_start, byte_end)` pair into the cleaned blob's
    UTF-8 bytes, exactly as glean stores it (`evidence[0]["byte_start"/"byte_end"]`) and dream/
    review re-validate it. `files` keeps the writes CO-LOCATED with that span — write-tool lines
    within ±SPAN_WINDOW bytes — narrowing the subject to the lesson's own edits (§3.0). FALLBACK:
    when no write lands in the window (or the span doesn't validate against this blob), return the
    whole-session union via `concepts.session_facts` — files only, never tools. So the key is at
    worst session-wide, never wrongly empty; it is EMPTY only for a genuinely no-edit, no-cwd
    session (see `is_empty`). `files` is sorted (stable JSON bytes).

    `cache` (optional, keyed by cleaned_hash) shares the per-blob parse across a batch: one
    session's events scan the cleaned text — and re-parse the raw for the union — once."""
    entry = cache.get(cleaned_hash) if cache is not None else None
    if entry is None:
        entry = _blob_entry(cleaned_hash, root)
        if cache is not None:
            cache[cleaned_hash] = entry
    files: list[str] | None = None
    sp = _valid_span(span, entry["nbytes"])
    if sp is not None:
        lo, hi = sp[0] - SPAN_WINDOW, sp[1] + SPAN_WINDOW
        near = sorted({p for (ls, le, p) in entry["writes"] if ls < hi and le > lo})
        if near:
            files = near
    if files is None:
        files = list(_session_union(entry, root))
    return {"repo": entry["repo"], "files": files}


def subject_overlap(a: dict, b: dict) -> float:
    """The soft scope signal between two subject keys — a weighted facet overlap, reusing the
    concepts weights (§3.3): `W_FILE` per shared file (the strongest co-location evidence, scaling
    with corroboration) + `W_REPO` once for a shared repo. The `tools` weight is fixed at 0 — CC
    tool names are near-identical across all projects, so they don't discriminate subjects; a
    `tools` field on a key is simply never read. Two empty keys share nothing → 0.0 (empty∩empty
    is not overlap, §3.1)."""
    from . import concepts                         # lazy: see module docstring
    score = concepts.W_FILE * len(set(a.get("files") or ()) & set(b.get("files") or ()))
    if a.get("repo") and a.get("repo") == b.get("repo"):
        score += concepts.W_REPO
    return score


def is_empty(key: dict) -> bool:
    """True iff the key scopes NOTHING — no repo, no files (a discussion/no-edit session with no
    cwd). Named so the resolve cascade's gate reads as policy: an empty-subject event is SEED-only
    via subject — it corroborates only through a high statement match at the cross-cutting bar,
    never through empty∩empty "overlap" (§3.1)."""
    return not key.get("repo") and not key.get("files")
