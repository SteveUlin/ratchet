"""glean — the first LLM stage: filter chunks for durable signal and extract verifiable
**events** (ADR-0004).

    tap → raw blob → weave → cleaned blob → chunk → chunkset → glean → events
                                                          (extract, LLM)

An event is a THIN POINTER into the cleaned blob, not a copy of its text: a byte span
(`evidence`) whose verbatim quote is `get(cleaned_hash)[span]` — TRUSTED — plus a one-sentence
model `summary` — UNTRUSTED. The trust anchor (the whole point): the model returns a quote, and
glean accepts the event only if that quote is a real substring of `resolve(chunk)`; a hallucinated
or paraphrased quote dies deterministically, before any event is written.

Events ARE blobs now (ADR-0007): a logical event is a RAW blob whose `source_id` is the
span-derived, deterministic `event_id`, and each extraction is an immutable VERSION (content =
`blob_hash(canonical-json(record))`). ADR-0004's objection — content-addressing a non-deterministic
output is wrong — dissolves by splitting identity (`event_id`) from content: a re-extraction with a
changed summary/markers is just a new version under the same `event_id`, `prev`-linked, latest wins;
a byte-identical re-extraction no-ops. Lineage stays content-addressed: every event embeds
`cleaned_hash`, and `get_meta(cleaned_hash).derived_from` walks to the raw blob, thence datastore.

THE UNIT OF WORK IS THE CHUNK, not the chunkset (ADR-0009). glean is a `block.Block`: `items()`
explodes each target chunkset into its individual chunks, `process()` extracts ONE chunk (one LLM
call), and the driver writes a `processed` marker PER CHUNK — including a filter-skipped chunk (0
events, 0 cost), so the done-set stays exact. "Already done" is that per-chunk `processed` decision
blob, keyed on `(chunk_key, prompt_version, model)`; the chunkset is now just the container `items()`
enumerates chunks from. This makes Ctrl-C cheap: a giant session no longer loses every chunk's work
on a transient outage or kill — only the in-flight chunk re-does (the heart of ADR-0009 fix #2).

The LLM call is the only impure step, so it is injected as a `Completer`; the core (prompt build,
parse, quote verification, span math, cost, idempotency) is pure and tested offline. The shipped
default shells out to the authed `claude` CLI (ADR-0004).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import blobstore, block, chunk, completer, config, weave
from .completer import Completer  # the LLM seam (Completion + the default binding) lives in `completer`

PROMPT_VERSION = "glean/3"     # bump to re-extract over the same frozen chunks (idempotency key);
                               # glean/3 adds the RELEVANCE marker judged vs the concept digest (4b/ADR-0019)
MIN_QUOTE_BYTES = 12           # a shorter "quote" is ambiguous (matches anywhere) → low-value evidence
SUMMARY_MAX = 240              # untrusted-field hygiene: cap the model's summary
OUT_NOUN = "events"            # the per-item output noun the Progress bar/line shows (glean emits events)

# Markers are the FILTER classification (ADR-0005): each learning is scored 0-1 on several salience
# axes (multi-label, not one bucket) so a downstream synthesis ("dream") layer can route and cluster.
# They classify, they do not gate — the gate is "is this a durable learning at all", so a plain
# preference (all markers low) still comes through.
MARKER_KINDS = ("surprise", "insight", "research")

# RELEVANCE is the per-event Bayesian-surprise-vs-the-store verdict (4b/ADR-0019, the long-deferred
# roadmap-#1 marker of ADR-0005): each event is judged against the concept digest ("what we already
# know") injected into the prompt — `novel` (nothing covers it) / `known` (a concept already states it)
# / `contradicts` (it overturns a concept). It FEEDS dream's salience ORDERING (novel/contradicts drain
# first, known sinks) — it does NOT gate. RECALL-FIRST coercion: an unknown/missing verdict → `novel`,
# the safe default — never silently call a learning `known` and sink it (the invisible false-negative is
# the costly error). It is the CHEAP, EARLY complement to dream's precise LATE belief-change judgment.
RELEVANCE_KINDS = ("novel", "known", "contradicts")
RELEVANCE_DEFAULT = "novel"    # the recall-safe default: doubt resolves toward "process it", never "drop it"


def clean_relevance(v) -> str:
    """Coerce the model's untrusted relevance verdict to a known kind, defaulting unknown/missing →
    `novel` (the recall-safe direction — `novel` boosts/keeps an event in dream's queue, `known` sinks it;
    so doubt must resolve to `novel`, never `known`). Mirrors `_clean_markers`/`_clean_relation`; dream
    reads the same coercion when it scales salience, so producer and consumer agree on one spelling."""
    return v if v in RELEVANCE_KINDS else RELEVANCE_DEFAULT

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
    "- \"confidence\": a number 0-1, how durable and reusable this is.\n"
    "- \"relevance\": judge this learning against WHAT WE ALREADY KNOW — the catalog of already-known "
    "concepts shown below the excerpt — one of:\n"
    "    novel — nothing in what we already know covers this; it is new information.\n"
    "    known — a known concept already states this; it is not new.\n"
    "    contradicts — this OVERTURNS or corrects something a known concept asserts (a belief change — "
    "the most important to surface).\n"
    "  When the catalog is empty or you are unsure, answer \"novel\": never suppress a learning by "
    "calling it known.\n\n"
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


def _user_prompt(chunk_text: str, cues: list[str], known: str | None = None) -> str:
    hint = ("\n\nStructural cues (weigh, do not over-trust): " + "; ".join(cues)) if cues else ""
    # The concept digest ("what we already know") rides BELOW the excerpt so the model judges each event's
    # `relevance` against it (4b/ADR-0019). It is PROVENANCE-RELEVANT to this chunk (ordered by facet
    # overlap), so the concepts most likely to already cover it survive the digest's budget cut.
    known_block = (f"\n\nWHAT WE ALREADY KNOW (judge each learning's relevance against this):\n{known}"
                   if known else "")
    return f"Excerpt:\n{chunk_text}{hint}{known_block}"


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
    fields (summary, markers, confidence) + intrinsic provenance. It stays a plain dict, deliberately:
    an event is a JSON serialization boundary (now a blob's content) the downstream judge reads as
    JSON, not an in-memory value type like `Chunk` (ADR-0004).

    ADR-0007 reshapes the record: `id` is now the blob's `source_id` (authoritative in meta), kept
    here only as a convenience content mirror; `status` is DROPPED (state is a decision, never an
    in-record field). `producer` stays in content (the cost-amortization target) and is mirrored into
    `origin_ref` at ingest so "who/when/how produced this version" is answerable from meta alone."""
    bstart, bend = span
    return {
        "id": event_id(ch.cleaned_hash, bstart, bend),
        "cleaned_hash": ch.cleaned_hash,
        "evidence": [{"byte_start": bstart, "byte_end": bend}],
        "summary": str(candidate.get("summary", "")).strip()[:SUMMARY_MAX],
        "markers": _clean_markers(candidate.get("markers")),
        "relevance": clean_relevance(candidate.get("relevance")),   # novelty vs the store (unknown → novel)
        "confidence": _clean_score(candidate.get("confidence"), 0.5),
        "producer": {"stage": "glean", "model": model, "prompt_version": PROMPT_VERSION,
                     "run_id": run_id, "cost_usd": None},
        "supersedes": None,
    }


