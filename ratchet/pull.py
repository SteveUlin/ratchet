"""pull — one command to sweep every registered source and run the $0 prep (ADR-0034).

    ratchet pull                 # tap projects + files + urls + feed-new-entries, then weave + chunk ($0)
    ratchet pull --max-usd 2     # ...and a budgeted glean tick after prep (the only spend)
    ratchet pull --dry-run       # list what WOULD be swept, per source — no network, no writes

`pull` is the "call one function to at least tap updated sources" verb (sulin's ask). It taps the
implicit projects sweep + every registered file/url/feed (`sources.py`), then runs the idempotent,
no-LLM prep (weave --all, chunk --all). It is $0 by default; `--max-usd C` adds a budgeted glean tick.

REUSE, NOT RE-IMPLEMENTATION (ADR-0009). `pull` drives the SAME `block.run` over the SAME
Tap/Weave/Chunk/Glean blocks a hand-run would — so every idempotency, cursor, per-item commit, and
error-isolation guarantee is inherited, never copied. It is the composition root (imports every
stage); `sources.py` stays the leaf registry so the two concerns test independently.

NETWORK HONESTY (§6). A source that touches the network degrades PER SOURCE. A dead FEED (its own
fetch/parse) is caught in `resolve_plan`, logged, marked failed in the summary, and the sweep
continues. A dead per-URL tap is isolated one level down by the Block driver (counted `errored`,
retried next pull) — `pull` inherits that idiom rather than re-inventing it.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import blobstore, block, chunk, completer, config, fetch, glean, sources, tap, weave

DEFAULT_DATASTORE = tap.DEFAULT_DATASTORE   # the implicit projects sweep root (ADR-0034 §2)
DEFAULT_MODEL = completer.DEFAULT_MODEL      # the glean tick's model, when --max-usd asks for one


@dataclass
class SourcePlan:
    """What a pull WILL touch, resolved from the registry (+ feeds, unless offline). `feed_entries`
    are the NEW entries per feed (id + link), retained so the seen cursor updates after tap;
    `feed_failures` are the feeds whose own fetch/parse raised (per-source degradation, §6)."""
    datastore: Path
    files: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)                      # registered feed URLs (always)
    feed_entries: list[tuple[str, str, str]] = field(default_factory=list)  # (feed_url, entry_id, entry_url)
    feed_failures: list[tuple[str, str]] = field(default_factory=list)      # (feed_url, one-line error)


def _oneline(ex: Exception) -> str:
    return " ".join(str(ex).split()) or ex.__class__.__name__


def resolve_plan(root: Path, *, datastore: Path,
                 feed_fetcher: Callable[[str], str], offline: bool) -> SourcePlan:
    """Read the registry into a plan. `offline` (the --dry-run path) skips ALL feed fetches — a
    dry-run resolves no entries and touches no network — so the plan lists feeds but not their
    entries. Online, each feed is fetched+parsed in ISOLATION: a raise is recorded in
    `feed_failures` and the sweep goes on."""
    entries = sources.load_registry(root)
    plan = SourcePlan(datastore=datastore,
                      files=[e["path"] for e in entries if e["kind"] == sources.KIND_FILE],
                      urls=[e["url"] for e in entries if e["kind"] == sources.KIND_URL],
                      feeds=[e["url"] for e in entries if e["kind"] == sources.KIND_FEED])
    if offline:
        return plan
    for feed_url in plan.feeds:
        try:
            parsed = sources.parse_feed(feed_fetcher(feed_url))
        except Exception as ex:                          # per-source isolation — a dead feed never aborts
            plan.feed_failures.append((feed_url, _oneline(ex)))
            continue
        seen = sources.feed_seen(root, feed_url)
        for entry_id, link in parsed:
            if entry_id not in seen:                     # only NEW posts fetch (the seen cursor, §3)
                plan.feed_entries.append((feed_url, entry_id, link))
    return plan


# --- the run: tap every source → prep → (optional) glean, one summary line per stage --------------

def run(root: Path | None = None, *, datastore: Path = DEFAULT_DATASTORE,
        max_usd: float | None = None, model: str = DEFAULT_MODEL, dry_run: bool = False,
        tap_fetcher: Callable | None = None, feed_fetcher: Callable[[str], str] | None = None,
        completer_factory: Callable[[str], completer.Completer] | None = None,
        out=sys.stdout) -> dict:
    """Sweep every source, prep, optionally glean. Returns a per-stage summary dict (also printed to
    `out`, one honest line per stage). The two network seams are injectable for offline tests
    (`tap_fetcher` = the --url page fetcher; `feed_fetcher` = the feed XML fetcher), exactly as
    `TapBlock(fetcher=…)` and `fetch_url`/`fetch_feed` bind in production."""
    root = config.ensure_layout(root)
    tap_fetcher = tap_fetcher or fetch.fetch_url
    feed_fetcher = feed_fetcher or sources.fetch_feed
    plan = resolve_plan(root, datastore=datastore, feed_fetcher=feed_fetcher, offline=dry_run)

    if dry_run:
        return _dry_run(plan, out=out)

    # ---- TAP: the projects sweep (implicit) + one explicit-list tap over files/urls/feed-entries ----
    # Two TapBlock runs because an explicit `files`/`urls` list REPLACES the datastore sweep
    # (TapBlock.items) — the projects sweep must be its own instance. skip_self is tap's ADR-0025
    # default so pull never re-ingests ratchet's own completer transcripts.
    reps = [block.run(tap.TapBlock(datastore=datastore, skip_self=config.data_root()), root=root)]
    doc_urls = plan.urls + [link for _f, _i, link in plan.feed_entries]
    if plan.files or doc_urls:                           # else TapBlock(files=(), urls=()) would sweep the datastore
        reps.append(block.run(tap.TapBlock(files=tuple(plan.files), urls=tuple(doc_urls),
                                           fetcher=tap_fetcher), root=root))
    # advance each feed's seen cursor for the entries that reached the store — a dead entry link has
    # no version, stays unseen, and retries next pull (the ADR-0033 dead-url retry idiom, §3).
    landed: dict[str, list[str]] = defaultdict(list)
    for feed_url, entry_id, link in plan.feed_entries:
        if blobstore.latest_version(link, root) is not None:
            landed[feed_url].append(entry_id)
    for feed_url, ids in landed.items():
        sources.mark_feed_seen(root, feed_url, ids)

    tap_new = sum(r.outputs for r in reps)
    tap_summary = {"new": tap_new, "examined": sum(r.examined for r in reps),
                   "errored": sum(r.errored for r in reps),
                   "feed_new_entries": len(plan.feed_entries), "feed_failures": len(plan.feed_failures)}
    _line(out, "tap", f"{tap_new} new raw · {tap_summary['errored']} errored · "
                      f"{tap_summary['examined']} examined  "
                      f"(feeds: {tap_summary['feed_new_entries']} new "
                      f"{'entry' if tap_summary['feed_new_entries'] == 1 else 'entries'}, "
                      f"{tap_summary['feed_failures']} unreachable)")
    for feed_url, err in plan.feed_failures:             # name each failed feed (per-source honesty)
        print(f"  feed FAILED {feed_url} — {err}", file=out)

    # ---- PREP: weave --all, chunk --all — deterministic, idempotent, $0 (ADR-0009 blocks) ----
    weave_rep = block.run(weave.WeaveBlock(), root=root)
    chunk_rep = block.run(chunk.ChunkBlock(), root=root)
    _line(out, "weave", f"{weave_rep.outputs} woven ({weave_rep.examined} examined)")
    _line(out, "chunk", f"{chunk_rep.outputs} chunkset(s) ({chunk_rep.examined} examined)")

    # ---- GLEAN: opt-in, budgeted. $0 by default — pull never spends unless --max-usd asks (§5) ----
    glean_summary = None
    if max_usd is not None:
        factory = completer_factory or completer.make_cli_completer
        blk = glean.GleanBlock(factory(model), model=model, targets=glean.all_chunksets(root))
        greport = block.run(blk, max_usd=max_usd, root=root, priority=block.priority_strategy("greedy"))
        glean_summary = {"events": greport.outputs, "cost_usd": greport.cost_usd,
                         "examined": greport.examined, "errored": greport.errored,
                         "stopped_on_budget": greport.stopped_on_budget}
        tail = "  [stopped: budget]" if greport.stopped_on_budget else ""
        _line(out, "glean", f"{greport.outputs} events · {blk.rejected} rejected · "
                            f"${greport.cost_usd:.4f}{tail}")

    return {"tap": tap_summary,
            "weave": {"woven": weave_rep.outputs, "examined": weave_rep.examined},
            "chunk": {"chunksets": chunk_rep.outputs, "examined": chunk_rep.examined},
            "glean": glean_summary}


def _line(out, stage: str, body: str) -> None:
    print(f"{stage:<6}  {body}", file=out)


def _dry_run(plan: SourcePlan, *, out) -> dict:
    """List the plan, offline: implicit projects + every registered source. Feeds are NOT resolved
    (that needs a fetch), so their entries show as "resolved on a real pull"."""
    print("pull --dry-run — would sweep (no network, no writes):", file=out)
    print(f"  projects  {plan.datastore}  [implicit]", file=out)
    for f in plan.files:
        print(f"  file      {f}", file=out)
    for u in plan.urls:
        print(f"  url       {u}  (re-fetched; unchanged → no-op)", file=out)
    for feed_url in plan.feeds:
        print(f"  feed      {feed_url}  (new entries resolved on a real pull)", file=out)
    return {"tap": {"files": len(plan.files), "urls": len(plan.urls), "feeds": len(plan.feeds)},
            "weave": None, "chunk": None, "glean": None}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="pull",
        description="One command: tap every registered source (projects + files + urls + feed-new-entries), "
                    "then run the $0 prep (weave, chunk). --max-usd adds a budgeted glean tick.")
    ap.add_argument("--datastore", type=Path, default=DEFAULT_DATASTORE,
                    help=f"the implicit projects (transcripts) sweep root (default: {DEFAULT_DATASTORE})")
    ap.add_argument("--max-usd", type=float, metavar="C",
                    help="ALSO run a glean tick after prep, capped at this spend (default: none — pull is $0)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"the glean tick's model, when --max-usd asks for one (default: {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be swept, per source; no network, no writes")
    args = ap.parse_args(argv)
    run(datastore=args.datastore, max_usd=args.max_usd, model=args.model, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
