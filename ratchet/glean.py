"""glean — the first LLM stage: filter chunks for durable signal and extract verifiable
**events** (ADR-0004).

    tap → raw blob → weave → cleaned blob → chunk → chunkset → glean → events
                                                          (extract, LLM)

An event is a THIN POINTER into the cleaned blob, not a copy of its text: a byte span
(`evidence`) whose verbatim quote is `get(cleaned_hash)[span]` — TRUSTED — plus a one-sentence
model `summary` — UNTRUSTED. The trust anchor (the whole point): the model returns a quote, and
glean accepts the event only if that quote is a real substring of `resolve(chunk)`; a hallucinated
or paraphrased quote dies deterministically, before any event is written.

Events are NOT blobstore blobs. A chunkset→chunks step is a *deterministic* function (so `chunk`
content-addresses it); an LLM extraction is *not* — the same `derived_from` yields different bytes
across runs/models, and a per-run shard spans many cleaned blobs. So events go to an append-only
log (`events/glean-<run_id>.jsonl`, `.partial`→rename on clean exit; readers glob+merge), exactly
the event store of ADR-0001 §1/§4. Lineage stays content-addressed: every event embeds
`cleaned_hash`, and `get_meta(cleaned_hash).derived_from` walks to the raw blob, thence datastore.

The LLM call is the only impure step, so it is injected as a `Completer`; the core (prompt build,
parse, quote verification, span math, cost, idempotency) is pure and tested offline. The shipped
default shells out to the authed `claude` CLI (ADR-0004).
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import blobstore, chunk, completer, config, runlog, weave
from .completer import Completer  # the LLM seam (Completion + the default binding) lives in `completer`

PROMPT_VERSION = "glean/2"     # bump to re-extract over the same frozen chunks (idempotency key)
MIN_QUOTE_BYTES = 12           # a shorter "quote" is ambiguous (matches anywhere) → low-value evidence
SUMMARY_MAX = 240              # untrusted-field hygiene: cap the model's summary

# Markers are the FILTER classification (ADR-0005): each learning is scored 0-1 on several salience
# axes (multi-label, not one bucket) so a downstream synthesis ("dream") layer can route and cluster.
# They classify, they do not gate — the gate is "is this a durable learning at all", so a plain
# preference (all markers low) still comes through.
MARKER_KINDS = ("surprise", "insight", "research")

SYSTEM_PROMPT = (
    "You extract durable, reusable learnings from a single excerpt of a Claude Code session "
    "transcript, for a system that mines transcripts to improve a developer's future sessions.\n\n"
    "A learning is anything a FUTURE session would be better for knowing. Most excerpts contain "
    "nothing durable — that is expected; return an empty list rather than inventing signal.\n\n"
    "For each learning, return:\n"
    "- \"quote\": the EXACT span of transcript text that is the evidence, copied VERBATIM — "
    "character for character, including punctuation and casing. Do not paraphrase, summarize, "
    "translate, trim, or join across gaps with \"...\". A quote that is not a literal substring of "
    "the excerpt is discarded.\n"
    "- \"summary\": one imperative sentence (<= 200 chars) a future session could act on.\n"
    "- \"markers\": an object scoring 0-1 how strongly this learning is each of:\n"
    "    surprise — something broke an expectation: a command or test failed, an assumption was "
    "wrong, or the user corrected or redirected the work.\n"
    "    insight  — a non-obvious realization about this person, project, or how to do something well.\n"
    "    research — a researched finding or external fact established during the session.\n"
    "  A learning may score on several at once; a plain preference or fact that is none of these "
    "scores them all low (but is still worth returning).\n"
    "- \"confidence\": a number 0-1, how durable and reusable this is.\n\n"
    "Output ONLY a JSON object: {\"events\": [ ... ]}. No prose, no code fences. If the excerpt "
    "holds nothing durable, output {\"events\": []}."
)

# Cheap, no-LLM structural priors fed to the model so it weighs the most extraction-worthy cues. They
# nudge the markers; they NEVER gate (the prior-art warning: a regex marker alone fires on quoted
# errors / rhetorical "no"s — high recall, low precision, so the LLM adjudicates). Surprise = a
# command/test failed OR the user redirected the work — the two highest-value, cheaply-detectable cues.
_FAILURE_CUES = ("[error]", "traceback (most recent call last)", "assertionerror", "exception:",
                 "npm err!", " failed", "exit code 1", "exited 1", "fatal:", "panic:", "fail:")
_REDIRECT_CUES = (  # deliberately loose substrings ("no " fires on "another") — the cue only raises a
                    # prior the LLM adjudicates, so over-firing is cheap; under-firing would cost recall
    "actually", "no,", "no ", "don't", "do not", "instead", "wait", "stop",
    "not ", "wrong", "revert", "undo", "rather", "that's wrong", "i said")


# --- filter + parse + verify ----------------------------------------------------------------

def has_signal_potential(text: str, *, min_chars: int = 80) -> bool:
    """A cheap, conservative pre-filter — skip the LLM call for excerpts that *cannot* carry a
    durable learning (too small, or no human/assistant turn — pure tool noise or a lone compact
    marker). The model is the real filter; this only spares obvious junk an API call."""
    if len(text) < min_chars:
        return False
    return "[user]" in text or "[assistant]" in text


def structural_cues(text: str) -> list[str]:
    """Free, no-LLM marker priors. A rendered tool error signals a likely failure/surprise; a short
    corrective user turn signals a likely redirect (also a surprise). HIGH RECALL, low precision —
    the cues only raise a prior for the model to adjudicate, never drop a chunk (a false-positive cue
    is cheap; a missed durable learning is not — ADR-0005)."""
    low = text.lower()
    cues = []
    if any(m in low for m in _FAILURE_CUES):
        cues.append("a command/test failure or error appears (a possible surprise)")
    for seg in low.split("[user]")[1:]:         # each user turn's text, up to the next speaker tag
        u = seg.split("[assistant]")[0].strip()
        if u and len(u) < 240 and any(c in u for c in _REDIRECT_CUES):
            cues.append("a user turn looks corrective/redirecting (a possible surprise)")
            break
    return cues


def _user_prompt(chunk_text: str, cues: list[str]) -> str:
    hint = ("\n\nStructural cues (weigh, do not over-trust): " + "; ".join(cues)) if cues else ""
    return f"Excerpt:\n{chunk_text}{hint}"


def parse_candidates(text: str) -> list[dict]:
    """Pull the `events` array out of the model's object (the ```json fence is handled by the shared
    `completer.parse_json_object`). Defensive: malformed output → no candidates, no crash."""
    obj = completer.parse_json_object(text)
    events = obj.get("events") if obj else None
    return [c for c in events if isinstance(c, dict)] if isinstance(events, list) else []


def event_id(cleaned_hash: str, byte_start: int, byte_end: int) -> str:
    """sha256(cleaned_hash + first span)[:12] — span-derived, so two runs dedup on the same
    evidence regardless of model, prompt, or run (ADR-0004). Consumers dedup by this id."""
    return hashlib.sha256(f"{cleaned_hash}:{byte_start}:{byte_end}".encode()).hexdigest()[:12]


_clean_score = completer.clean_score   # shared untrusted-score hygiene (clamp + scrub NaN/inf)


def _clean_markers(v) -> dict:
    """Coerce the model's untrusted marker object to a clamped score per known kind (missing → 0)."""
    v = v if isinstance(v, dict) else {}
    return {k: _clean_score(v.get(k)) for k in MARKER_KINDS}