# --- the event store (blobs, derived views) -------------------------------------------------


def load_events(root: Path | None = None) -> list[dict]:
    """The current event of every source — for each `event_id`, its LATEST version (ADR-0007 §4).
    `latest_by_kind('event')` already folds each source's version history to its newest snapshot, so
    re-extraction churn is absorbed by the TimeMap (latest wins) rather than surfacing as duplicate
    log lines; consumers no longer dedup by id. Lineage stays content-addressed via `cleaned_hash`."""
    return [json.loads(blobstore.get(h, root))
            for h in blobstore.latest_by_kind("event", root).values()]


def event_content(ev: dict) -> dict:
    """The STORED content of an event version — model output + intrinsic provenance pointers ONLY
    (ADR-0007 blob_shape). `producer` (model/run_id/cost_usd, all run-varying) is DROPPED here and
    moves to meta.origin_ref; `status` is gone (state is a decision). This projection is what makes
    "a re-extraction with unchanged output is a no-op" hold: the canonical-json of this view re-hashes
    identically run-to-run, so only a changed summary/markers/confidence forks a new version. `id`
    stays as a convenience mirror (it == source_id, deterministic, so it never perturbs the hash)."""
    return {
        "id": ev["id"],
        "cleaned_hash": ev["cleaned_hash"],
        "evidence": ev["evidence"],
        "summary": ev["summary"],
        "markers": ev["markers"],
        # `relevance` reads DEFENSIVELY: a pre-4b (glean/2) event blob lacks it → projects as `novel`, the
        # recall-safe default, so an old event never sinks in dream's queue on a missing field. A glean/3
        # re-extraction (the PROMPT_VERSION bump) re-stamps the real verdict; latest version wins.
        "relevance": clean_relevance(ev.get("relevance")),
        "confidence": ev["confidence"],
        "supersedes": ev.get("supersedes"),
    }


