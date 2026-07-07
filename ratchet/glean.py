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

Events ARE blobs (ADR-0007): a logical event is a RAW blob whose `source_id` is the
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

DOCUMENT MODE (ADR-0031): a chunk from a `document` chunkset (tap `--file` → weave's passthrough
render) is a curated rules file the owner WROTE, not a session — most of it IS durable signal. The
mode swaps the system prompt (`DOC_SYSTEM_PROMPT`: extract each rule, restate it) and relaxes the
speaker-tag pre-filter; the trust anchor is UNCHANGED — the model still points at numbered lines
and the system copies bytes (ADR-0026). Mode is read off the chunkset SIDECAR (no content read),
and the done-key carries `DOC_PROMPT_VERSION` so the two extractions version independently.
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

PROMPT_VERSION = "glean/5"     # bump to re-extract over the same frozen chunks (idempotency key).
                               # glean/5 (ADR-0036): BLIND extraction — the concept digest ("what we already
                               # know") and the per-event `relevance` judgment are GONE from the prompt. glean
                               # is now a pure function of the chunk; novelty-vs-the-store lives solely in
                               # resolve (ADR-0028), which USES a re-occurrence to corroborate rather than
                               # sinking it. The bump RE-OPENS chunks for OPTIONAL blind re-glean — forward-only,
                               # budgeted, the glean/2→3→4 precedent; the digest-contaminated glean/4 events
                               # stay valid until a re-glean, and re-gleaning blind may RECOVER events the
                               # digest once suppressed as `known`. (glean/4: pointed at lines, ADR-0026 —
                               # still true; that trust anchor is unchanged.)
DOC_PROMPT_VERSION = "glean-doc/2"   # DOCUMENT mode's own version knob (ADR-0031); glean-doc/2 is the BLIND
                               # doc prompt (ADR-0036 dropped the digest/relevance clause here too). It rides in the
                               # done-KEY (GleanBlock.key), not params — params is a run-level constant
                               # and one --all run mixes modes. Bumping it re-extracts documents only.
                               # Known asymmetry, accepted: bumping PROMPT_VERSION (in params) re-keys
                               # doc chunks too; their re-extraction re-ingests byte-identically, so a
                               # transcript prompt bump wastes a few LLM calls on the (small) document
                               # corpus, never duplicates data.
REF_PROMPT_VERSION = "glean-ref/1"   # EXTERNAL-REFERENCE mode's version (ADR-0037): a fetched page
                               # (tap --url) extracts under REF_SYSTEM_PROMPT, keyed independently of the
                               # owner-authored glean-doc/2 — so a --url doc and a --file doc never share
                               # a done-key, and the reference prompt versions on its own.
SUMMARY_MAX = 240              # untrusted-field hygiene: cap the model's summary. Deliberately ABOVE
                               # the prompts' "<= 200 chars" ask — tolerance for model overshoot: the
                               # prompt asks, the hygiene cap forgives (a mild run-on keeps its tail)
OUT_NOUN = "events"            # the per-item output noun the Progress bar/line shows (glean emits events)

# The VALID-TIME recency term of priority(): a BONUS of `W_RECENT · 0.5^(material_age_days /
# RECENT_HALF_LIFE_DAYS)` added to the structural score, where material age counts from when the
# session ACTUALLY HAPPENED — the raw transcript's `origin_ref.mtime` (the valid-time clock,
# ADR-0023 / temporal.session_valid_times), NOT `fetched_at` (when ratchet ingested it). The split is
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

# NOVELTY-vs-the-store is NOT glean's job (ADR-0036). glean once judged each event's `relevance`
# (novel/known/contradicts) against an injected concept digest and fed it into dream's salience order.
# That broke corroboration: a lesson recurring in a DISTINCT session — exactly what matures a claim —
# was marked `known`, sank in the queue, and under any budget cap never reached resolve. Novelty now
# lives in ONE place, resolve's statement-first matching (ADR-0028), which USES a re-occurrence to
# corroborate rather than suppress it. glean extracts BLIND — a pure function of the chunk.

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
    "- \"confidence\": a number 0-1, how durable and reusable this is.\n\n"
    "Output ONLY a JSON object: {\"events\": [ ... ]}. No prose, no code fences. If the excerpt "
    "holds nothing durable, output {\"events\": []}."
)