def verify(candidate: dict, ch: chunk.Chunk, cleaned_bytes: bytes) -> tuple[int, int] | None:
    """The trust anchor — and nothing else. Accept a candidate's quote iff it is a real substring of
    the chunk's OWN bytes, and return its `[start, end)` span IN THE CLEANED BLOB (never the quote
    text). A hallucinated, paraphrased, all-whitespace, or too-short quote returns None — rejected
    deterministically, before any event exists."""
    quote = candidate.get("quote")
    if not isinstance(quote, str) or not quote.strip():   # all-whitespace is real text but zero signal
        return None
    qb = quote.encode("utf-8")
    if len(qb) < MIN_QUOTE_BYTES:
        return None
    off = cleaned_bytes[ch.byte_start:ch.byte_end].find(qb)   # within this chunk's bytes only
    if off < 0:
        return None
    bstart = ch.byte_start + off
    bend = bstart + len(qb)
    if cleaned_bytes[bstart:bend] != qb:        # invariant (holds by construction); guards offset regressions
        return None
    return bstart, bend


def build_event(candidate: dict, ch: chunk.Chunk, span: tuple[int, int], *,
                model: str, run_id: str) -> dict:
    """Assemble the event record for a verified span — the event FORMAT lives here (ADR-0004/0005). An
    event is a thin pointer (the span, never the quote text) + the untrusted, hygiene-cleaned model
    fields (summary, markers, confidence) + producer provenance. It stays a plain dict, deliberately:
    an event is a JSONL serialization boundary the downstream judge reads as JSON, not an in-memory
    value type like `Chunk` (ADR-0004)."""
    bstart, bend = span
    return {
        "id": event_id(ch.cleaned_hash, bstart, bend),
        "cleaned_hash": ch.cleaned_hash,
        "evidence": [{"byte_start": bstart, "byte_end": bend}],
        "summary": str(candidate.get("summary", "")).strip()[:SUMMARY_MAX],
        "markers": _clean_markers(candidate.get("markers")),
        "confidence": _clean_score(candidate.get("confidence"), 0.5),
        "producer": {"stage": "glean", "model": model, "prompt_version": PROMPT_VERSION,
                     "run_id": run_id, "cost_usd": None},
        "supersedes": None,
        "status": "extracted",
    }