def _ingest_event(ev: dict, *, model: str, run_id: str, chunkset_hash: str,
                  root: Path | None) -> None:
    """Freeze one event as a RAW blob VERSION keyed on its span-derived `event_id` (ADR-0007). The
    content is `event_content(ev)` (run-invariant => byte-identical re-extraction no-ops); `producer`
    is mirrored into `origin_ref` so provenance is answerable from meta alone."""
    blobstore.ingest(
        blobstore.canonical_json(event_content(ev)), source_kind="event", source_id=ev["id"],
        origin_ref={"stage": "glean", "model": model, "prompt_version": PROMPT_VERSION,
                    "run_id": run_id, "cost_usd": ev["producer"].get("cost_usd"),
                    "cleaned_hash": ev["cleaned_hash"], "chunkset_hash": chunkset_hash},
        root=root)


# --- the Block: glean as a uniform PER-CHUNK stage (ADR-0009) -------------------------------

@dataclass
class ChunkItem:
    """One enumerated unit of work — a single chunk plus the chunkset it came from. The chunkset is
    carried only for the event's lineage `origin_ref` (which chunkset produced this event); the unit
    of persistence and idempotency is the CHUNK, not the chunkset (ADR-0009)."""
    chunk: chunk.Chunk
    chunkset_hash: str


def chunk_key(ch: chunk.Chunk) -> str:
    """A per-chunk deterministic id over the CHUNK boundary span, kept provably DISJOINT from
    event/takeaway source-ids by a `:chunk` suffix (events key on the evidence span — a sub-span of
    the chunk — so without the suffix a single-turn chunk whose sole evidence is the whole chunk would
    collide). The chunkset pins the spans, so two runs over the same frozen chunkset produce the same
    chunk keys → idempotency is exact. The done-key is `(chunk_key, PROMPT_VERSION, model)`."""
    return hashlib.sha256(
        f"{ch.cleaned_hash}:{ch.byte_start}:{ch.byte_end}:chunk".encode()).hexdigest()[:16]


