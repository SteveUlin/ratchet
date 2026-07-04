"""glean — the first LLM stage: filter chunks for durable signal and extract verifiable
**events** (ADR-0004).

    tap → raw blob → weave → cleaned blob → chunk → chunkset → glean → events
                                                          (extract, LLM)

An event is a THIN POINTER into the cleaned blob, not a copy of its text: a byte span
(`evidence`) whose verbatim text is `get(cleaned_hash)[span]` — TRUSTED — plus a one-sentence
model `summary` — UNTRUSTED. The trust anchor (the whole point, ADR-0026): the chunk is shown to
the model with NUMBERED lines, and the model returns the line numbers its evidence lives on; glean
copies those lines' bytes from the immutable blob. The model never reproduces transcript text, so
evidence cannot be hallucinated — it is verbatim by construction, not by checking a retyped quote.

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
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import blobstore, block, chunk, completer, config, sig, subject, weave
from .completer import Completer  # the LLM seam (Completion + the default binding) lives in `completer`

PROMPT_VERSION = "glean/4"     # bump to re-extract over the same frozen chunks (idempotency key);
                               # glean/4: the model POINTS at numbered lines, it no longer retypes a quote
                               # (ADR-0026) — evidence is copy-pasted from the blob, never transcribed
SUMMARY_MAX = 240              # untrusted-field hygiene: cap the model's summary
OUT_NOUN = "events"            # the per-item output noun the Progress bar/line shows (glean emits events)

# The VALID-TIME recency term of priority(): a BONUS of `W_RECENT · 0.5^(material_age_days /
# RECENT_HALF_LIFE_DAYS)` added to the structural score, where material age counts from when the
# session ACTUALLY HAPPENED — the raw transcript's `origin_ref.mtime` (the valid-time clock,
# ADR-0023 / dream._session_valid_times), NOT `fetched_at` (when ratchet ingested it). The split is
# the whole point under a backfill: hundreds of transcripts ARRIVE in one week, so arrival-time is a
# flat cohort with nothing to order by, while their session dates spread over months — valid-time is
# the only clock that can say "mine the owner's recent life first", so the concept layer reflects NOW
# sooner instead of spending the first budget-capped ticks on archaeology. It COMPOSES with the Aging
# term rather than fighting it: recency reads when the material HAPPENED (a bonus that decays as the
# session recedes), aging reads how long it has WAITED in the queue (a boost that grows) — so recent
# sessions drain first today, and anti-starvation still guarantees the old tail surfaces eventually.
# Escape hatch: W_RECENT = 0 restores the pure-structural score, bit-for-bit.
#
# WHICH CLOCK dates the material — `RECENT_CLOCK`, sulin's knob (2026-07-03):
#   "valid-then-arrival" (default) — the conversation's own date (`origin_ref.mtime`) when it exists,
#       else FALL BACK to `fetched_at` (when tap pulled it). The fallback exists for source kinds with
#       no conversation clock at all: a PDF or fetched webpage (the future researcher pre-tap source)
#       enters the owner's life when it is PULLED — arrival IS its honest date. Transcripts virtually
#       always carry mtime (measured: 0 undated of 252 real sessions), so the fallback is rare there
#       and its one distortion (old conversation + fresh arrival → full bonus) correspondingly rare.
#   "valid"   — strict: no conversation date, no bonus (unknown never outranks known-recent).
#   "arrival" — the tap date only (what a pure fetched-material corpus would want).
# Per-source-kind clock mapping is the natural upgrade when non-transcript sources land (backlog).
RECENT_CLOCK = "valid-then-arrival"   # --recent-clock overrides per run
RECENT_CLOCKS = ("valid-then-arrival", "valid", "arrival")
W_RECENT = 1.0                  # UNTUNED — the full-freshness bonus, sized against the ~0.5-4.0
                                # structural envelope (1.0 ≈ half a "user turn present"); a defensible
                                # default, not a fitted one — wants a gold set
RECENT_HALF_LIFE_DAYS = 60.0    # UNTUNED — a two-month-old session carries half the bonus; the scale
                                # of "current life" vs archaeology, chosen to spread a months-wide
                                # backfill, not fitted

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
    "nothing durable — that is expected; an empty list is the right answer far more often than not, "
    "so prefer it over inventing signal.\n\n"
    "The excerpt is shown with a line number on every line (`12| ...`). You do NOT retype the "
    "transcript — you POINT at it by line number, and the system copies those exact bytes as the "
    "evidence. This is deliberate: copied bytes are always faithful, whereas anything you retype could "
    "drift. So your task is to pick the tightest line range that carries each learning, and describe it.\n\n"
    "For each learning, return:\n"
    "- \"lines\": {\"from\": N, \"to\": M} — the inclusive line numbers (read from the `N|` prefixes) "
    "whose text IS the evidence. A single line is {\"from\": N, \"to\": N}. Choose the smallest range "
    "that still stands on its own as evidence for the learning.\n"
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
    "  When the catalog is empty or you are unsure, answer \"novel\". The reason is a cost asymmetry: a "
    "real learning wrongly marked \"known\" sinks out of sight, while a duplicate marked \"novel\" is "
    "cheaply de-duplicated later — so doubt should resolve toward \"novel\".\n\n"
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


def _user_prompt(numbered_excerpt: str, cues: list[str], known: str | None = None) -> str:
    hint = ("\n\nStructural cues (weigh, do not over-trust): " + "; ".join(cues)) if cues else ""
    # The concept digest ("what we already know") rides BELOW the excerpt so the model judges each event's
    # `relevance` against it (4b/ADR-0019). It is PROVENANCE-RELEVANT to this chunk (ordered by facet
    # overlap), so the concepts most likely to already cover it survive the digest's budget cut.
    known_block = (f"\n\nWHAT WE ALREADY KNOW (judge each learning's relevance against this):\n{known}"
                   if known else "")
    # The excerpt arrives already line-numbered (number_lines): the model selects line ranges, never text.
    return f"Excerpt (each line is numbered — cite lines, do not copy text):\n{numbered_excerpt}{hint}{known_block}"


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


def number_lines(cleaned_bytes: bytes, ch: chunk.Chunk) -> tuple[str, list[tuple[int, int]]]:
    """Present the chunk as 1-based numbered lines for the model to POINT at, and return the parallel
    map (line N → `(byte_start, byte_end)` IN THE CLEANED BLOB). The model selects line ranges and the
    system copies those exact bytes, so the model never reproduces transcript text — evidence is
    verbatim by construction, not by a trust check on a retyped quote (ADR-0026).

    Lines split on `\\n`; a line's span EXCLUDES its trailing newline (so adjacent lines don't overlap
    and a single-line selection is exactly that line's text). Offsets are absolute in the cleaned blob,
    computed in BYTES (a multibyte char makes byte≠char offsets), so the returned span resolves with a
    plain `cleaned_bytes[start:end]`. Decoding for DISPLAY tolerates a split multibyte char at a chunk
    edge (`errors="replace"`); the stored span is the real bytes regardless."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = ch.byte_start
    for i, raw in enumerate(cleaned_bytes[ch.byte_start:ch.byte_end].split(b"\n"), start=1):
        start, end = pos, pos + len(raw)
        spans.append((start, end))
        parts.append(f"{i}| " + raw.decode("utf-8", "replace"))
        pos = end + 1                                   # +1 for the '\n' that split consumed
    return "\n".join(parts), spans