# --- extract one chunkset -------------------------------------------------------------------

@dataclass
class ChunksetResult:
    chunkset_hash: str
    cleaned_hash: str
    events: list[dict]
    rejected: int          # candidates whose quote failed verification (hallucinated/short)
    calls: int             # LLM calls made (chunks that passed the pre-filter)
    cost_usd: float        # authoritative: includes no-signal calls (events amortize only theirs)
    errored: int = 0       # chunks whose LLM call failed after retries (isolated, run continued)

    @classmethod
    def empty(cls, chunkset_hash: str) -> "ChunksetResult":
        """Nothing to extract (no chunks) — legitimately done."""
        return cls(chunkset_hash, "", [], 0, 0, 0.0)

    @classmethod
    def absent(cls, chunkset_hash: str) -> "ChunksetResult":
        """The chunkset or its cleaned blob is missing (e.g. a TTL-reclaimed derived blob): a failed
        chunkset (calls==0, errored>0) so `run` retries it rather than marking it done."""
        return cls(chunkset_hash, "", [], 0, 0, 0.0, errored=1)


def extract_chunkset(chunkset_hash: str, complete: Completer, *, model: str, run_id: str,
                     root: Path | None = None) -> ChunksetResult:
    """Filter then extract over one materialized chunkset (consume only — no re-fetch, no re-render).
    Each surviving chunk is resolved by slicing the immutable cleaned blob, sent to the model, and
    every returned quote is verified against those same bytes."""
    try:
        chunks = chunk.load(chunkset_hash, root)
        if not chunks:
            return ChunksetResult.empty(chunkset_hash)
        cleaned_hash = chunks[0].cleaned_hash
        cleaned_bytes = blobstore.get(cleaned_hash, root).encode("utf-8")
    except FileNotFoundError:
        return ChunksetResult.absent(chunkset_hash)

    events: list[dict] = []
    seen: set[str] = set()
    rejected = calls = errored = 0
    total_cost = 0.0
    for ch in chunks:
        text = cleaned_bytes[ch.byte_start:ch.byte_end].decode("utf-8")
        if not has_signal_potential(text):
            continue
        prompt = _user_prompt(text, structural_cues(text))   # our code, outside the try (bugs surface)
        try:
            comp = complete(SYSTEM_PROMPT, prompt)
        except Exception:
            errored += 1                         # the injected seam is untrusted; isolate ANY failure
            continue
        calls += 1
        call_cost = completer.cost_of(comp)
        total_cost += call_cost
        accepted = []
        for cand in parse_candidates(comp.text):
            span = verify(cand, ch, cleaned_bytes)   # trust check first ...
            if span is None:
                rejected += 1
                continue
            ev = build_event(cand, ch, span, model=model, run_id=run_id)   # ... then build the record
            if ev["id"] not in seen:             # same span twice in a run → one event
                seen.add(ev["id"])
                accepted.append(ev)
        share = round(call_cost / len(accepted), 8) if accepted else 0.0  # amortize over its events
        for ev in accepted:
            ev["producer"]["cost_usd"] = share
        events.extend(accepted)
    return ChunksetResult(chunkset_hash, cleaned_hash, events, rejected, calls, total_cost, errored)