# The DOCUMENT-mode variant (ADR-0031): same JSON contract, same pointing discipline (ADR-0026),
# inverted prior. A transcript excerpt is mostly noise (the empty list is the expected answer); a
# curated rules document the owner WROTE is mostly signal — each stated rule/directive/preference
# is one event, and the summary RESTATES the rule rather than inferring a lesson from behavior.
DOC_SYSTEM_PROMPT = (
    "You extract the durable rules from an excerpt of a curated rules/notes document its owner "
    "WROTE (a CLAUDE.md, a preferences file, working notes), for a system that folds the owner's "
    "written configuration into its reviewed knowledge layer.\n\n"
    "Unlike a session transcript, this document is already distilled: expect MOST of it to be "
    "deliberate, durable directives. Extract each distinct rule, directive, or stated preference "
    "as one event; skip only headings, decorative structure, and prose that states no rule.\n\n"
    "The excerpt is shown with a line number on every line (`12| ...`). You do NOT retype the "
    "document — you POINT at it by line number, and the system copies those exact bytes as the "
    "evidence. Pick the tightest line range that carries each whole rule.\n\n"
    "For each rule, return:\n"
    "- \"lines\": {\"from\": N, \"to\": M} — the inclusive line numbers (read from the `N|` "
    "prefixes) whose text IS the rule. A single line is {\"from\": N, \"to\": N}.\n"
    "- \"summary\": one imperative sentence (<= 200 chars) that RESTATES the rule faithfully — "
    "restate, do not embellish, generalize, or merge neighboring rules.\n"
    "- \"markers\": an object scoring 0-1 on surprise / insight / research — usually all LOW for "
    "a stated rule (nothing broke an expectation; it is a directive, not a discovery).\n"
    "- \"confidence\": a number 0-1, how durable and actionable the rule is.\n\n"
    "Output ONLY a JSON object: {\"events\": [ ... ]}. No prose, no code fences. If the excerpt "
    "holds no rules (pure boilerplate), output {\"events\": []}."
)