def resolve_lines(candidate: dict, line_spans: list[tuple[int, int]],
                  cleaned_bytes: bytes) -> tuple[int, int] | None:
    """The trust anchor, reshaped (ADR-0026): map a candidate's LINE selection to a `[start, end)` byte
    span in the cleaned blob. The model points; we copy. Tolerant by design (recall-first) — the model
    slipping a line number should cost coarseness, never the whole learning:
      - accept `{"from": N, "to": M}`, a bare int, or a `[i, j]` list; a missing `to` means a single line;
      - swap a reversed range; CLAMP out-of-range numbers into the chunk (a near-miss still yields real,
        nearby evidence);
      - only a selection we cannot read AT ALL (no `lines`, non-numeric, empty chunk) returns None.
    A resolved span that is empty or all-whitespace also returns None — zero-signal evidence, the line-era
    analogue of the old too-short-quote reject."""
    if not line_spans:
        return None
    sel = candidate.get("lines")
    if isinstance(sel, dict):
        lo, hi = sel.get("from"), sel.get("to", sel.get("from"))
    elif isinstance(sel, int):
        lo = hi = sel
    elif isinstance(sel, (list, tuple)) and sel:
        lo, hi = sel[0], sel[-1]
    else:
        return None
    try:
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        return None
    if lo > hi:
        lo, hi = hi, lo
    n = len(line_spans)
    lo, hi = max(1, min(lo, n)), max(1, min(hi, n))     # clamp into the chunk's line range
    bstart, bend = line_spans[lo - 1][0], line_spans[hi - 1][1]
    if bend <= bstart or not cleaned_bytes[bstart:bend].strip():
        return None                                     # empty / whitespace-only selection → no signal
    return bstart, bend