# --- the event store + processed ledger (runlog: append-only shards, glob+merge) -------------

GLEAN_KEY = ("chunkset_hash", "prompt_version", "model")   # the processed-ledger idempotency key


def load_events(root: Path | None = None) -> list[dict]:
    """Every committed event (`events/glean-*.jsonl`; `.partial` shards ignored). Raw — consumers
    dedup by id (ADR-0004). Thin wrapper over the shared `runlog` substrate."""
    return runlog.read_stream("glean", root)


def processed_index(root: Path | None = None) -> set[tuple[str, str, str]]:
    """The done-set keyed by (chunkset_hash, prompt_version, model). A re-run skips a key already
    here; bumping PROMPT_VERSION or model changes the key (re-extract over the same frozen chunks)."""
    return runlog.processed_index("glean", GLEAN_KEY, root)


# --- run: orchestrate over a set of chunksets -----------------------------------------------

@dataclass
class RunReport:
    run_id: str
    results: list[ChunksetResult] = field(default_factory=list)
    skipped: int = 0           # chunksets already done for (prompt_version, model)
    stopped_on_budget: bool = False

    @property
    def events(self) -> int:
        return sum(len(r.events) for r in self.results)

    @property
    def rejected(self) -> int:
        return sum(r.rejected for r in self.results)

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    @property
    def errored(self) -> int:
        return sum(r.errored for r in self.results)


def run(chunkset_hashes: list[str], complete: Completer, *, model: str = completer.DEFAULT_MODEL,
        max_usd: float | None = None, root: Path | None = None) -> RunReport:
    """Extract over chunksets idempotently. A chunkset already in the processed ledger for this
    (prompt_version, model) is skipped with zero LLM calls. Events are written durable FIRST, then
    the processed marker (commit) — so a crash reprocesses and never leaves a false 'done' (a
    reprocessed event re-appears under the same id; consumers dedup by id, ADR-0001 §6). `.partial`
    shards rename to final only on clean exit (incl. a budget stop). A chunkset whose *every* call
    failed (transient outage / absent blob) writes no marker and is retried next run; a partially
    failed one is marked done (its errored chunks need a prompt/model re-key to retry)."""
    root = config.ensure_layout(root)
    runlog.sweep_partials(root)
    rid = runlog.run_id()
    done = processed_index(root)
    report = RunReport(run_id=rid)

    with runlog.ShardRun("glean", rid, root) as sh:
        for cs in chunkset_hashes:
            key = (cs, PROMPT_VERSION, model)
            if key in done:
                report.skipped += 1
                continue
            if max_usd is not None and report.cost_usd >= max_usd:
                report.stopped_on_budget = True
                break
            res = extract_chunkset(cs, complete, model=model, run_id=rid, root=root)
            for ev in res.events:                 # events durable first ...
                sh.emit(ev)
            report.results.append(res)
            if res.calls == 0 and res.errored:    # nothing succeeded (transient outage / absent blob) —
                continue                          # don't write a 'done' marker; retry the chunkset next run
            sh.mark({                             # ... then the commit marker (the chunkset is done)
                "chunkset_hash": cs, "cleaned_hash": res.cleaned_hash,
                "prompt_version": PROMPT_VERSION, "model": model, "run_id": rid, "at": runlog.now(),
                "n_events": len(res.events), "n_rejected": res.rejected, "n_errored": res.errored,
                "n_calls": res.calls, "cost_usd": round(res.cost_usd, 8),
            })
            done.add(key)
    return report


