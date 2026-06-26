"""chunk — window a cleaned blob into bounded units and MATERIALIZE them as a chunkset: one
stored, auditable pointer-set per cleaned blob (ADR-0003). No on-the-fly generation — once built,
a chunk is resolved by slicing the immutable cleaned blob at recorded byte offsets, never
re-rendered.

Pipeline, every step a content-addressed blob with lineage:
    tap → raw blob → weave → cleaned blob → chunk → chunkset → glean → events

A chunk is a POINTER, not a copy: `[byte_start, byte_end)` into the cleaned blob's UTF-8 bytes.
Content-addressing makes the pointer exact (`cleaned_hash` pins the bytes), so a text copy could
only duplicate or diverge. Chunks never split a turn and never cross a compact segment; `budget`
is the consumer's window size. Resolving a chunk reads only stored bytes — `get(cleaned_hash)`
sliced — so the trust check is `quote in resolve(chunk)`, anchored to an immutable blob.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import blobstore, config, weave

CHUNKSET_FORMAT = "transcript.chunkset/1"  # source-kind . shape . PACKING version (bump if `_group` changes)
DEFAULT_BUDGET = 12000   # chars per chunk (~3k tokens); a single larger turn stands alone


@dataclass
class Chunk:
    cleaned_hash: str   # the cleaned blob this points into (lineage to raw, thence datastore)
    byte_start: int     # [byte_start, byte_end) into the cleaned blob's UTF-8 bytes
    byte_end: int
    turn_start: int     # turn index range (human-auditable)
    turn_end: int
    segment: int        # the compact segment this chunk lives in
    kinds: list[str]    # speaker kinds present


@dataclass
class _Group:
    turn_start: int
    turn_end: int
    char_start: int
    char_end: int
    segment: int
    kinds: list[str]


def _group(turns: list[weave.Turn], budget: int) -> list[_Group]:
    """Pack turns into windows ≤ `budget` chars, never splitting a turn; a compact boundary
    (segment change) always starts a fresh window. A turn larger than `budget` stands alone."""
    groups: list[_Group] = []
    cur: list[weave.Turn] = []
    cur_len, cur_seg = 0, None

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        groups.append(_Group(turn_start=cur[0].index, turn_end=cur[-1].index,
                             char_start=cur[0].start, char_end=cur[-1].end,
                             segment=cur[0].segment, kinds=sorted({t.kind for t in cur})))
        cur, cur_len = [], 0

    for t in turns:
        tlen = t.end - t.start
        if cur and (t.segment != cur_seg or cur_len + tlen + 2 > budget):  # +2 ≈ the "\n\n" join
            flush()
        cur.append(t)
        cur_len += tlen + 2
        cur_seg = t.segment
    flush()
    return groups


def _byte_index(text: str, positions) -> dict[int, int]:
    """char offset -> UTF-8 byte offset for the wanted positions, in one pass over the text."""
    want = sorted(set(positions))
    res, bi, pi = {}, 0, 0
    for ci in range(len(text) + 1):
        while pi < len(want) and want[pi] == ci:
            res[ci] = bi
            pi += 1
        if ci < len(text):
            bi += len(text[ci].encode("utf-8"))
    while pi < len(want):                       # any position == len(text)
        res[want[pi]] = bi
        pi += 1
    return res


def build(doc: weave.RenderedDoc, *, budget: int = DEFAULT_BUDGET) -> list[Chunk]:
    """Chunk an already-rendered cleaned doc into pointers (no blobstore writes)."""
    groups = _group(doc.turns, budget)
    bidx = _byte_index(doc.text, [p for g in groups for p in (g.char_start, g.char_end)])
    return [Chunk(cleaned_hash=doc.cleaned_hash, byte_start=bidx[g.char_start],
                  byte_end=bidx[g.char_end], turn_start=g.turn_start, turn_end=g.turn_end,
                  segment=g.segment, kinds=g.kinds) for g in groups]


def resolve(chunk: Chunk, root: Path | None = None) -> str:
    """The chunk's text, by slicing the immutable cleaned blob — stored bytes only, no render."""
    try:
        data = blobstore.get(chunk.cleaned_hash, root)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"cleaned blob {chunk.cleaned_hash[:12]} absent — re-materialize it from its raw "
            f"(its sidecar's `derived_from`) to resolve this chunk") from e
    return data.encode("utf-8")[chunk.byte_start:chunk.byte_end].decode("utf-8")