def build_event(candidate: dict, ch: chunk.Chunk, span: tuple[int, int], *,
                model: str, run_id: str, root: Path | None = None,
                subject_cache: dict | None = None) -> dict:
    """Assemble the event record for a verified span — the event FORMAT lives here (ADR-0004/0005). An
    event is a thin pointer (the span, never the quote text) + the untrusted, hygiene-cleaned model
    fields (summary, markers, confidence) + intrinsic provenance. It stays a plain dict, deliberately:
    an event is a JSON serialization boundary (now a blob's content) the downstream judge reads as
    JSON, not an in-memory value type like `Chunk` (ADR-0004).

    ADR-0007 reshapes the record: `id` is now the blob's `source_id` (authoritative in meta), kept
    here only as a convenience content mirror; `status` is DROPPED (state is a decision, never an
    in-record field). `producer` stays in content (the cost-amortization target) and is mirrored into
    `origin_ref` at ingest so "who/when/how produced this version" is answerable from meta alone.

    dream-v3 §2.1 (S1) stamps two DETERMINISTIC identity features here — no extra LLM call:
    `subject_key` (WHERE the lesson lives: repo + files co-located with the FIRST evidence span,
    `subject.subject_key`) and `stmt_sig` (the char-shingle signature of the STORED summary,
    `sig.stmt_sig`). resolve reads them off the blob; a pre-stamp event lacks them and resolve
    computes-on-read instead. `subject_cache` shares the per-cleaned-blob subject parse across a
    batch (the GleanBlock instance owns one, the `_facet_cache` idiom)."""
    bstart, bend = span
    summary = str(candidate.get("summary", "")).strip()[:SUMMARY_MAX]
    return {
        "id": event_id(ch.cleaned_hash, bstart, bend),
        "cleaned_hash": ch.cleaned_hash,
        "evidence": [{"byte_start": bstart, "byte_end": bend}],
        "summary": summary,
        "subject_key": subject.subject_key(root or config.data_root(), ch.cleaned_hash,
                                           (bstart, bend), subject_cache),
        "stmt_sig": sig.stmt_sig(summary),
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
        # The dream-v3 §2.1 stamps (S1) ride the projection ONLY when present: a pre-stamp blob lacks
        # them and must re-hash BYTE-IDENTICALLY (no spurious version on a fold or no-op re-ingest) —
        # resolve computes-on-read for those. Stamped events keep them verbatim; both are deterministic
        # functions of span + summary, so they never perturb re-extraction idempotency.
        **{k: ev[k] for k in ("subject_key", "stmt_sig") if k in ev},
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
    parallel_safe = True      # each chunk is INDEPENDENT — nothing one chunk writes changes what a
                              # concurrent chunk reads (events are content-addressed per span; no
                              # read-your-writes, unlike resolve), so `block.run(parallel=N)` may
                              # overlap this block's LLM calls. The shared instance state is audited
                              # for that below: tallies take a lock, caches stay lock-free by design.
    finalize = block.no_finalize

    def __init__(self, complete: Completer, *, model: str = completer.DEFAULT_MODEL,
                 targets: list[str] | None = None, topic: str | None = None,
                 recent_clock: str = RECENT_CLOCK) -> None:
        self.complete = complete
        self.model = model
        self._targets = targets   # explicit chunkset list (the bare-hash / shim path); else by source_id
        self.topic = topic        # PROCESSING FOCUS: extract only this project's chunks (ADR-0022)
        if recent_clock not in RECENT_CLOCKS:
            raise ValueError(f"recent_clock must be one of {RECENT_CLOCKS}, got {recent_clock!r}")
        self.recent_clock = recent_clock   # which stamp dates the material (see the RECENT_CLOCK knob)
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROMPT_VERSION), ("model", model))
        # run-total tallies (instance-scoped; the Report stays uniform). `_tally_lock` guards them
        # under `--parallel`: `+=` on a shared int is a read-modify-write, so a thread switch between
        # the load and the store LOSES an increment — WRONG audit data, hence the lock. (The caches
        # below are the opposite case and stay lock-free: a race there only duplicates an idempotent,
        # content-keyed derivation — CPython dict get/set are atomic and the value is deterministic.)
        self._tally_lock = threading.Lock()
        self.events = 0           # events ingested this run (== block.Report.outputs)
        self.rejected = 0         # candidates whose quote failed verification
        self.calls = 0            # LLM calls made (signal-bearing chunks; filter-skips don't call)
        # The concept digest seam (4b/ADR-0019): the run-invariant facet pass is built ONCE (first signal
        # chunk) and re-rendered per chunk with that chunk's `relevant_to`, so the O(concepts) raw re-parse
        # is paid once per run, not once per chunk; `_facet_cache` shares each cleaned blob's facets across
        # chunks. `None` until built — a glean run with zero signal chunks never touches concepts.
        self._digest_ctx: dict | None = None
        self._facet_cache: dict = {}
        # LOCK-FREE by design under `--parallel` (this and every cache below: _facet_cache, _age_cache,
        # _vt_cache, _subject_cache, and the lazy _digest_ctx/_digest_failed latch): each memoizes a DETERMINISTIC
        # derivation keyed by content hash, so the worst a race can do is build the same value twice —
        # duplicated work, idempotent and content-addressed, never wrong data. A lock would buy nothing.
        # The digest-disable latch (4b/ADR-0019): a persistent build failure (a bug in `digest_context`/
        # `chunk_facets`/`concept_digest`) sets this on the FIRST exception, so every later chunk
        # short-circuits to the empty sentinel WITHOUT re-attempting the expensive build (no retry-storm).
        # Surfaced ONCE in the run summary — a permanent novelty-disable must be detectable, not silent.
        self._digest_failed = False
        # per-chunk audit recorded for marker_extra — THREAD-LOCAL, the one shared slot where a race
        # would produce WRONG data (not merely duplicated work): under `--parallel` a plain dict here
        # could hold whichever chunk finished LAST, mis-attributing another chunk's tallies into a
        # marker. The driver reads marker_extra in the SAME thread that ran process (serial: the main
        # loop; parallel: the worker, see block._run_pool), so a thread-local is exact in both lanes.
        self._last = threading.local()
        # the TWO-CLOCK stamp seam (ADR-0021 aging + the valid-time recency term): `_root` is captured
        # in items() (the driver's root, set before ordering calls priority/age); the caches memoize the
        # RAW transcript's stamps behind each cleaned blob — `_age_cache` its `fetched_at` (TRANSACTION
        # time: when the material arrived, age()'s wait-clock) and `_vt_cache` its `origin_ref.mtime`
        # (VALID time: when the session happened, priority()'s recency clock, ADR-0023). Both live on
        # the same raw meta, so ONE `_fill_stamps` hop (cleaned → derived_from → raw, two meta reads)
        # fills both per cleaned blob: the many chunks SHARING one cleaned_hash pay the read pair once,
        # and an Aging run pays nothing beyond what priority() already paid.
        self._root: Path | None = None
        self._age_cache: dict[str, str | None] = {}   # cleaned_hash → raw fetched_at (None = unknown)
        self._vt_cache: dict[str, str | None] = {}    # cleaned_hash → raw origin_ref.mtime (None = undated)
        # The subject-stamp seam (dream-v3 §2.1, S1): `subject_key` derivation parses the cleaned blob
        # once (meta hop + write-line scan, maybe a raw re-parse) — shared across the many events of one
        # session's chunks, keyed by cleaned_hash. The `_facet_cache`/`_age_cache` idiom.
        self._subject_cache: dict = {}

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
        once at chunk-time is the deferred upgrade.

        A VALID-TIME recency bonus rides on top of the structural score (see W_RECENT):
        `W_RECENT · 0.5^(material_age / RECENT_HALF_LIFE_DAYS)`, where material age counts from the raw
        transcript's `origin_ref.mtime` — when the session HAPPENED, not when it arrived. The structural
        cues are date-blind, so under a backfill (every transcript sharing one arrival week) hundreds of
        chunks tie at the structural ceiling and the tick's pick among them is arbitrary; the session
        dates spread over months, and the bonus breaks the tie toward the owner's RECENT life — current
        lessons reach review first, the concept layer reflects NOW sooner. It composes with the Aging
        term: recency decays as the session recedes (happened-recently drains first), aging grows as the
        queue-wait lengthens (the old tail is guaranteed to surface) — the two clocks deliberately pull
        at different items. Costs two meta reads per cleaned BLOB (shared by its chunks via `_vt_cache`;
        still never a content read). WHICH stamp dates the material is the RECENT_CLOCK policy
        (default: the conversation's own date, arrival as the fallback for conversation-less sources);
        an item with no usable stamp earns NO bonus rather than the age-0 maximum (see below).
        W_RECENT = 0 restores the pure-structural score."""
        ch = item.chunk
        kinds = set(ch.kinds or [])
        has_user = 2.0 if "user" in kinds else 0.0
        # `compact` is EXCLUDED from diversity — measured, not guessed (2026-07-04, the 317-chunk
        # glean/4 cohort): compact-segment chunks yielded HALF the events of their score-mates with
        # 20% zeros, because a compaction summary is a RETELLING of already-compressed material —
        # the durable lessons live in the original exchanges, and rewarding the kind's presence
        # actively mis-ranked (score 4.5 compact chunks under-yielded 4.0 plain ones, Spearman −0.32).
        diversity = 0.5 * len(kinds - {"compact"})
        interaction = 0.1 * min(ch.turn_end - ch.turn_start, 10)
        if ch.cleaned_hash not in self._vt_cache:
            self._fill_stamps(ch.cleaned_hash)
        # Pick the material's date by the RECENT_CLOCK policy: the conversation's own date when the
        # source has one, else (default policy) the ARRIVAL stamp — a PDF/webpage-style source has no
        # conversation clock, and "when it entered the owner's life" is its honest date (sulin,
        # 2026-07-03). An item with NO usable stamp under the policy earns NO bonus (0.0), never the
        # age-0 maximum: unknown must not outrank known-recent. This deliberately INVERTS
        # `config.age_days`'s missing-stamp degrade (0.0 = "treat as fresh") — safe for age(), where
        # 0.0 merely WITHHOLDS the anti-starvation boost, but here age-0 would GRANT the full bonus.
        if self.recent_clock == "arrival":
            stamp = self._age_cache[ch.cleaned_hash]
        elif self.recent_clock == "valid":
            stamp = self._vt_cache[ch.cleaned_hash]
        else:                                          # "valid-then-arrival" (default)
            stamp = self._vt_cache[ch.cleaned_hash] or self._age_cache[ch.cleaned_hash]
        recency = W_RECENT * 0.5 ** (config.age_days(stamp) / RECENT_HALF_LIFE_DAYS) if stamp else 0.0
        return has_user + diversity + interaction + recency

    def _fill_stamps(self, cleaned_hash: str) -> None:
        """Fill BOTH stamp caches for one cleaned blob in ONE lineage hop. The cleaned blob is a render —
        derived meta carries `created_at` (resettable by any re-render), never its source's stamps — so
        both clocks hop cleaned → `derived_from` → raw meta (recompute-on-read, ADR-0013; the same hop
        `session_of`/`project_of` walk) and read the raw transcript's `fetched_at` (transaction time,
        age()'s clock) and `origin_ref.mtime` (valid time, priority()'s clock) off the SAME meta — two
        reads fill two caches. Degrades to None on absent/unreadable lineage, never raises (a missing
        stamp must not crash ordering). An mtime that does not PARSE normalizes to None here: downstream
        None means "undated → no bonus", whereas letting `config.age_days` degrade it (0.0 = fresh)
        would award the MAXIMUM bonus to an unreadable date — the wrong direction (see priority())."""
        fetched = mtime = None
        try:
            raw = blobstore.get_meta(cleaned_hash, self._root).get("derived_from")
            if raw:
                m = blobstore.get_meta(raw, self._root)
                fetched = m.get("fetched_at")
                mtime = (m.get("origin_ref") or {}).get("mtime")
        except (OSError, json.JSONDecodeError):
            pass                              # absent/unreadable lineage → both stamps unknown, never raise
        if mtime is not None:
            try:
                datetime.fromisoformat(str(mtime))
            except (TypeError, ValueError):
                mtime = None                  # an unparseable date IS an unknown date — no bonus
        self._age_cache[cleaned_hash] = fetched
        self._vt_cache[cleaned_hash] = mtime

    def age(self, item: ChunkItem) -> float:
        """The chunk's AGE in DAYS for the Aging policy (ADR-0021): `now() - fetched_at` of the RAW
        transcript BEHIND the chunk's cleaned blob — how long this material has waited un-gleaned. The
        cleaned blob is a render, so its age is its SOURCE's age: derived meta carries `created_at`
        (when the render ran — resettable by any re-render), never `fetched_at` (when the material
        arrived), so age reads the raw's stamp through the `_fill_stamps` lineage hop. Reading the
        cleaned meta directly finds no stamp on any weave-derived blob and silently flattens every age
        to 0.0, never firing aging's anti-starvation term.

        glean is budget-gated (LLM + `--max-usd`), so under a months-long backlog Greedy's lowest-yield
        chunks could starve forever; aging lets an old chunk's `score + λ·age` eventually overtake
        fresher richer ones (bounded latency). CHEAP — two meta reads, cached per cleaned_hash (no
        content slice, no LLM), so it keeps the amortized-queue O(1)-per-item promise: chunks share a
        cleaned_hash, so `_age_cache` pays the hop once per blob (and priority()'s valid-time read rides
        the same hop — an Aging run pays no extra reads over a Greedy one). Degrades to 0.0 ("fresh")
        when the lineage or stamp is gone/unparseable — never raises (a missing recency must not crash
        ordering). Only Aging calls this; Greedy ignores age. NOTE the deliberate asymmetry with
        priority()'s recency term: a missing stamp here reads as FRESH (0.0 merely withholds the aging
        boost), there as UNDATED (no bonus) — each is the conservative direction for its own term."""
        ch = item.chunk.cleaned_hash
        if ch not in self._age_cache:
            self._fill_stamps(ch)
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
        done-set stays exact and next run skips it with no LLM call (ADR-0009). The trust anchor: the
        model returns LINE NUMBERS into this chunk and the system copies those lines' bytes (ADR-0026),
        so evidence is verbatim by construction — the model never reproduces transcript text. A raised
        completer (or an absent cleaned blob) propagates — the driver isolates it as
        `errored`, writes no marker, and the chunk is retried next run (per-chunk retry, ADR-0009)."""
        ch = item.chunk
        cleaned_bytes = blobstore.get(ch.cleaned_hash, root).encode("utf-8")   # FileNotFoundError → errored
        text = cleaned_bytes[ch.byte_start:ch.byte_end].decode("utf-8")
        if not has_signal_potential(text):
            self._last.d = {"n_rejected": 0, "n_calls": 0, "cleaned_hash": ch.cleaned_hash}
            return (0, 0.0)                       # filter-skip: still marked done (0-output marker)

        known = self._relevant_digest(ch.cleaned_hash, root)   # "what we already know", relevant to THIS chunk;
        # the per-chunk digest build is glean's DELIBERATE advisory exception — a bug there is ISOLATED +
        # recall-first (swallowed → empty sentinel, latched off; see `_relevant_digest`), NOT surfaced.
        numbered, line_spans = number_lines(cleaned_bytes, ch)  # the model points at these lines; we copy bytes
        prompt = _user_prompt(numbered, structural_cues(text), known)  # our code; a bug HERE surfaces (driver errored)
        comp = self.complete(SYSTEM_PROMPT, prompt)           # the sole LLM call; raising → driver errored
        with self._tally_lock:                    # shared int += — a lost increment is wrong audit data
            self.calls += 1
        call_cost = completer.cost_of(comp)

        accepted: list[dict] = []
        seen: set[str] = set()
        rejected = 0
        for cand in parse_candidates(comp.text):
            span = resolve_lines(cand, line_spans, cleaned_bytes)   # line selection → cleaned-blob byte span
            if span is None:
                rejected += 1
                continue
            ev = build_event(cand, ch, span, model=self.model, run_id=run_id,   # ... then the record
                             root=root, subject_cache=self._subject_cache)      # (stamps ride along, §2.1)
            if ev["id"] not in seen:              # same span twice in this chunk → one event
                seen.add(ev["id"])
                accepted.append(ev)
        share = round(call_cost / len(accepted), 8) if accepted else 0.0   # amortize over its events
        for ev in accepted:                       # event blobs committed durable first (the driver writes
            ev["producer"]["cost_usd"] = share    # the chunk's marker LAST, after this returns)
            _ingest_event(ev, model=self.model, run_id=run_id,
                          chunkset_hash=item.chunkset_hash, root=root)

        with self._tally_lock:                    # see __init__: the tallies lock, the caches don't
            self.events += len(accepted)
            self.rejected += rejected
        self._last.d = {"n_events": len(accepted), "n_rejected": rejected, "n_calls": 1,
                        "cleaned_hash": ch.cleaned_hash}
        return (len(accepted), call_cost)

    def marker_extra(self, item: ChunkItem) -> dict:
        """The per-chunk audit fields for the marker body (n_rejected/n_calls/cleaned_hash for THIS
        chunk). The driver calls this right after `process` IN THE SAME THREAD (serial: the main loop;
        parallel: the worker — block._run_pool), so the thread-local `_last` is the just-processed
        chunk's tally, never a concurrent chunk's."""
        return dict(getattr(self._last, "d", {}))


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
    ap.add_argument("--parallel", type=int, default=1, metavar="N",
                    help=f"concurrent LLM calls, capped at {block.PARALLEL_MAX} (default 1 = serial). "
                         "2-3 when stepping away from the keyboard; shares your interactive token "
                         "budget, so it buys latency, not capacity")
    ap.add_argument("--breaker-errors", type=int, default=block.BREAKER_ERRORS, metavar="K",
                    help=f"abort the tick after K CONSECUTIVE chunk failures — an unbroken run means a "
                         f"systemic wall (usage window / auth / network), not K flaky chunks; aborted "
                         f"chunks stay pending (default {block.BREAKER_ERRORS}; 0 disables)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list chunks that would be processed (skips done); no LLM calls")
    ap.add_argument("--scores", action="store_true",
                    help="read-only: the pending queue's priority-score distribution (stats + "
                         "histogram + top/bottom items — what a capped tick buys); composes with "
                         "--priority/--topic/--source-id; no LLM calls, no writes")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-chunk progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy over enumerated chunks (default: greedy = highest-yield first)")
    ap.add_argument("--recent-clock", choices=RECENT_CLOCKS, default=RECENT_CLOCK,
                    help="which stamp dates material for the recency bonus: the conversation's own date "
                         "with arrival as fallback (default — conversation-less sources like fetched "
                         "PDFs/pages are dated by when they were pulled), strict 'valid' (no date, no "
                         "bonus), or 'arrival' (tap date only)")
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

    if args.scores:                               # read-only early-return, --dry-run's sibling: the
        blk = GleanBlock(completer.make_cli_completer(args.model), model=args.model,   # completer is
                         targets=targets, topic=args.topic,          # bound but never called (no LLM)
                         recent_clock=args.recent_clock)
        print(block.scores_report(blk, root=config.ensure_layout(), priority=args.priority))
        return

    complete = completer.make_cli_completer(args.model)
    blk = GleanBlock(complete, model=args.model, targets=targets, topic=args.topic,
                     recent_clock=args.recent_clock)
    # the stage owns its Progress now (the driver only speaks the protocol). None when there is nothing
    # to watch (--quiet or --dry-run); else built from this stage's args + OUT_NOUN.
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, cap=args.max_usd, params=dict(blk.params), out_noun=OUT_NOUN, verbose=args.verbose)
    report = block.run(blk, max_usd=args.max_usd, limit=args.limit, dry_run=args.dry_run,
                       priority=block.priority_strategy(args.priority), progress=progress,
                       parallel=args.parallel, breaker_errors=args.breaker_errors)

    if args.dry_run:
        print(f"\nglean-{report.run_id}: {report.would_process} chunk(s) would process "
              f"({report.skipped} already done for {PROMPT_VERSION}/{args.model}).")
        return
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    if report.breaker_tripped:
        tail += "  [stopped: breaker]"
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
