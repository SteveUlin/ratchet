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

from . import blobstore, block, config, weave

CHUNKSET_FORMAT = "transcript.chunkset/1"  # source-kind . shape . PACKING version (bump if `_group` changes)
DEFAULT_BUDGET = 12000   # chars per chunk (~3k tokens); a single larger turn stands alone
OUT_NOUN = "chunksets"   # the per-item output noun the Progress bar/line shows


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


# --- the Block: chunk as a uniform stage (item = a cleaned blob → chunkset; ADR-0009) ------

class ChunkBlock:
    """chunk as a `block.Block` — the batch/idempotent surface over `materialize`. Per ADR-0009 the
    item is the CLEANED blob (its identity downstream); `process` maps cleaned → its raw parent via
    `derived_from`, then calls the workhorse `materialize(raw)` (which re-weaves + builds + freezes
    the chunkset, all idempotent/content-addressed). Deterministic, no LLM, cost 0.

    Idempotency keys on (cleaned_hash, render_version, budget): the chunkset is a pure function of
    those three. Bumping budget re-chunks (a distinct chunkset by content already; the marker now
    records it too); bumping RENDER_VERSION re-renders upstream → a new cleaned blob → a new item."""

    name = "chunk"
    commits_per_item = True
    finalize = block.no_finalize
    marker_extra = block.no_marker_extra
    priority = block.no_priority
    age = block.no_age                       # cheap deterministic stage — aging is moot (ADR-0021)

    def __init__(self, budget: int = DEFAULT_BUDGET) -> None:
        self.budget = budget
        self.params: tuple[tuple[str, str], ...] = (
            ("render_version", weave.RENDER_VERSION), ("budget", str(budget)))

    def items(self, root: Path, *, source_id: str | None = None):
        """Enumerate cleaned blobs to chunk. --all → every derived blob of the cleaned-doc format
        (one scan of iter_meta). --source-id → walk the source's latest raw → its cleaned blob(s)."""
        if source_id is not None:
            raw = blobstore.latest_version(source_id, root)
            if raw is None:
                return
            for m in blobstore.derived_for(raw, root, fmt=weave.RENDER_FORMAT):
                yield m["content_hash"]
            return
        for m in blobstore.iter_meta(root):
            if m.get("kind") == "derived" and m.get("format") == weave.RENDER_FORMAT:
                yield m["content_hash"]

    def key(self, cleaned_hash: str) -> str:
        """The cleaned blob hash — the chunkset is a deterministic function of
        (cleaned_hash, render_version, budget)."""
        return cleaned_hash

    def process(self, cleaned_hash: str, *, root: Path, run_id: str) -> tuple[int, float]:
        """cleaned → raw (via derived_from) → materialize(raw, budget). 1 output if the chunkset was
        newly written else 0; cost always 0. A TTL-reclaimed raw parent (can't happen — raw is kept
        forever, ADR-0003) would surface here as a KeyError/FileNotFoundError the driver isolates."""
        raw = blobstore.get_meta(cleaned_hash, root)["derived_from"]
        _, written, _ = materialize(raw, budget=self.budget, root=root)
        return (1 if written else 0, 0.0)


# --- CLI: batch-materialize chunksets via the block, or inspect ONE blob's chunks ----------

def _show(cs_hash: str, h: str, chunks: list[Chunk], full: bool) -> None:
    """Single-blob inspector: the chunk table (and, with --show, each resolved-text preview)."""
    cleaned = chunks[0].cleaned_hash if chunks else "—"
    print(f"raw {h[:12]} → cleaned {cleaned[:12]} → chunkset {cs_hash[:12]} ({len(chunks)} chunks)")
    for c in chunks:
        text = resolve(c)
        head = text.splitlines()[0] if text else ""
        print(f"  [{c.turn_start:>3}–{c.turn_end:<3}] seg{c.segment} "
              f"{c.byte_end - c.byte_start:>6}B {'/'.join(c.kinds):<18} {head[:60]}")
        if full:
            print(weave._truncate(text, 400), "\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="chunk",
                                 description="Materialize chunksets (pointers) for transcript blobs.")
    ap.add_argument("hash", nargs="?", help="raw blob hash (else --source-id / --all)")
    ap.add_argument("--source-id", help="this logical source's latest blob")
    ap.add_argument("--all", action="store_true", help="chunk every cleaned blob in the store")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="chars per chunk")
    ap.add_argument("--show", action="store_true",
                    help="print one blob's chunk table + resolved-text previews (no batch)")
    # uniform block knobs (per ADR-0009)
    ap.add_argument("--limit", type=int, help="cap items EXAMINED")
    ap.add_argument("--dry-run", action="store_true", help="list what would chunk; do nothing")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--max-usd", type=float, help="(no cost; inert — chunk never calls an LLM)")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy (inert — chunk emits no per-item signal, so every policy is arrival order)")
    args = ap.parse_args(argv)


    # --show is the single-blob inspector path (like weave --render): materialize this ONE blob and
    # print its chunks. A bare hash here is a RAW hash (as today), so materialize takes it directly.
    if args.show:
        h = args.hash or (blobstore.latest_version(args.source_id) if args.source_id else None)
        if not h:
            ap.error("give a blob hash or --source-id to --show")
        if not blobstore.has(h):
            ap.error(f"no such blob: {h}")
        cs_hash, _, chunks = materialize(h, budget=args.budget)
        _show(cs_hash, h, chunks, full=True)
        return

    if not (args.all or args.source_id or args.hash):
        ap.error("give a blob hash, --source-id, or --all")

    # the stage owns its Progress now (the driver only speaks the protocol). None for --quiet/--dry-run;
    # else built from this stage's args + OUT_NOUN. chunk has no LLM cost, so cap is omitted.
    def make_progress(blk):
        if args.quiet or args.dry_run:
            return None
        return block.Progress(blk.name, params=dict(blk.params), out_noun=OUT_NOUN, verbose=args.verbose)

    # The batch/idempotent path: drive the block over cleaned blobs. A bare hash is a RAW hash today;
    # map it to its cleaned blob (materialize the raw first) so the single-item key is the cleaned
    # hash, matching the block's item identity — then run a one-item block over that cleaned hash.
    prio = block.priority_strategy(args.priority)
    blk = ChunkBlock(budget=args.budget)
    if args.hash and not args.all:
        if not blobstore.has(args.hash):
            ap.error(f"no such blob: {args.hash}")
        cleaned_hash, _, _ = weave.materialize(args.hash)   # raw → cleaned (idempotent)
        one = _OneCleaned(cleaned_hash, budget=args.budget)
        block.run(one, dry_run=args.dry_run, priority=prio, progress=make_progress(one))
        return
    block.run(blk, source_id=args.source_id, max_usd=args.max_usd, limit=args.limit,
              dry_run=args.dry_run, priority=prio, progress=make_progress(blk))


class _OneCleaned(ChunkBlock):
    """A ChunkBlock scoped to a single cleaned hash — the bare-`chunk <raw-hash>` path (the raw is
    mapped to its cleaned blob first), kept on the shared driver (done-skip + marker)."""

    def __init__(self, cleaned_hash: str, *, budget: int = DEFAULT_BUDGET) -> None:
        super().__init__(budget=budget)
        self._cleaned_hash = cleaned_hash

    def items(self, root, *, source_id=None):
        if blobstore.has(self._cleaned_hash, root):
            yield self._cleaned_hash


if __name__ == "__main__":
    main()