def load(chunkset_hash: str, root: Path | None = None) -> list[Chunk]:
    """The chunks of a stored chunkset blob."""
    d = json.loads(blobstore.get(chunkset_hash, root))
    return [Chunk(**c) for c in d["chunks"]]


def chunkset_for(cleaned_hash: str, root: Path | None = None, *,
                 budget: int | None = None) -> str | None:
    """The chunkset hash derived from a cleaned blob. A cleaned blob may have several chunksets
    (one per budget); pass `budget` to pick one, else the first found is returned (arbitrary
    across budgets)."""
    for m in blobstore.derived_for(cleaned_hash, root, fmt=CHUNKSET_FORMAT):
        if budget is None or m.get("tags", {}).get("budget") == budget:
            return m["content_hash"]
    return None


def materialize(raw_hash: str, *, budget: int = DEFAULT_BUDGET,
                root: Path | None = None) -> tuple[str, bool, list[Chunk]]:
    """Run the cleaned→chunkset step: let `weave` commit the cleaned blob (it owns that artifact's
    provenance), then freeze the chunkset of pointers into it. Idempotent — both writes are
    content-addressed no-ops on re-run. Returns (chunkset_hash, written, chunks)."""
    root = root or config.data_root()
    cleaned_hash, _, doc = weave.materialize(raw_hash, root=root)
    chunks = build(doc, budget=budget)
    payload = json.dumps({"cleaned_hash": cleaned_hash, "render_version": doc.render_version,
                          "format": CHUNKSET_FORMAT, "budget": budget,
                          "chunks": [asdict(c) for c in chunks]},
                         ensure_ascii=False, indent=2)
    tags = weave.tags_from_meta(blobstore.get_meta(raw_hash, root))
    cs_hash, written = blobstore.put_derived(
        payload, source_kind=doc.source_kind, derived_from=cleaned_hash, produced_by="chunk",
        render_version=doc.render_version,   # inherited: pins which render the offsets index into
        fmt=CHUNKSET_FORMAT, tags={**tags, "budget": budget}, root=root)
    return cs_hash, written, chunks


# --- CLI: materialize a chunkset for a blob, show the chunks -------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="chunk",
                                 description="Materialize a chunkset (pointers) for a transcript blob.")
    ap.add_argument("hash", nargs="?", help="raw blob hash (else --source-id)")
    ap.add_argument("--source-id", help="resolve the latest blob of this logical source")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="chars per chunk")
    ap.add_argument("--show", action="store_true", help="print each chunk's resolved text preview")
    args = ap.parse_args(argv)

    h = args.hash or (blobstore.latest_version(args.source_id) if args.source_id else None)
    if not h:
        ap.error("give a blob hash or --source-id")
    if not blobstore.has(h):
        ap.error(f"no such blob: {h}")

    cs_hash, written, chunks = materialize(h, budget=args.budget)
    cleaned = chunks[0].cleaned_hash if chunks else "—"
    print(f"raw {h[:12]} → cleaned {cleaned[:12]} → chunkset {cs_hash[:12]} "
          f"({'wrote' if written else 'exists'}, {len(chunks)} chunks)")
    for c in chunks:
        text = resolve(c)
        head = text.splitlines()[0] if text else ""
        print(f"  [{c.turn_start:>3}–{c.turn_end:<3}] seg{c.segment} "
              f"{c.byte_end - c.byte_start:>6}B {'/'.join(c.kinds):<18} {head[:60]}")
        if args.show:
            print(weave._truncate(text, 400), "\n")


if __name__ == "__main__":
    main()
