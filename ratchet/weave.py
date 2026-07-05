"""weave — reconstruct a transcript blob's active conversation and render it into one clean,
speaker-tagged document: the *cleaned* blob. Deterministic, no LLM (ADR-0003).

A raw transcript blob is a TREE, not a transcript: each `.jsonl` line is one content *block*
(an assistant turn is a chain of block-records sharing `message.id`); rewinds/retries fork
sibling branches; a compact severs the `parentUuid` chain (bridged by `logicalParentUuid`);
subagent threads are `isSidechain`. weave turns that into one linear document:

1. `active_path` — walk `parentUuid` back from the last leaf (file order disambiguates
   branches: the surviving tip was appended last), hopping `logicalParentUuid` (and, when that
   target is absent, file order) across compacts so pre-compact history — frozen in the blob,
   the highest-value extraction material — is kept. Sidechains drop (separate conversation).
2. `render` — fold each `tool_result` next to its `tool_use` BY `tool_use_id` (a global index,
   not tree position — a linear walk silently drops parallel-call results). Drop noise (system
   bookkeeping, `isMeta` caveats, empty thinking); truncate big tool output.

The whole session renders to ONE cleaned blob (a compact is context management, not a task
change — ADR-0003). `materialize` freezes it as a content-addressed derived blob; chunking is a
separate block (`ratchet.chunk`) that windows the cleaned blob on demand. Trust anchor: weave is
deterministic, so the cleaned blob is reproducible from an immutable hash and a downstream quote
verifies as `get(cleaned_hash)[span]`; lineage is all content-addressed hops:
event -> span in cleaned blob -> derived_from -> raw blob -> datastore.

DOCUMENT MODE (ADR-0031): a `document` raw blob (tap `--file` — the owner's hand-written
CLAUDE.md, later fetched PDFs/pages) has no conversation tree to reconstruct; `render_document`
is a line-preserving passthrough MINUS any `ratchet:generated` region — the SELF-LOOP GUARD (see
`strip_generated`). Mode is picked by the raw meta's `source_kind`; everything downstream (chunk
windows, glean spans) is unchanged because both modes end in the same RenderedDoc shape.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from . import blobstore, block, config

RENDER_VERSION = "weave/1"             # bump when render logic changes — pins which logic made a span
                                       # (ONE version for both modes: a doc-render change re-keys
                                       # transcript renders too, but weave is $0 and content-addressed,
                                       # so the re-render no-ops — not worth a second knob)
RENDER_FORMAT = "transcript.render/1"  # the cleaned-blob artifact's shape (source-kind . shape . ver)
DOC_RENDER_FORMAT = "document.render/1"   # the document passthrough's shape (same naming scheme)
RENDER_FORMATS = (RENDER_FORMAT, DOC_RENDER_FORMAT)   # every cleaned-doc shape chunk enumerates over
OUT_NOUN = "cleaned"                    # the per-item output noun the Progress bar/line shows


def render_format(source_kind: str) -> str:
    """The cleaned-blob format for a source kind. The format string self-describes WHICH render made
    the artifact (`source-kind . shape . ver`), so a sidecar-only consumer can tell a transcript
    render from a document render without reading content."""
    return DOC_RENDER_FORMAT if source_kind == "document" else RENDER_FORMAT


# THE SELF-LOOP GUARD (ADR-0031, non-negotiable): `generate --apply` writes a marked
# `<!-- ratchet:generated START … -->` … `<!-- ratchet:generated END -->` region into a target
# CLAUDE.md. Re-ingesting that region would let ratchet corroborate its own projections — the
# concept layer laundering its output back in as fresh document evidence (ADR-0025's tap-self-skip,
# one layer up). The guard is STRUCTURAL because it lives at RENDER time: every downstream span
# points into the CLEANED blob, and the cleaned blob simply does not contain the region, so no
# event can ever cite generated text. The markers couple to `generate.START`/`generate.END` by
# CONVENTION, not import — generate→dream→glean→weave would cycle; tests/test_document.py pins the
# exact marker text so drift trips a test, not the guard.
GENERATED_START_PREFIX = "<!-- ratchet:generated START"
GENERATED_END = "<!-- ratchet:generated END -->"


def strip_generated(text: str) -> str:
    """Remove every ratchet:generated region — a START-prefixed marker through the END marker,
    inclusive. Defensive where `generate._region_span` is strict, and deliberately so: generate
    REFUSES an ambiguous region (it must never clobber human content), but the guard here strips
    ALL regions, and an UNTERMINATED start strips to end-of-file. When in doubt, exclude —
    over-stripping costs a little recall; under-stripping poisons the loop."""
    out = text
    while True:
        s = out.find(GENERATED_START_PREFIX)
        if s == -1:
            return out
        e = out.find(GENERATED_END, s)
        out = out[:s] + (out[e + len(GENERATED_END):] if e != -1 else "")


# --- 1. parse + active-path reconstruction ------------------------------------------------

def parse(blob_text: str) -> list[dict]:
    """Decode the raw `.jsonl` into records, skipping blank/corrupt lines."""
    out = []
    for line in blob_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def active_path(records: list[dict]) -> list[dict]:
    """The surviving conversation thread, oldest→newest. Index non-sidechain records by uuid;
    take the tip = last file-order user/assistant record; walk back to a root, hopping each
    severed link in three tiers:

    1. `parentUuid` — the normal turn-to-turn edge.
    2. `logicalParentUuid` — the compact bridge (a compact nulls `parentUuid`).
    3. file-order fallback — to the highest-ordered earlier user/assistant record. A compact's
       `logicalParentUuid` sometimes points at a record never persisted to the blob (or one in a
       parent session); the pre-compact history still sits in the blob in append order, so file
       order is the bridge. Walking `parentUuid` from that tail still drops abandoned branches.

    Abandoned rewind branches and sidechains fall away naturally."""
    by_uuid = {r["uuid"]: r for r in records if r.get("uuid") and not r.get("isSidechain")}
    order = {r["uuid"]: i for i, r in enumerate(records) if r.get("uuid")}
    ua = sorted((u for u, r in by_uuid.items() if r.get("type") in ("user", "assistant")),
                key=lambda u: order[u])
    tip = None
    for r in reversed(records):
        if r.get("uuid") in by_uuid and r.get("type") in ("user", "assistant"):
            tip = r["uuid"]
            break
    if tip is None:
        return []
    spine, seen = [], set()

    def bridge(o: int) -> str | None:
        """Highest-ordered earlier user/assistant record not yet visited (strictly decreasing o
        ⇒ termination; one session's earlier records are all legitimately its history)."""
        cand = None
        for u in ua:
            if order[u] >= o:
                break
            if u not in seen:
                cand = u
        return cand

    cur = tip
    while cur and cur in by_uuid and cur not in seen:
        seen.add(cur)
        r = by_uuid[cur]
        spine.append(r)
        pu = r.get("parentUuid")
        lp = r.get("logicalParentUuid")
        if pu in by_uuid and pu not in seen:
            cur = pu
        elif lp in by_uuid and lp not in seen:
            cur = lp
        else:
            cur = bridge(order[r["uuid"]])
    spine.reverse()
    return spine


# --- 2. render ----------------------------------------------------------------------------

@dataclass
class Turn:
    kind: str          # "user" | "assistant" | "compact"
    start: int         # [start, end) char offset into the cleaned doc — the turn's text is the slice
    end: int
    segment: int       # compact-segment index (0, then +1 past each compact / resume severance)
    index: int         # position in doc.turns


@dataclass
class RenderedDoc:
    text: str
    turns: list[Turn]
    source_kind: str
    render_version: str
    cleaned_hash: str  # blob_hash(text) — the cleaned blob's content-addressed identity


def _drop_surrogates(s: str) -> str:
    """Replace unpaired surrogates (1:1, so char offsets are preserved). Claude Code truncates
    tool output by UTF-16 unit and can split a surrogate pair, yielding a lone surrogate that
    survives `json.loads` but is not UTF-8 encodable — it would otherwise crash the content hash."""
    return s.encode("utf-8", "replace").decode("utf-8") if s else s


# Render truncation caps — how much of each block survives into the cleaned doc. A head/tail pair keeps
# the START (the intent) and the END (the outcome/conclusion) and elides the middle with a counted marker.
# subject.py's SPAN_WINDOW is sized off these caps — retune them together.
THINKING_HEAD, THINKING_TAIL = 2000, 400   # thinking is context, not ground truth — keep its shape, shed bulk
TEXT_HEAD, TEXT_TAIL = 4000, 1000          # the lesson-bearing prose — the render's largest caps; assistant
                                           # text and user turns share them (both are the conversation itself)
RESULT_HEAD, RESULT_TAIL = 1000, 300       # tool results are bulky + mechanical: head = what came back,
                                           # tail = the exit/summary lines
TOOL_LINE_CAP = 280                        # one single-line `→ name: arg` per tool call — a pointer, not a dump
TRUNCATE_SLACK = 40                        # skip eliding when the marker costs about what it saves
INSPECT_HEAD = 70                          # the --inspect debug listing's per-turn preview width


def _truncate(s: str, head: int, tail: int = 0) -> str:
    if len(s) <= head + tail + TRUNCATE_SLACK:
        return s
    elided = len(s) - head - tail
    if tail:
        return f"{s[:head]} …[{elided} chars elided]… {s[-tail:]}"
    return f"{s[:head]} …[{elided} chars elided]…"


def _tool_use_line(b: dict) -> str:
    name = b.get("name", "tool")
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    arg = ""
    for k in ("command", "file_path", "path", "pattern", "url", "query", "subagent_type",
              "description"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            arg = v.strip()
            break
    if not arg and inp:
        arg = json.dumps({k: inp[k] for k in list(inp)[:4]}, ensure_ascii=False, default=str)
    line = f"→ {name}" + (f": {arg}" if arg else "")
    return _truncate(line.replace("\n", " "), TOOL_LINE_CAP)


def _tool_result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        s = c
    elif isinstance(c, list):
        s = "\n".join((b.get("text") or "") for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
    else:
        s = "" if c is None else str(c)
    return ("[error] " + s) if block.get("is_error") else s


def _indent_result(res: str) -> str:
    res = _truncate(res.strip(), RESULT_HEAD, RESULT_TAIL)
    return "  ⤷ " + res.replace("\n", "\n     ")


def _index_tool_results(records: list[dict]) -> dict[str, str]:
    """tool_use_id -> rendered result, over the whole file (minus sidechains). Folding by id (not
    tree position) recovers parallel-call results a linear walk would drop. Ids are globally
    unique; on the rare reused id (a rewound attempt), the later (survivor) result wins."""
    idx: dict[str, str] = {}
    for r in records:
        if r.get("isSidechain"):
            continue
        m = r.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        for b in c:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                idx[b["tool_use_id"]] = _tool_result_text(b)  # last (survivor) wins
    return idx


def _mid(r: dict) -> str:
    m = r.get("message")
    return (m.get("id") if isinstance(m, dict) else None) or r.get("uuid")


def _is_tool_result_only(c) -> bool:
    return (isinstance(c, list) and len(c) > 0
            and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in c))


def _render_assistant(blocks: list, tool_results: dict[str, str]) -> str:
    lines = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "thinking":
            tk = (b.get("thinking") or "").strip()
            if tk:
                lines.append("(thinking) " + _truncate(tk, THINKING_HEAD, THINKING_TAIL))
        elif bt == "text":
            tx = (b.get("text") or "").strip()
            if tx:
                lines.append(_truncate(tx, TEXT_HEAD, TEXT_TAIL))
        elif bt == "tool_use":
            lines.append(_tool_use_line(b))
            res = tool_results.get(b.get("id"))
            if res:
                lines.append(_indent_result(res))
    return "\n".join(lines).strip()


def _render_user(c) -> str:
    if isinstance(c, str):
        s = c
    elif isinstance(c, list):
        s = "\n".join((b.get("text") or "") for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
    else:
        s = ""
    return _truncate(s.strip(), TEXT_HEAD, TEXT_TAIL)


def _spine_turns(spine: list[dict], tool_results: dict[str, str]) -> list[tuple[str, str, int]]:
    """(kind, body, segment) per turn. Consecutive same-`message.id` assistant block-records are
    one turn; a tool_result-only user record is dropped (folded); a compact boundary opens a new
    segment (a within-doc chunking hint, not a doc split — ADR-0003)."""
    out: list[tuple[str, str, int]] = []
    seg, i = 0, 0
    while i < len(spine):
        r = spine[i]
        t = r.get("type")
        if t == "assistant":
            mid = _mid(r)
            blocks: list = []
            while i < len(spine) and spine[i].get("type") == "assistant" and _mid(spine[i]) == mid:
                c = spine[i].get("message", {}).get("content")
                if isinstance(c, list):
                    blocks.extend(c)
                elif isinstance(c, str):
                    blocks.append({"type": "text", "text": c})
                i += 1
            body = _render_assistant(blocks, tool_results)
            if body:
                out.append(("assistant", "[assistant]\n" + body, seg))
        elif t == "user":
            c = r.get("message", {}).get("content")
            if not r.get("isMeta") and not _is_tool_result_only(c):
                body = _render_user(c)
                if body:
                    out.append(("user", "[user]\n" + body, seg))
            i += 1
        elif t == "system" and r.get("subtype") == "compact_boundary":
            seg += 1
            cm = r.get("compactMetadata") or {}
            out.append(("compact",
                        f"[compact] context compacted "
                        f"({cm.get('trigger', '?')}, {cm.get('preTokens', '?')} pre-tokens)",
                        seg))
            i += 1
        else:
            i += 1
    return out


def render_document(blob_text: str, *, title: str | None = None) -> RenderedDoc:
    """DOCUMENT MODE (ADR-0031): a line-preserving passthrough — the cleaned text is the file's own
    bytes MINUS any ratchet:generated region (the self-loop guard), behind one `[document] <path>`
    header turn (the speaker-tag analogue: a chunk stays self-describing about what it excerpts).

    There is no speaker structure to reconstruct, so the turn grain is the text's own blank-line
    paragraphs: splitting the body on the exact `"\\n\\n"` separator the transcript render joins
    with makes split+join the IDENTITY — turns tile the cleaned blob byte-for-byte (the same
    invariant chunk relies on), chunk windows pack whole paragraphs (a rule/bullet never splits
    mid-thought under budget), and glean's numbered lines line up with the file's own lines. One
    segment (documents don't compact)."""
    body = strip_generated(blob_text)
    parts = [f"[document] {title}" if title else "[document]"] + body.split("\n\n")
    texts, turns, pos = [], [], 0
    for p in parts:
        p = _drop_surrogates(p)                       # 1:1, offset-preserving (verbatim ingest makes
        texts.append(p)                               # a lone surrogate near-impossible; belt+braces)
        turns.append(Turn(kind="document", start=pos, end=pos + len(p), segment=0, index=len(turns)))
        pos += len(p) + 2
    text = "\n\n".join(texts)
    return RenderedDoc(text=text, turns=turns, source_kind="document",
                       render_version=RENDER_VERSION, cleaned_hash=blobstore.blob_hash(text))


def render(blob_text: str, *, source_kind: str = "transcript",
           origin: dict | None = None) -> RenderedDoc:
    """Active path → one linear, speaker-tagged cleaned document with per-turn char spans.

    Mode dispatches on `source_kind` (the raw meta's): a `document` takes the line-preserving
    passthrough (`render_document`; `origin` — the raw's origin_ref — supplies the header's path),
    everything else the transcript reconstruction below."""
    if source_kind == "document":
        o = origin or {}
        return render_document(blob_text, title=o.get("path") or o.get("session_id"))
    records = parse(blob_text)
    spine = active_path(records)
    tool_results = _index_tool_results(records)
    parts, turns, pos = [], [], 0
    for kind, body, seg in _spine_turns(spine, tool_results):
        body = _drop_surrogates(body)
        start = pos
        parts.append(body)
        pos += len(body)
        turns.append(Turn(kind=kind, start=start, end=pos, segment=seg, index=len(turns)))
        parts.append("\n\n")
        pos += 2
    text = "".join(parts)
    if text.endswith("\n\n"):
        text = text[:-2]                              # trailing separator; no turn span includes it
    return RenderedDoc(text=text, turns=turns, source_kind=source_kind,
                       render_version=RENDER_VERSION, cleaned_hash=blobstore.blob_hash(text))


# --- 3. blobstore-backed entry points -----------------------------------------------------

def tags_from_meta(m: dict) -> dict:
    """Passthrough filters a consumer can use without re-reading the blob."""
    o = m.get("origin_ref") or {}
    return {k: o.get(k) for k in ("project", "session_id", "cwd", "git_branch") if o.get(k)}


def render_blob(raw_hash: str, root: Path | None = None) -> RenderedDoc:
    m = blobstore.get_meta(raw_hash, root)
    return render(blobstore.get(raw_hash, root), source_kind=m.get("source_kind", "transcript"),
                  origin=m.get("origin_ref"))


def materialize(raw_hash: str, *, expires_at: str | None = None,
                root: Path | None = None) -> tuple[str, bool, RenderedDoc]:
    """Freeze the cleaned doc as a content-addressed DERIVED blob (the span-anchoring artifact).
    Idempotent — re-rendering reproduces the hash, so a TTL-reclaimed cleaned blob is
    reconstructible. Returns (cleaned_hash, written, doc) — `doc` lets `chunk` reuse the render
    instead of weave's provenance being reissued downstream."""
    root = root or config.data_root()
    m = blobstore.get_meta(raw_hash, root)
    doc = render(blobstore.get(raw_hash, root), source_kind=m.get("source_kind", "transcript"),
                 origin=m.get("origin_ref"))
    h, written = blobstore.put_derived(doc.text, source_kind=doc.source_kind, derived_from=raw_hash,
                                       produced_by="weave", render_version=doc.render_version,
                                       fmt=render_format(doc.source_kind), tags=tags_from_meta(m),
                                       expires_at=expires_at, h=doc.cleaned_hash, root=root)
    return h, written, doc


# --- the Block: weave as a uniform stage (item = a raw blob → cleaned blob; ADR-0009) ------

class WeaveBlock:
    """weave as a `block.Block` — the batch/idempotent surface over `materialize`. The item is a raw
    transcript blob; `process` is `materialize` (deterministic, no LLM, cost 0). Idempotency keys on
    (raw_hash, render_version): the cleaned blob is a pure function of those two, so bumping
    RENDER_VERSION re-renders every raw blob (a new cleaned blob + a new marker) — the "re-render on
    logic change" weave lacked when it relied purely on content-addressing.

    `materialize` stays the public single-source workhorse (CLI/chunk reuse it); this just wraps it in
    the shared driver for `--all` + streaming progress + the done-skip."""

    name = "weave"
    commits_per_item = True
    finalize = block.no_finalize             # no cross-item dependency
    marker_extra = block.no_marker_extra     # no per-item audit fields
    priority = block.no_priority             # arrival order
    age = block.no_age                       # cheap deterministic stage — aging is moot (ADR-0021)

    def __init__(self) -> None:
        # render_version is read at construction so a single instance pins one logic version per run.
        self.params: tuple[tuple[str, str], ...] = (("render_version", RENDER_VERSION),)

    def items(self, root: Path, *, source_id: str | None = None):
        """Enumerate raw blobs to render. --all → every transcript AND document source's LATEST raw
        version (the two rendered source kinds; a superseded snapshot need not be re-rendered: it is
        content-addressed and its cleaned blob already exists if ever rendered). --source-id → just
        that source's latest (a session id, or a document's path)."""
        if source_id is not None:
            h = blobstore.latest_version(source_id, root)
            if h is not None:
                yield h
            return
        for kind in ("transcript", "document"):
            yield from blobstore.latest_by_kind(kind, root).values()

    def key(self, raw_hash: str) -> str:
        """The raw blob hash — content-addressed and stable; the cleaned blob is a deterministic
        function of (raw_hash, render_version), so the marker keys on it."""
        return raw_hash

    def process(self, raw_hash: str, *, root: Path, run_id: str) -> tuple[int, float]:
        """Materialize the cleaned blob (idempotent content-addressed put_derived, itself crash-safe
        content-then-meta). 1 output if newly written, 0 if it already existed; cost always 0."""
        _, written, _ = materialize(raw_hash, root=root)
        return (1 if written else 0, 0.0)


# --- CLI: inspect ONE blob (turn summary / full render), or batch-materialize via the block -

def _inspect(h: str) -> None:
    """Read-only single-blob view: the default turn summary (or --render's full doc)."""
    doc = render_blob(h)
    segs = sorted({t.segment for t in doc.turns})
    print(f"raw {h[:12]}  cleaned {doc.cleaned_hash[:12]}  {len(doc.turns)} turns  "
          f"{len(doc.text)} chars  segments {segs}")
    for t in doc.turns[:40]:
        head = doc.text[t.start:t.end].splitlines()[0] if t.end > t.start else ""
        print(f"  [{t.index:>3}] seg{t.segment} {t.end - t.start:>6}c {_truncate(head, INSPECT_HEAD)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="weave",
                                 description="Render transcript blobs into cleaned documents.")
    ap.add_argument("hash", nargs="?", help="raw blob hash (else --source-id / --all)")
    ap.add_argument("--source-id", help="this logical source's latest blob")
    ap.add_argument("--all", action="store_true",
                    help="materialize every transcript's and document's latest raw")
    ap.add_argument("--render", action="store_true", help="print the full cleaned document (one blob)")
    ap.add_argument("--inspect", action="store_true",
                    help="print the turn summary of one blob (no materialize)")
    # uniform block knobs (per ADR-0009)
    ap.add_argument("--limit", type=int, help="cap items EXAMINED")
    ap.add_argument("--dry-run", action="store_true", help="list what would materialize; do nothing")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--max-usd", type=float, help="(no cost; inert — weave never calls an LLM)")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy (inert — weave emits no per-item signal, so every policy is arrival order)")
    args = ap.parse_args(argv)

    # READ-ONLY inspectors of ONE blob stay a separate path: --render (full doc) / --inspect (turn
    # summary). Without them, a bare `weave <hash>` / `weave --source-id <id>` / `weave --all`
    # MATERIALIZES (matching chunk/glean/dream where the bare invocation does the work).
    if args.render or args.inspect:
        h = args.hash or (blobstore.latest_version(args.source_id) if args.source_id else None)
        if not h:
            ap.error("give a blob hash or --source-id to inspect")
        if not blobstore.has(h):
            ap.error(f"no such blob: {h}")
        if args.render:
            print(render_blob(h).text)
        else:
            _inspect(h)
        return

    if not (args.all or args.source_id or args.hash):
        ap.error("give a blob hash, --source-id, or --all")

    # the stage owns its Progress now (the driver only speaks the protocol). None for --quiet/--dry-run;
    # else built from this stage's args + OUT_NOUN. weave has no LLM cost, so cap is omitted.
    def make_progress(blk):
        if args.quiet or args.dry_run:
            return None
        return block.Progress(blk.name, params=dict(blk.params), out_noun=OUT_NOUN, verbose=args.verbose)

    # The batch/idempotent path: drive the block. A bare hash scopes to that one raw blob (a one-item
    # block so the driver's done-skip + marker still apply — no bespoke materialize that bypasses them).
    prio = block.priority_strategy(args.priority)
    if args.hash and not args.all:
        if not blobstore.has(args.hash):
            ap.error(f"no such blob: {args.hash}")
        one = _OneRaw(args.hash)
        block.run(one, dry_run=args.dry_run, priority=prio, progress=make_progress(one))
        return
    wb = WeaveBlock()
    block.run(wb, source_id=args.source_id, max_usd=args.max_usd, limit=args.limit,
              dry_run=args.dry_run, priority=prio, progress=make_progress(wb))


class _OneRaw(WeaveBlock):
    """A WeaveBlock scoped to a single raw hash — the bare-`weave <hash>` path, kept on the shared
    driver (done-skip + marker) rather than a direct materialize call that would bypass them."""

    def __init__(self, raw_hash: str) -> None:
        super().__init__()
        self._raw_hash = raw_hash

    def items(self, root, *, source_id=None):
        if blobstore.has(self._raw_hash, root):
            yield self._raw_hash


if __name__ == "__main__":
    main()