class GleanBlock:
    """glean as a `block.Block` — the per-chunk LLM extract stage (ADR-0009). `items()` enumerates the
    target chunksets exactly as the old `run` did, then EXPLODES each into its chunks; `process()`
    extracts ONE chunk (one LLM call) and ingests its event blobs; the driver writes a `processed`
    marker per chunk — including a filter-skipped chunk (0 events) — so the done-set is exact.

    Idempotency keys on (chunk_key, PROMPT_VERSION, model). Per-chunk commit makes Ctrl-C cheap: a
    kill mid-backfill keeps every committed chunk and re-does only the in-flight one. A raised
    completer is isolated by the driver (counts `errored`, writes NO marker, retried next run).

    The run-total audit fields the old `RunReport` exposed (events/rejected/calls/errored) accumulate
    on the INSTANCE — the uniform `block.Report` stays stage-agnostic; the shim and tests read the
    rich tallies here."""

    name = "glean"
    commits_per_item = True
    finalize = block.no_finalize

    def __init__(self, complete: Completer, *, model: str = completer.DEFAULT_MODEL,
                 targets: list[str] | None = None, topic: str | None = None) -> None:
        self.complete = complete
        self.model = model
        self._targets = targets   # explicit chunkset list (the bare-hash / shim path); else by source_id
        self.topic = topic        # PROCESSING FOCUS: extract only this project's chunks (ADR-0022)
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROMPT_VERSION), ("model", model))
        # run-total tallies (instance-scoped; the Report stays uniform)
        self.events = 0           # events ingested this run (== block.Report.outputs)
        self.rejected = 0         # candidates whose quote failed verification
        self.calls = 0            # LLM calls made (signal-bearing chunks; filter-skips don't call)
        # The concept digest seam (4b/ADR-0019): the run-invariant facet pass is built ONCE (first signal
        # chunk) and re-rendered per chunk with that chunk's `relevant_to`, so the O(concepts) raw re-parse
        # is paid once per run, not once per chunk; `_facet_cache` shares each cleaned blob's facets across
        # chunks. `None` until built — a glean run with zero signal chunks never touches concepts.
        self._digest_ctx: dict | None = None
        self._facet_cache: dict = {}
        # The digest-disable latch (4b/ADR-0019): a persistent build failure (a bug in `digest_context`/
        # `chunk_facets`/`concept_digest`) sets this on the FIRST exception, so every later chunk
        # short-circuits to the empty sentinel WITHOUT re-attempting the expensive build (no retry-storm).
        # Surfaced ONCE in the run summary — a permanent novelty-disable must be detectable, not silent.
        self._digest_failed = False
        # per-chunk audit recorded for marker_extra (set in process, read by the driver immediately after)
        self._last: dict = {}
        # the Aging seam (ADR-0021): `age` needs the run's root + a recency read. `_root` is captured in
        # items() (the driver's root, set before ordering calls age); `_age_cache` memoizes each cleaned
        # blob's `fetched_at` so the many chunks SHARING one cleaned_hash pay a single meta read, not one
        # each. Only the Aging strategy calls age() — Greedy ignores it, so a non-aging run touches neither.
        self._root: Path | None = None
        self._age_cache: dict[str, str | None] = {}

    # -- enumeration: target chunksets → their chunks ----------------------------------------

    def _target_chunksets(self, root: Path, source_id: str | None) -> list[str]:
        if self._targets is not None:
            return self._targets
        if source_id is not None:
            cs = chunkset_for_source(source_id, root)
            return [cs] if cs else []
        return all_chunksets(root)

    def items(self, root: Path, *, source_id: str | None = None):
        """Enumerate every chunk of every target chunkset. The chunkset is the CONTAINER; the chunk is
        the item. `chunk.load` returns the chunk pointers even if the cleaned blob is TTL-gone — that
        absence surfaces in `process` when it slices the cleaned bytes (→ FileNotFoundError → errored
        → retried), not here, so a missing blob is isolated per chunk, never a crashed enumeration."""
        self._root = root                         # capture the driver's root for age() (called during ordering)
        # PROCESSING FOCUS (`glean --topic`, ADR-0022): keep only chunks whose source PROJECT contains the
        # topic substring (case-insensitive), reached by the same `cleaned_hash` → raw `origin_ref.project`
        # hop age() walks — one cached meta read per cleaned blob (chunks share a cleaned_hash), no LLM. A
        # chunk whose project can't be resolved does NOT match (focus narrows). Default (None) → no filter.
        topic = self.topic.lower() if self.topic else None
        proj_cache: dict = {}
        for cs in self._target_chunksets(root, source_id):
            try:
                chunks = chunk.load(cs, root)
            except FileNotFoundError:
                continue                          # the chunkset blob itself is gone — nothing to enumerate
            for ch in chunks:
                if topic is not None:
                    proj = blobstore.project_of(ch.cleaned_hash, root, proj_cache)
                    if not proj or topic not in proj.lower():
                        continue
                yield ChunkItem(chunk=ch, chunkset_hash=cs)

    def key(self, item: ChunkItem) -> str:
        return chunk_key(item.chunk)

    def priority(self, item: ChunkItem) -> float:
        """Pre-LLM salience for the amortized queue (ADR-0010 §8): order chunks by LIKELY durable yield
        so a `--limit`/`--max-usd`-capped tick gleans the richest chunks FIRST and the backlog drains
        best-first. Uses ONLY the chunk POINTER's free structural cues — `kinds` (speaker kinds present)
        and the turn span — so it costs NO content read: prioritizing must not re-introduce the very
        per-tick O(bytes) scan amortization is meant to avoid. Signals, strongest first: a USER turn is
        present (where the human steers / corrects / states a preference — the gold source of durable
        learnings), speaker-kind diversity (a real exchange beats a tool-output monologue), and a small
        interaction-count term. Deliberately NOT byte length — a long tool dump is bytes-heavy but
        low-yield. Recall-first: this only ORDERS, never gates; a richer content-salience hint computed
        once at chunk-time is the deferred upgrade."""
        ch = item.chunk
        kinds = set(ch.kinds or [])
        has_user = 2.0 if "user" in kinds else 0.0
        diversity = 0.5 * len(kinds)
        interaction = 0.1 * min(ch.turn_end - ch.turn_start, 10)
        return has_user + diversity + interaction

    def age(self, item: ChunkItem) -> float:
        """The chunk's AGE in DAYS for the Aging policy (ADR-0021): `now() - fetched_at` of the chunk's
        source CLEANED blob — how long this transcript material has waited un-gleaned. glean is budget-gated
        (LLM + `--max-usd`), so under a months-long backlog Greedy's lowest-yield chunks could starve
        forever; aging lets an old chunk's `score + λ·age` eventually overtake fresher richer ones (bounded
        latency). CHEAP — one cached meta read (no content slice, no LLM), so it keeps the amortized-queue
        O(1)-per-item promise: chunks share a cleaned_hash, so `_age_cache` reads each blob's stamp once.
        Degrades to 0.0 ("fresh") if the blob/stamp is gone or unparseable — never raises (a missing recency
        must not crash ordering). Only Aging calls this; Greedy ignores age, so glean stays byte-identical."""
        ch = item.chunk.cleaned_hash
        if ch not in self._age_cache:
            try:
                self._age_cache[ch] = blobstore.get_meta(ch, self._root).get("fetched_at")
            except (OSError, json.JSONDecodeError):
                self._age_cache[ch] = None        # absent/unreadable meta → treat as fresh (0.0), never raise
        return config.age_days(self._age_cache[ch])

    # -- the concept digest seam: "what we already know", provenance-relevant to THIS chunk --------

    def _relevant_digest(self, cleaned_hash: str, root: Path) -> str:
        """The bounded, structured "what we already know" render (ADR-0018) ORDERED to THIS chunk: the
        concepts most facet-overlapping the chunk's provenance survive the digest's budget cut, so a
        near-duplicate of a thin, single-session concept is judged `known`, not falsely `novel` (4b/ADR-
        0019). The expensive facet pass (`digest_context`) is cached on the instance — built once per run,
        re-rendered per chunk with the chunk's `relevant_to`.

        ADVISORY + RECALL-FIRST: this only shapes the model's relevance verdict, never the trust anchor, so
        any failure to BUILD the context degrades to the empty sentinel rather than costing an extraction —
        the model then sees nothing known and defaults every event to `novel` (the recall-safe direction).
        The `concepts` import is FUNCTION-LOCAL: concepts→dream→glean would cycle on a top-level import."""
        from . import concepts                       # lazy: avoids the concepts→dream→glean import cycle
        if self._digest_failed:                      # a prior build raised → novelty is OFF for the run;
            return concepts.DIGEST_EMPTY             # don't re-attempt the expensive build every chunk
        try:
            if self._digest_ctx is None:
                self._digest_ctx = concepts.digest_context(root)
            facets = concepts.chunk_facets(cleaned_hash, root, cache=self._facet_cache)
            return concepts.concept_digest(root, relevant_to=facets, context=self._digest_ctx)
        except Exception:                            # advisory signal: never block extraction (recall-first;
            self._digest_failed = True               # KeyboardInterrupt/BaseException still propagate, ADR-
            return concepts.DIGEST_EMPTY             # 0009). Latch OFF + surface once — not silent forever.

    # -- extract ONE chunk -------------------------------------------------------------------

    def process(self, item: ChunkItem, *, root: Path, run_id: str) -> tuple[int, float]:
        """Extract one chunk → ingest its event blobs → return (n_events, call_cost). A filter-skipped
        chunk returns (0, 0.0) immediately — the driver still writes its 0-output marker, so the
        done-set stays exact and next run skips it with no LLM call (ADR-0009). The trust anchor is
        unchanged: every returned quote is verified against THIS chunk's own bytes before any event
        exists. A raised completer (or an absent cleaned blob) propagates — the driver isolates it as
        `errored`, writes no marker, and the chunk is retried next run (per-chunk retry, ADR-0009)."""
        ch = item.chunk
        cleaned_bytes = blobstore.get(ch.cleaned_hash, root).encode("utf-8")   # FileNotFoundError → errored
        text = cleaned_bytes[ch.byte_start:ch.byte_end].decode("utf-8")
        if not has_signal_potential(text):
            self._last = {"n_rejected": 0, "n_calls": 0, "cleaned_hash": ch.cleaned_hash}
            return (0, 0.0)                       # filter-skip: still marked done (0-output marker)

        known = self._relevant_digest(ch.cleaned_hash, root)   # "what we already know", relevant to THIS chunk;
        # the per-chunk digest build is glean's DELIBERATE advisory exception — a bug there is ISOLATED +
        # recall-first (swallowed → empty sentinel, latched off; see `_relevant_digest`), NOT surfaced.
        prompt = _user_prompt(text, structural_cues(text), known)  # our code; a bug HERE surfaces (driver errored)
        comp = self.complete(SYSTEM_PROMPT, prompt)           # the sole LLM call; raising → driver errored
        self.calls += 1
        call_cost = completer.cost_of(comp)

        accepted: list[dict] = []
        seen: set[str] = set()
        rejected = 0
        for cand in parse_candidates(comp.text):
            span = verify(cand, ch, cleaned_bytes)            # trust check first ...
            if span is None:
                rejected += 1
                continue
            ev = build_event(cand, ch, span, model=self.model, run_id=run_id)   # ... then the record
            if ev["id"] not in seen:              # same span twice in this chunk → one event
                seen.add(ev["id"])
                accepted.append(ev)
        share = round(call_cost / len(accepted), 8) if accepted else 0.0   # amortize over its events
        for ev in accepted:                       # event blobs committed durable first (the driver writes
            ev["producer"]["cost_usd"] = share    # the chunk's marker LAST, after this returns)
            _ingest_event(ev, model=self.model, run_id=run_id,
                          chunkset_hash=item.chunkset_hash, root=root)

        self.events += len(accepted)
        self.rejected += rejected
        self._last = {"n_events": len(accepted), "n_rejected": rejected, "n_calls": 1,
                      "cleaned_hash": ch.cleaned_hash}
        return (len(accepted), call_cost)

    def marker_extra(self, item: ChunkItem) -> dict:
        """The per-chunk audit fields for the marker body (n_rejected/n_calls/cleaned_hash for THIS
        chunk). The driver calls this right after `process`, so `self._last` is the just-processed
        chunk's tally."""
        return dict(self._last)