# --- target resolution: glean consumes existing chunksets (never re-chunks) -----------------

def all_chunksets(root: Path | None = None) -> list[str]:
    """Every materialized chunkset in the store, by one scan over the derived sidecars."""
    return [m["content_hash"] for m in blobstore.iter_meta(root)
            if m.get("format") == chunk.CHUNKSET_FORMAT]


def chunkset_for_source(source_id: str, root: Path | None = None) -> str | None:
    """The chunkset of a logical source's latest snapshot: raw → cleaned → chunkset, all by sidecar
    scan (no re-fetch, no re-render). None if `chunk` hasn't materialized one yet."""
    raw = blobstore.latest_version(source_id, root)
    if not raw:
        return None
    cleaned = next(blobstore.derived_for(raw, root, fmt=weave.RENDER_FORMAT), None)
    if not cleaned:
        return None
    return chunk.chunkset_for(cleaned["content_hash"], root)


# --- CLI ------------------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="glean", description="Extract verifiable events from materialized chunksets (LLM).")
    ap.add_argument("hash", nargs="?", help="a chunkset hash (else --source-id / --all)")
    ap.add_argument("--source-id", help="the chunkset of this logical source's latest snapshot")
    ap.add_argument("--all", action="store_true", help="every materialized chunkset in the store")
    ap.add_argument("--model", default=completer.DEFAULT_MODEL,
                    help=f"claude model (default: {completer.DEFAULT_MODEL})")
    ap.add_argument("--limit", type=int, help="cap chunksets examined this run (before the done-skip)")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this")
    ap.add_argument("--dry-run", action="store_true",
                    help="list chunksets that would be processed (skips done); no LLM calls")
    args = ap.parse_args(argv)

    if args.all:
        targets = all_chunksets()
    elif args.source_id:
        cs = chunkset_for_source(args.source_id)
        if not cs:
            ap.error(f"no chunkset for source {args.source_id!r} — run `chunk` first")
        targets = [cs]
    elif args.hash:
        if not blobstore.has(args.hash):
            ap.error(f"no such blob: {args.hash}")
        targets = [args.hash]
    else:
        ap.error("give a chunkset hash, --source-id, or --all")
    if args.limit is not None:
        targets = targets[:args.limit]

    if args.dry_run:
        done = processed_index()
        todo = [c for c in targets if (c, PROMPT_VERSION, args.model) not in done]
        print(f"{len(targets)} chunkset(s), {len(todo)} to process "
              f"({len(targets) - len(todo)} already done for {PROMPT_VERSION}/{args.model}):")
        for c in todo:
            print(f"  would glean {c[:12]}")
        return

    complete = completer.make_cli_completer(args.model)
    report = run(targets, complete, model=args.model, max_usd=args.max_usd)
    for r in report.results:
        err = f", {r.errored} errored" if r.errored else ""
        print(f"  {r.chunkset_hash[:12]} → cleaned {r.cleaned_hash[:12]}: "
              f"{len(r.events)} events, {r.rejected} rejected, {r.calls} calls{err}, ${r.cost_usd:.4f}")
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\nglean-{report.run_id}: {len(report.results)} processed, {report.skipped} skipped, "
          f"{report.events} events, {report.rejected} rejected{errs}, ${report.cost_usd:.4f}{tail}")


if __name__ == "__main__":
    main()