# The EXTERNAL-REFERENCE variant (ADR-0037): a fetched page/PDF (tap --url) is NEITHER a noisy
# transcript NOR the owner's own rules — it is THIRD-PARTY material the user saved to learn from.
# So it inherits the document prompt's mostly-signal, restate-don't-infer posture (a curated article
# is dense), but drops the "rules its owner WROTE" framing that made the model REFUSE external docs
# ("this is public documentation, not the user's rules") — each event is a CLAIM THE SOURCE MAKES,
# not a directive the user issued. The authority distinction rides downstream: a source's claim
# proposes a `reference`-kind concept far more often than a `behavioral` directive (ADR-0029).
REF_SYSTEM_PROMPT = (
    "You extract the durable claims from an excerpt of an EXTERNAL REFERENCE document the user "
    "SAVED to learn from — an article, documentation page, or blog post they found valuable — for a "
    "system that folds reference material the user cares about into its reviewed knowledge layer.\n\n"
    "Like a curated notes file, this document is DISTILLED, so extract generously; but UNLIKE the "
    "user's own notes, it is THIRD-PARTY material — each event is a CLAIM THE SOURCE MAKES, never a "
    "directive the user issued. Extract each distinct durable claim, principle, finding, or "
    "recommendation as one event; skip headings, navigation, decorative structure, and prose that "
    "states nothing durable.\n\n"
    "The excerpt is shown with a line number on every line (`12| ...`). You do NOT retype the "
    "document — you POINT at it by line number, and the system copies those exact bytes as the "
    "evidence. Pick the tightest line range that carries each whole claim.\n\n"
    "For each claim, return:\n"
    "- \"lines\": {\"from\": N, \"to\": M} — the inclusive line numbers whose text IS the claim. A "
    "single line is {\"from\": N, \"to\": N}.\n"
    "- \"summary\": one sentence (<= 200 chars) stating the claim FAITHFULLY as the source makes it "
    "— state it, do not embellish, generalize, merge, or phrase it as the user's own rule.\n"
    "- \"markers\": an object scoring 0-1 on surprise / insight / research — a reference article "
    "often scores insight or research (substantive external knowledge); surprise is usually low.\n"
    "- \"confidence\": a number 0-1, how durable and reusable the claim is.\n\n"
    "Output ONLY a JSON object: {\"events\": [ ... ]}. No prose, no code fences. If the excerpt "
    "holds nothing durable (pure navigation/boilerplate), output {\"events\": []}."
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

def has_signal_potential(text: str, *, min_chars: int = 80, mode: str = "transcript") -> bool:
    """A cheap, conservative pre-filter — skip the LLM call for excerpts that *cannot* carry a
    durable learning (too small, or no human/assistant turn — pure tool noise or a lone compact
    marker). The model is the real filter; this only spares obvious junk an API call.

    DOCUMENT and REFERENCE modes have no speaker structure to require — a curated rules file or a
    fetched article is almost all signal — so only the size floor applies (ADR-0031/0037)."""
    if len(text) < min_chars:
        return False
    return mode in ("document", "reference") or "[user]" in text or "[assistant]" in text


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


def _user_prompt(numbered_excerpt: str, cues: list[str]) -> str:
    hint = ("\n\nStructural cues (weigh, do not over-trust): " + "; ".join(cues)) if cues else ""
    # The excerpt arrives already line-numbered (number_lines): the model selects line ranges, never text.
    # Blind extraction (ADR-0036): nothing about the concept layer rides here — glean is a pure function
    # of the chunk, and resolve owns novelty-vs-the-store.
    return f"Excerpt (each line is numbered — cite lines, do not copy text):\n{numbered_excerpt}{hint}"


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
                subject_cache: dict | None = None,
                prompt_version: str = PROMPT_VERSION) -> dict:
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
    batch (the GleanBlock instance owns one, the `_age_cache` idiom)."""
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
        "confidence": _clean_score(candidate.get("confidence"), 0.5),
        "producer": {"stage": "glean", "model": model, "prompt_version": prompt_version,
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
        # No `relevance` (ADR-0036): glean extracts blind, so new events carry no verdict. A pre-0036 blob
        # may still hold a stale `relevance` field — it is deliberately NOT projected here, so nothing reads
        # it and it never perturbs a fold. A blind re-glean (glean/5) forks a clean version, latest wins.
        "confidence": ev["confidence"],
        "supersedes": ev.get("supersedes"),
        # The dream-v3 §2.1 stamps (S1) ride the projection ONLY when present: a pre-stamp blob lacks
        # them and must re-hash BYTE-IDENTICALLY (no spurious version on a fold or no-op re-ingest) —
        # resolve computes-on-read for those. Stamped events keep them verbatim; both are deterministic
        # functions of span + summary, so they never perturb re-extraction idempotency.
        **{k: ev[k] for k in ("subject_key", "stmt_sig") if k in ev},
    }


def _ingest_event(ev: dict, *, model: str, run_id: str, chunkset_hash: str,
                  root: Path | None, prompt_version: str = PROMPT_VERSION) -> None:
    """Freeze one event as a RAW blob VERSION keyed on its span-derived `event_id` (ADR-0007). The
    content is `event_content(ev)` (run-invariant => byte-identical re-extraction no-ops); `producer`
    is mirrored into `origin_ref` so provenance is answerable from meta alone."""
    blobstore.ingest(
        blobstore.canonical_json(event_content(ev)), source_kind="event", source_id=ev["id"],
        origin_ref={"stage": "glean", "model": model, "prompt_version": prompt_version,
                    "run_id": run_id, "cost_usd": ev["producer"].get("cost_usd"),
                    "cleaned_hash": ev["cleaned_hash"], "chunkset_hash": chunkset_hash},
        root=root)


# --- the Block: glean as a uniform PER-CHUNK stage (ADR-0009) -------------------------------

@dataclass
class ChunkItem:
    """One enumerated unit of work — a single chunk plus the chunkset it came from. The chunkset is
    carried only for the event's lineage `origin_ref` (which chunkset produced this event); the unit
    of persistence and idempotency is the CHUNK, not the chunkset (ADR-0009). `mode` is the
    extraction variant (ADR-0031) — "document" for a chunk of a document chunkset, read once per
    chunkset off its SIDECAR in items(), so key()/process() never pay a lookup."""
    chunk: chunk.Chunk
    chunkset_hash: str
    mode: str = "transcript"


def chunk_key(ch: chunk.Chunk) -> str:
    """A per-chunk deterministic id over the CHUNK boundary span, kept provably DISJOINT from
    event/takeaway source-ids by a `:chunk` suffix (events key on the evidence span — a sub-span of
    the chunk — so without the suffix a single-turn chunk whose sole evidence is the whole chunk would
    collide). The chunkset pins the spans, so two runs over the same frozen chunkset produce the same
    chunk keys → idempotency is exact. The done-key is `(chunk_key, PROMPT_VERSION, model)`."""
    return hashlib.sha256(
        f"{ch.cleaned_hash}:{ch.byte_start}:{ch.byte_end}:chunk".encode()).hexdigest()[:16]


def structural_score(ch: chunk.Chunk) -> float:
    """The pointer-only, DATE-BLIND salience of a chunk — `has_user + diversity + interaction`, the
    part of `priority()` that is a pure function of the chunk BOUNDARY (never a content read, never a
    clock). `priority()` adds only the valid-time recency bonus on top of this. Recency is deliberately
    excluded HERE — it drifts with wall-clock, so a stored marker's chunk would re-bucket over time; the
    structural part is stable for the life of the frozen chunkset."""
    kinds = set(ch.kinds or [])
    has_user = 2.0 if "user" in kinds else 0.0
    # `compact` is EXCLUDED from diversity — measured, not guessed (2026-07-04, the 317-chunk glean/4
    # cohort): compact-segment chunks yielded HALF the events of their score-mates with 20% zeros,
    # because a compaction summary is a RETELLING of already-compressed material — the durable lessons
    # live in the original exchanges, and rewarding the kind's presence actively mis-ranked (score 4.5
    # compact chunks under-yielded 4.0 plain ones, Spearman −0.32).
    diversity = 0.5 * len(kinds - {"compact"})
    interaction = 0.1 * min(ch.turn_end - ch.turn_start, 10)
    return has_user + diversity + interaction


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
                 targets: list[str] | None = None, source_filter: str | None = None,
                 exclude: tuple[str, ...] = (), recent_clock: str = RECENT_CLOCK) -> None:
        self.complete = complete
        self.model = model
        self._targets = targets   # explicit chunkset list (the bare-hash / shim path); else by source_id
        self.source_filter = source_filter  # PROCESSING FOCUS: extract only this source's chunks (ADR-0022)
        self.exclude = tuple(exclude)       # JUNK QUARANTINE: skip chunks whose source handle contains any
                                            # listed substring (tap --exclude's vocabulary) — the include
                                            # filter's complement at the money gate; see items()
        if recent_clock not in RECENT_CLOCKS:
            raise ValueError(f"recent_clock must be one of {RECENT_CLOCKS}, got {recent_clock!r}")
        self.recent_clock = recent_clock   # which stamp dates the material (see the RECENT_CLOCK knob)
        self.params: tuple[tuple[str, str], ...] = (("prompt_version", PROMPT_VERSION), ("model", model))
        # run-total tallies (instance-scoped; the Report stays uniform). `_tally_lock` guards them
        # under `--parallel`: `+=` on a shared int is a read-modify-write, so a thread switch between
        # the load and the store LOSES an increment — WRONG audit data, hence the lock. (The caches
        # below are the opposite case and stay lock-free: a race there only duplicates an idempotent,
        # content-keyed derivation — CPython dict get/set are atomic and the value is deterministic.)
        self._tally_lock = threading.Lock()
        self.events = 0           # events ingested this run (== block.Report.outputs)
        self.rejected = 0         # candidates whose quote failed verification
        self.calls = 0            # per-CHUNK LLM calls made (signal chunks; filter-skips don't call)
        # PROMPT-CACHE instrumentation (R0/ADR-0035, survives ADR-0036): the run totals of input served
        # from a warm cache (`cache_read`) and written into it (`cache_creation`), summed across chunk
        # calls. They ride the per-chunk marker (marker_extra) and the run summary line so whether the
        # CLI's cross-invocation prompt cache actually fires stays MEASURED, never assumed.
        self.cache_read = 0
        self.cache_creation = 0
        # LOCK-FREE by design under `--parallel` (every cache below: _age_cache, _vt_cache, _meta_cache,
        # _subject_cache): each memoizes a DETERMINISTIC derivation keyed by content hash, so the worst a
        # race can do is build the same value twice — duplicated work, idempotent and content-addressed,
        # never wrong data. A lock would buy nothing.
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
        # the same raw meta, so ONE `_fill_stamps` hop (`blobstore.raw_meta_of`) fills both per cleaned
        # blob: the many chunks SHARING one cleaned_hash pay the hop once, and an Aging run pays
        # nothing beyond what priority() already paid.
        self._root: Path | None = None
        self._age_cache: dict[str, str | None] = {}   # cleaned_hash → raw fetched_at (None = unknown)
        self._vt_cache: dict[str, str | None] = {}    # cleaned_hash → raw origin_ref.mtime (None = undated)
        self._meta_cache: dict = {}                   # cleaned_hash → raw meta (`raw_meta_of`'s cache),
                                                      # SHARED by items()'s --source/--exclude handle reads
                                                      # and _fill_stamps — the lineage hop is paid once per
                                                      # cleaned blob across the filters AND both clocks
        # The subject-stamp seam (dream-v3 §2.1, S1): `subject_key` derivation parses the cleaned blob
        # once (meta hop + write-line scan, maybe a raw re-parse) — shared across the many events of one
        # session's chunks, keyed by cleaned_hash. The `_age_cache` idiom.
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
        # PROCESSING FOCUS (`glean --source`, ADR-0022) + JUNK QUARANTINE (`--exclude`): both read the
        # chunk's SOURCE handle (case-insensitive substring, the same `cleaned_hash` → raw handle hop
        # age() rides — one cached `raw_meta_of` read per cleaned blob, no LLM), composed include-FIRST
        # then exclude. The asymmetry on an unresolvable handle is deliberate: include FOCUSES (no
        # handle → no match → dropped), exclude drops only a POSITIVE match (no handle → kept) — each
        # filter acts only on what it can prove. Defaults (None / empty) → no filtering.
        source_filter = self.source_filter.lower() if self.source_filter else None
        exclude = tuple(x.lower() for x in self.exclude)
        for cs in self._target_chunksets(root, source_id):
            try:
                chunks = chunk.load(cs, root)
            except FileNotFoundError:
                continue                          # the chunkset blob itself is gone — nothing to enumerate
            # the extraction MODE (ADR-0031), one sidecar read per chunkset — never a content read.
            # An unreadable sidecar degrades to transcript mode (the recall-safe default: the strict
            # transcript prior under-extracts a document; it never fabricates).
            try:
                kind = blobstore.get_meta(cs, root).get("source_kind")
            except (OSError, json.JSONDecodeError):
                kind = None
            mode = "document" if kind == "document" else "transcript"
            # A document is OWNER-AUTHORED (--file: origin_ref carries `path`) or an EXTERNAL REFERENCE
            # (--url: origin_ref carries `url`, ADR-0033/0037) — one raw-meta hop per chunkset (all its
            # chunks share one cleaned blob → one origin), cached, disambiguates the extraction prompt.
            if mode == "document" and chunks:
                origin = (blobstore.raw_meta_of(chunks[0].cleaned_hash, root, self._meta_cache)
                          or {}).get("origin_ref") or {}
                if origin.get("url"):
                    mode = "reference"
            for ch in chunks:
                if source_filter is not None or exclude:
                    proj = blobstore.project_of(ch.cleaned_hash, root, self._meta_cache)
                    low = (proj or "").lower()
                    if source_filter is not None and (not proj or source_filter not in low):
                        continue
                    if proj and any(x in low for x in exclude):
                        continue
                yield ChunkItem(chunk=ch, chunkset_hash=cs, mode=mode)

    def key(self, item: ChunkItem) -> str:
        """The done-key target. A DOCUMENT/REFERENCE chunk's key carries its mode's PROMPT VERSION so
        the modes stay independently versioned — bumping the document (or reference) prompt re-extracts
        only that mode, never the transcript done-set (ADR-0031/0037). The chunk keys themselves can
        never collide across modes (a chunk belongs to exactly one cleaned blob, one source kind); the
        suffix gives each non-transcript extraction its own version knob."""
        k = chunk_key(item.chunk)
        if item.mode == "reference":
            return f"{k}:{REF_PROMPT_VERSION}"
        if item.mode == "document":
            return f"{k}:{DOC_PROMPT_VERSION}"
        return k

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
        return structural_score(ch) + recency

    def _fill_stamps(self, cleaned_hash: str) -> None:
        """Fill BOTH stamp caches for one cleaned blob in ONE lineage hop. The cleaned blob is a render —
        derived meta carries `created_at` (resettable by any re-render), never its source's stamps — so
        both clocks read the raw meta via `blobstore.raw_meta_of` (recompute-on-read, ADR-0013; the one
        single-sourced hop `session_of`/`project_of` are field reads of): the raw transcript's
        `fetched_at` (transaction time, age()'s clock) and `origin_ref.mtime` (valid time, priority()'s
        clock) come off the SAME dict — one hop fills two caches, and `_meta_cache` shares it with the
        --source/--exclude reads in items(). Degrades to None on absent/unreadable lineage, never raises
        (a missing stamp must not crash ordering). An mtime that does not PARSE normalizes to None here:
        downstream None means "undated → no bonus", whereas letting `config.age_days` degrade it (0.0 =
        fresh) would award the MAXIMUM bonus to an unreadable date — the wrong direction (see priority())."""
        m = blobstore.raw_meta_of(cleaned_hash, self._root, self._meta_cache) or {}
        fetched = m.get("fetched_at")
        mtime = (m.get("origin_ref") or {}).get("mtime")
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
        prose_mode = item.mode in ("document", "reference")   # curated text (no transcript speaker structure)
        cleaned_bytes = blobstore.get(ch.cleaned_hash, root).encode("utf-8")   # FileNotFoundError → errored
        text = cleaned_bytes[ch.byte_start:ch.byte_end].decode("utf-8")
        if not has_signal_potential(text, mode=item.mode):
            self._last.d = {"n_rejected": 0, "n_calls": 0, "cleaned_hash": ch.cleaned_hash,
                            "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
            return (0, 0.0)                       # filter-skip: still marked done (0-output marker)

        numbered, line_spans = number_lines(cleaned_bytes, ch)  # the model points at these lines; we copy bytes
        # The extraction prompt per mode (ADR-0031/0037): owner-authored document, external reference, or
        # transcript. Curated prose (document/reference) takes NO structural cues — the cues are
        # transcript-shaped (speaker tags, tool errors) and would only misfire on an article. pv stamps
        # the event's PROVENANCE only (event_content drops it → re-extraction stays a no-op); each mode
        # versions independently so one mode's prompt bump re-extracts only its own corpus.
        cues = [] if prose_mode else structural_cues(text)
        if item.mode == "reference":
            system, pv = REF_SYSTEM_PROMPT, REF_PROMPT_VERSION
        elif item.mode == "document":
            system, pv = DOC_SYSTEM_PROMPT, DOC_PROMPT_VERSION
        else:
            system, pv = SYSTEM_PROMPT, PROMPT_VERSION
        prompt = _user_prompt(numbered, cues)     # BLIND (ADR-0036): the excerpt + cues alone, no digest
        comp = self.complete(system, prompt)      # the sole LLM call; raising → driver errored (per-chunk)
        with self._tally_lock:                    # shared int += — a lost increment is wrong audit data
            self.calls += 1
            self.cache_read += comp.cache_read_tokens         # cache instrumentation (R0): run totals, measured
            self.cache_creation += comp.cache_creation_tokens
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
                             root=root, subject_cache=self._subject_cache,      # (stamps ride along, §2.1)
                             prompt_version=pv)
            if ev["id"] not in seen:              # same span twice in this chunk → one event
                seen.add(ev["id"])
                accepted.append(ev)
        share = round(call_cost / len(accepted), 8) if accepted else 0.0   # amortize over its events
        for ev in accepted:                       # event blobs committed durable first (the driver writes
            ev["producer"]["cost_usd"] = share    # the chunk's marker LAST, after this returns)
            _ingest_event(ev, model=self.model, run_id=run_id,
                          chunkset_hash=item.chunkset_hash, root=root, prompt_version=pv)

        with self._tally_lock:                    # see __init__: the tallies lock, the caches don't
            self.events += len(accepted)
            self.rejected += rejected
        # the per-chunk marker records this call's token + cache figures (R0/ADR-0035) alongside the audit
        # counts, so whether the CLI prompt cache fired is reconstructable from the store, never assumed.
        # `input_tokens` is the NON-cached input (total input = input + cache_read + cache_creation).
        self._last.d = {"n_events": len(accepted), "n_rejected": rejected, "n_calls": 1,
                        "cleaned_hash": ch.cleaned_hash,
                        "input_tokens": comp.input_tokens, "output_tokens": comp.output_tokens,
                        "cache_read": comp.cache_read_tokens, "cache_creation": comp.cache_creation_tokens}
        return (len(accepted), call_cost)

    def marker_extra(self, item: ChunkItem) -> dict:
        """The per-chunk audit fields for the marker body (n_rejected/n_calls/cleaned_hash for THIS
        chunk). The driver calls this right after `process` IN THE SAME THREAD (serial: the main loop;
        parallel: the worker — block._run_pool), so the thread-local `_last` is the just-processed
        chunk's tally, never a concurrent chunk's."""
        return dict(getattr(self._last, "d", {}))


# --- run: a thin compat shim over the block driver (keeps dream/review setup untouched) -----

class _ShimReport(block.ProxyReport):
    """The shape `glean.run` returns — the pre-block-driver report contract (ADR-0009), kept so its
    callers read one shape — a thin WRAPPER, not a copy. The `block.ProxyReport` base
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
    """Every materialized chunkset in the store (transcript AND document shapes, ADR-0031), by one
    scan over the derived sidecars."""
    return [m["content_hash"] for m in blobstore.iter_meta(root)
            if m.get("format") in chunk.CHUNKSET_FORMATS]