# --- run: a thin compat shim over the block driver (keeps dream/review setup untouched) -----

class _ShimReport(block.ProxyReport):
    """The shape the old `glean.run` returned — a thin WRAPPER, not a copy. The `block.ProxyReport` base
    holds the uniform `block.Report` the driver populated plus the GleanBlock instance and forwards every
    uniform field by reading THROUGH them (`@anti-desync`, the spec's #4 — no copy that can drift); this
    subclass adds only the genuinely-extra instance tallies (events/rejected), read off the block.
    dream/review call `glean.run([cs], fake, model='fake')` purely to populate the event store; they read
    `.events`/`.skipped`."""

    # the genuinely-extra instance tallies (the Report has no place for these) — read off the block
    @property
    def events(self) -> int:      # == self._report.outputs, but the block is the audit source of truth
        return self._blk.events
    @property
    def rejected(self) -> int:
        return self._blk.rejected


def run(chunkset_hashes: list[str], complete: Completer, *, model: str = completer.DEFAULT_MODEL,
        max_usd: float | None = None, limit: int | None = None, root: Path | None = None,
        priority: block.PriorityStrategy | None = None, progress=None) -> _ShimReport:
    """Compat shim: extract over the chunks of the given chunksets via the per-chunk `block.run`
    driver (ADR-0009). Builds a `GleanBlock` over an explicit chunkset list and returns a
    `_ShimReport` WRAPPING the uniform `block.Report` + the block's tallies — so existing callers
    (dream/review test setup, the old `glean.run([cs], fake, model=...)` shape) keep working with
    minimal change. Idempotency, per-chunk commit, error isolation, and the budget stop are the
    driver's (now at CHUNK granularity, not chunkset). `progress` defaults to None (silent) so a
    setup helper doesn't spew per-chunk lines — the caller injects a Progress to see them."""
    blk = GleanBlock(complete, model=model, targets=list(chunkset_hashes))
    report = block.run(blk, max_usd=max_usd, limit=limit, root=root, priority=priority, progress=progress)
    return _ShimReport(report, blk)


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
    ap.add_argument("--topic", help="PROCESSING FOCUS: extract only chunks from a PROJECT/source whose name "
                    "contains this substring (case-insensitive; semantic-tag topic is deferred)")
    ap.add_argument("--limit", type=int,
                    help="cap CHUNKS examined this run, before the done-skip (per-chunk now, ADR-0009)")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this (between chunks)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list chunks that would be processed (skips done); no LLM calls")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-chunk progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy over enumerated chunks (default: greedy = highest-yield first)")
    args = ap.parse_args(argv)

    # Resolve the target chunksets (the enumeration containers); items() explodes them into chunks.
    if args.all:
        targets: list[str] = all_chunksets()
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

    complete = completer.make_cli_completer(args.model)
    blk = GleanBlock(complete, model=args.model, targets=targets, topic=args.topic)
    # the stage owns its Progress now (the driver only speaks the protocol). None when there is nothing
    # to watch (--quiet or --dry-run); else built from this stage's args + OUT_NOUN.
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, cap=args.max_usd, params=dict(blk.params), out_noun=OUT_NOUN, verbose=args.verbose)
    report = block.run(blk, max_usd=args.max_usd, limit=args.limit, dry_run=args.dry_run,
                       priority=block.priority_strategy(args.priority), progress=progress)

    if args.dry_run:
        print(f"\nglean-{report.run_id}: {report.would_process} chunk(s) would process "
              f"({report.skipped} already done for {PROMPT_VERSION}/{args.model}).")
        return
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\nglean-{report.run_id}: {report.examined} examined, {report.processed} done, "
          f"{report.skipped} skipped, {report.outputs} events, {blk.rejected} rejected{errs}, "
          f"${report.cost_usd:.4f}{tail}")
    if blk._digest_failed:                           # the digest-disable latch tripped — surface it ONCE so a
        print("  WARNING: concept digest build failed — novelty awareness was DISABLED this run "  # permanent
              "(every event judged `novel`); see GleanBlock._relevant_digest. This is glean's deliberate "  # off
              "advisory exception (isolated + recall-first), not a crash.")                          # is visible


if __name__ == "__main__":
    main()