def chunkset_for_source(source_id: str, root: Path | None = None) -> str | None:
    """The chunkset of a logical source's latest snapshot: raw → cleaned → chunkset, all by sidecar
    scan (no re-fetch, no re-render). None if `chunk` hasn't materialized one yet."""
    raw = blobstore.latest_version(source_id, root)
    if not raw:
        return None
    cleaned = next((m for m in blobstore.derived_for(raw, root)
                    if m.get("format") in weave.RENDER_FORMATS), None)
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
    ap.add_argument("--source", help="PROCESSING FOCUS: extract only chunks whose SOURCE handle contains "
                    "this substring, case-insensitive — the originating project for transcripts (e.g. taro), "
                    "the file path for documents (e.g. CLAUDE.md). Exact single-source enumeration is "
                    "--source-id; a semantic TAG filter (garden vocabulary) is the deferred sibling knob")
    ap.add_argument("--exclude", action="append", default=[], metavar="SUBSTR",
                    help="skip chunks whose SOURCE handle contains this substring, case-insensitive "
                         "(repeatable; --source's vocabulary, tap --exclude's) — the include filter's "
                         "complement at the money gate. The append-only store has no retire-source, so "
                         "junk tapped before tap grew its self-skip/--exclude (ADR-0025), e.g. -tmp- "
                         "test fixtures, sits pending forever; this keeps it out of the LLM spend. "
                         "Composes with --source: include first, then exclude")
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
                         "--priority/--source/--source-id; no LLM calls, no writes")
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
                         targets=targets, source_filter=args.source,  # bound but never called (no LLM)
                         exclude=tuple(args.exclude), recent_clock=args.recent_clock)
        print(block.scores_report(blk, root=config.ensure_layout(), priority=args.priority))
        return

    complete = completer.make_cli_completer(args.model)
    blk = GleanBlock(complete, model=args.model, targets=targets, source_filter=args.source,
                     exclude=tuple(args.exclude), recent_clock=args.recent_clock)
    # the stage owns its Progress now (the driver only speaks the protocol). None when there is nothing
    # to watch (--quiet or --dry-run); else built from this stage's args + OUT_NOUN.
    progress = None if (args.quiet or args.dry_run) else block.Progress(
        blk.name, cap=args.max_usd, limit=args.limit, params=dict(blk.params), out_noun=OUT_NOUN,
        verbose=args.verbose)
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
    # the cache r/w figure (R0/ADR-0035): input tokens SERVED FROM / WRITTEN INTO the CLI's prompt cache
    # this run — whether cross-invocation caching actually fires, measured not assumed. Shown only when
    # there were chunk calls (a fully-skipped tick has nothing to report), so a re-run stays quiet.
    cache = f", cache r/w {blk.cache_read}/{blk.cache_creation}" if blk.calls else ""
    print(f"\nglean-{report.run_id}: {report.examined} examined, {report.processed} done, "
          f"{report.skipped} skipped, {report.outputs} events, {blk.rejected} rejected{errs}, "
          f"${report.cost_usd:.4f}{cache}{tail}")


if __name__ == "__main__":
    main()
