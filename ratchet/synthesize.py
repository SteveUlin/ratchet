"""synthesize — dream v3's deferred prose stage (design doc `dream-v3-design-2026-07-01.md` §7.3/§2.1,
ADR-0028 §8): fill durable `title`/`why` prose on claims that have MATURED.

    … resolve → claims (title = event summary, why = null) → SYNTHESIZE → prose → [review] …

resolve mints every claim with a provisional title and `why = null` — cheap, immediate, $0 or one
Haiku residue call. Prose is Sonnet, so it is DEFERRED: this Block fires only for a claim whose
recency-weighted net entrenchment has CROSSED the maturity bar (or on explicit review demand,
`--claim`), never per-new-event. The earlier "within one session of the bar" trigger is rejected for
the same reason as per-event synthesis: on a cold corpus nearly every fresh seed sits within one
session of the bar, re-creating Sonnet-per-event. Matured-only bounds the queue by the GRADUATION
rate, not the new-event rate — an all-NEW cold-start corpus pays Sonnet for nothing.

  ONE CALL PER CLAIM — system = the matured-claim variant of dream's SYNTH_SYSTEM; user = the claim's
    derived evidence quotes (folded from LIVE corroborates edges, spans re-validated at the fold's
    read boundary) + `concepts.concept_digest` as the trusted prior. The verdict {title, why,
    relation, confidence, drop} parses through dream's coercion idioms (`_clip`, `_clean_relation`,
    `clean_score`; unparseable/drop/empty-why → no version, never garbage prose).

  A NEW CLAIM VERSION, NEVER AN EDGE — the blob keeps seed identity ({id, seed_event, born}) and
    gains the prose (title improved, why filled) plus `why_fingerprint`: the live corroborates-edge-
    set fingerprint the prose consumed (`resolve.corro_fingerprint`). Support, scope, and signature
    stay pure folds over edges this stage never touches; the `why` is the ONE stored non-derived
    field, so the fingerprint is how the fold DETECTS staleness (`why_stale`) when the live edge set
    later diverges — a retraction after synthesis must not leave fused prose latched (§7.3).

  IDEMPOTENCY — the done-marker keys on (claim_id, prompt_version, model), so decay / re-crossing the
    bar / a drop verdict never re-pays the call. Re-synthesis is review-demand ONLY: `--claim` selects
    one claim regardless of the bar AND versions the done-key with a per-run `demand` param, so the
    marker never matches — an explicit demand always pays, the automatic path never re-pays.

  BUDGET — every item here is a paid Sonnet call (no $0 work to starve), so `--max-usd` goes to the
    DRIVER's plain break-on-budget: the tick stops cleanly, committed-so-far persists, the un-paid
    tail (no marker) retries next tick. resolve's deferral machinery would be dead weight.

`synthesize` is a `block.Block` (ADR-0009): per-claim commit is the claim version inside process(),
the driver's `processed` marker last. The LLM seam is one injected `Completer` (Sonnet),
offline-testable with fakes.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import blobstore, block, completer, config, dream, resolve
from .completer import Completer

PROMPT_VERSION = "synth/1"     # bump to re-synthesize NOT-yet-prosed claims with a sharper prompt
SYNTH_MODEL = "sonnet"         # prose is rare (bounded by the graduation rate) → afford the sharper model
OUT_NOUN = "claims"


# --- the prompt: dream's SYNTH_SYSTEM, re-derived for a MATURED multi-quote claim (§7.3) -----------

SYNTH_SYSTEM = (
    "You write the durable prose for ONE MATURED CLAIM in a developer's long-term memory. The claim "
    "was seeded from a Claude Code session and has since RECURRED; you are shown every verbatim "
    "EVIDENCE quote that corroborates it (the ground truth — trust the quotes) and its provisional "
    "title (a one-line machine summary that may be imprecise or wrong).\n\n"
    "State the ONE underlying lesson all the quotes teach and WHY it holds — do not just restate a "
    "quote, and never generalize beyond what the quotes support: if they show a specific tool, "
    "command, or workflow, name it.\n\n"
    "You are also given the developer's already-known CONCEPTS. Judge how this claim relates: new "
    "(nothing covers it), strengthens (more evidence for one), refines (narrows/extends one), or "
    "contradicts (overturns one — the most important to surface).\n\n"
    "Return ONLY a JSON object, no prose, no code fences:\n"
    '{\n'
    '  "title": a short noun phrase naming the claim (<= 80 chars),\n'
    '  "why": one or two sentences — the durable principle and why it holds (<= 280 chars),\n'
    '  "relation": {"kind": "new"|"strengthens"|"refines"|"contradicts", "concept_id": the related '
    'concept id or null, "note": a brief reason (<= 160 chars)},\n'
    '  "confidence": 0-1, how durable and reusable this is,\n'
    '  "drop": true if the evidence is noise, not a durable learning (then everything else is ignored)\n'
    '}'
)


def _synth_user(claim: dict, concept_digest: str, quotes: list[dict]) -> str:
    sup = claim.get("support") or {}
    lines = [f'[{i}] """{str(e.get("quote", ""))}"""' for i, e in enumerate(quotes, 1)]
    return (f"Known concepts:\n{concept_digest}\n\n"
            f"CLAIM (recurred across {sup.get('sessions', 0)} distinct sessions, "
            f"scope: {claim.get('scope', 'local')})\n"
            f"provisional title: {str(claim.get('title', '')).strip()!r}\n\n"
            f"EVIDENCE — verbatim quotes, one per corroborating event:\n" + "\n".join(lines))


def synthesize_claim(claim: dict, complete_synth: Completer, concept_digest: str, *,
                     known_concept_ids: set[str], model: str, run_id: str,
                     root: Path | None) -> tuple[dict | None, float]:
    """ONE Sonnet call fills a matured claim's prose (§7.3) and mints the new claim VERSION (same id,
    why filled, title improved, `why_fingerprint` stamped) — edges are never touched; every evidential
    attribute keeps deriving from the fold. Returns (content, cost); (None, cost) when the model
    declined (drop / unparseable / no usable why) — a successful adjudication that the evidence carries
    no durable prose: NO version is minted, the claim keeps its provisional title (review shows it with
    a "why pending" badge, §6), and the driver's marker stops the automatic path from re-paying —
    `--claim` re-demands it. The quotes were re-validated at the fold's read boundary
    (`dream._resolve_event` / `validate_span`); a claim with NO surviving quote raises — Sonnet must
    never write prose without verbatim ground truth (the event blobs are gone; retried, not marked)."""
    quotes = [e for e in (claim.get("evidence") or []) if str(e.get("quote", "")).strip()]
    if not quotes:
        raise ValueError(f"claim {claim.get('id')!r}: no verifiable evidence quotes to synthesize from")
    comp = complete_synth(SYNTH_SYSTEM, _synth_user(claim, concept_digest, quotes))
    cost = completer.cost_of(comp)
    parsed = completer.parse_json_object(comp.text) or {}
    if not parsed or parsed.get("drop"):
        return None, cost
    why = dream._clip(str(parsed.get("why", "")))
    if not why:                                    # no usable principle → decline, never garbage prose
        return None, cost
    content = {
        "id": claim["id"],
        "title": str(parsed.get("title", "")).strip()[:dream.TITLE_MAX] or str(claim.get("title", "")),
        "why": why,
        "relation": dream._clean_relation(parsed.get("relation"), known_concept_ids),
        "seed_event": claim.get("seed_event"),
        "born": claim.get("born"),
        # the edge set this prose consumed (§7.3): the fold recomputes it from live edges and flags
        # why_stale on divergence — the one stored non-derived field gets a staleness detector.
        "why_fingerprint": resolve.corro_fingerprint(claim["id"], claim.get("cites") or []),
    }
    blobstore.ingest(blobstore.canonical_json(content), source_kind=resolve.CLAIM_KIND,
                     source_id=content["id"],
                     origin_ref={"stage": "synthesize", "model": model,
                                 "prompt_version": PROMPT_VERSION, "run_id": run_id,
                                 "confidence": completer.clean_score(parsed.get("confidence"), 0.5),
                                 "cost_usd": round(cost, 8)},
                     root=root)
    return content, cost


# --- the Block: matured why-null claims, one paid call each, driver-standard everything ------------

class SynthesizeBlock:
    """The deferred prose stage as a `block.Block` (ADR-0009). `items()` = the claims from
    `resolve.claim_pool` with `why == null` AND net entrenchment >= the maturity bar — the MATURED,
    just before review (§7.3; never "near-mature": that trigger is Sonnet-per-event on a cold corpus,
    so a fresh 1-session claim on a cold corpus enumerates NOTHING). `--claim` overrides the queue
    with one explicit claim, bar ignored, done-key demand-versioned (review demand always pays).
    `process(claim)` = one Sonnet call → the new claim version; the driver writes the marker LAST,
    keyed on (claim_id, prompt_version, model)."""

    name = "synthesize"
    commits_per_item = True
    marker_extra = block.no_marker_extra
    finalize = block.no_finalize                   # no cross-item pass; forget belongs to resolve
    age = block.no_age                             # the queue is graduation-bounded; starvation is moot

    def __init__(self, complete_synth: Completer, *, model: str = SYNTH_MODEL,
                 maturity: float = dream.MATURITY_WEIGHT, claim: str | None = None) -> None:
        self.complete_synth = complete_synth
        self.model = model
        self.maturity = maturity
        self.claim_id = claim
        self.params: tuple[tuple[str, str], ...] = (
            ("prompt_version", PROMPT_VERSION), ("model", model))
        if claim is not None:
            # --claim is explicit review demand (§6/§7.3): a per-run `demand` param versions the
            # done-key so no marker ever matches — re-synthesis always pays on demand, while the
            # markers it writes never shadow the automatic (prompt_version, model) path.
            self.params += (("demand", config.run_id()),)
        # on-instance state, built once in items() — never a source of truth.
        self._digest: str = ""
        self._known_ids: set[str] = set()
        self._net: dict[str, float] = {}
        # Report tallies.
        self.n_queue = 0
        self.n_filled = 0
        self.n_dropped = 0
        self.claims: list[dict] = []

    def items(self, root: Path, *, source_id: str | None = None):
        """Build the digest + the entrenchment map once, then yield the matured why-null queue (or the
        one demanded claim). `source_id` is ignored (synthesize is a global pass)."""
        from .concepts import concept_digest           # lazy: the dream↔concepts cycle guard (ADR-0018)
        self._digest = concept_digest(root)
        self._known_ids = dream.valid_concept_ids(root)
        now = config.now()
        valid_times = dream._session_valid_times(root)
        pool = resolve.claim_pool(root)
        self._net = {c["id"]: dream.net_entrenchment(c, now, valid_times=valid_times) for c in pool}
        if self.claim_id is not None:                  # review demand: the bar does not gate it
            picked = [c for c in pool if c["id"] == self.claim_id]
            if not picked:
                raise ValueError(f"no live claim {self.claim_id!r} in the pool")
            self.n_queue = 1
            return picked
        queue = [c for c in pool if c.get("why") is None and self._net[c["id"]] >= self.maturity]
        self.n_queue = len(queue)
        return queue

    def key(self, claim: dict) -> str:
        return claim["id"]

    def priority(self, claim: dict) -> float:
        """Most-entrenched first: the claim closest to review is the one whose prose is worth paying
        for first under a --limit/--max-usd slice."""
        return self._net.get(claim["id"], 0.0)

    def process(self, claim: dict, *, root: Path, run_id: str) -> tuple[int, float]:
        content, cost = synthesize_claim(claim, self.complete_synth, self._digest,
                                         known_concept_ids=self._known_ids, model=self.model,
                                         run_id=run_id, root=root)
        if content is None:
            self.n_dropped += 1                        # declined: marker still lands (never re-pays)
            return 0, cost
        self.n_filled += 1
        self.claims.append(content)
        return 1, cost


# --- run: the thin shim + Report wrapper (the resolve/dream pattern) --------------------------------

class SynthesizeReport(block.ProxyReport):
    """`synthesize.run`'s report — the uniform driver Report plus the prose tallies, read THROUGH the
    block instance (never copied)."""

    @property
    def n_queue(self) -> int:
        return self._blk.n_queue
    @property
    def n_filled(self) -> int:
        return self._blk.n_filled
    @property
    def n_dropped(self) -> int:
        return self._blk.n_dropped
    @property
    def claims(self) -> list[dict]:
        return self._blk.claims


def run(complete_synth: Completer, *, model: str = SYNTH_MODEL,
        maturity: float = dream.MATURITY_WEIGHT, claim: str | None = None,
        max_usd: float | None = None, limit: int | None = None,
        priority: block.PriorityStrategy | None = None,
        progress: block.Progress | None = None, root: Path | None = None) -> SynthesizeReport:
    """Fill prose on the matured why-null claims — a thin shim over `block.run(SynthesizeBlock(...))`.
    `max_usd` goes to the DRIVER's break-on-budget (every item is a paid Sonnet call; the un-paid tail
    carries no marker and retries next tick)."""
    blk = SynthesizeBlock(complete_synth, model=model, maturity=maturity, claim=claim)
    report = block.run(blk, max_usd=max_usd, limit=limit, root=root, priority=priority,
                       progress=progress)
    return SynthesizeReport(report, blk)


# --- CLI -------------------------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="synthesize",
        description="dream v3 deferred prose: ONE Sonnet call per MATURED why-null claim (never "
                    "per-event; the queue is bounded by the graduation rate). --claim re-synthesizes "
                    "on review demand, bypassing both the bar and the done-marker.")
    ap.add_argument("--model", default=SYNTH_MODEL, help=f"prose model (default: {SYNTH_MODEL})")
    ap.add_argument("--maturity", type=float, default=dream.MATURITY_WEIGHT,
                    help="recency-weighted net-entrenchment bar a claim must have crossed to be "
                         "synthesized (the single bar, dream.MATURITY_WEIGHT — the reviewer's knob, "
                         "ADR-0027)")
    ap.add_argument("--claim", help="explicit review demand: synthesize THIS claim id regardless of "
                    "the bar; the done-key gains a per-run demand param, so an existing marker never "
                    "blocks re-synthesis (the ONLY re-synthesis path)")
    ap.add_argument("--max-usd", type=float, help="stop the run before spend exceeds this "
                    "(committed-so-far persists; the unpaid tail retries next tick)")
    ap.add_argument("--limit", type=int, help="cap claims examined this run (the entrenchment-ordered top)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the matured why-null queue (or the demanded claim); no LLM calls, no writes")
    ap.add_argument("--show", action="store_true", help="print each synthesized claim's title + why")
    ap.add_argument("--quiet", action="store_true", help="suppress the streaming per-claim progress line")
    ap.add_argument("--verbose", action="store_true", help="also log one idempotent line per item")
    ap.add_argument("--priority", choices=sorted(block.PRIORITY_STRATEGIES), default="greedy",
                    help="ordering policy over the queue (default: greedy = most-entrenched first)")
    args = ap.parse_args(argv)

    if args.dry_run:                                   # eyeball the queue before spending Sonnet
        root = config.ensure_layout()
        blk = SynthesizeBlock(None, model=args.model, maturity=args.maturity, claim=args.claim)
        try:
            queue = list(blk.items(root))
        except ValueError as e:
            print(f"synthesize: {e}")
            return
        queue = block.priority_strategy(args.priority).order(queue, blk.priority, blk.age)
        what = f"claim {args.claim} (review demand)" if args.claim else \
               f"matured why-null claim(s) awaiting prose (bar {args.maturity})"
        print(f"{len(queue)} {what}:")
        for c in queue[:40]:
            print(f"  [{blk.priority(c):.2f}] {c['id'][:16]}  {str(c.get('title', ''))[:72]!r}")
        return

    complete_synth = completer.make_cli_completer(args.model)
    progress = None if args.quiet else block.Progress(
        "synthesize", cap=args.max_usd, params={"prompt_version": PROMPT_VERSION, "model": args.model},
        out_noun=OUT_NOUN, verbose=args.verbose)
    try:
        report = run(complete_synth, model=args.model, maturity=args.maturity, claim=args.claim,
                     max_usd=args.max_usd, limit=args.limit,
                     priority=block.priority_strategy(args.priority), progress=progress)
    except ValueError as e:                            # an unknown --claim id, said plainly
        print(f"synthesize: {e}")
        return
    if args.show:
        for c in report.claims:
            print(f"\n  • {c['title']}  [{c['relation']['kind']}]")
            print(f"    {c['why']}")
    tail = "  [stopped: budget]" if report.stopped_on_budget else ""
    errs = f", {report.errored} errored" if report.errored else ""
    print(f"\nsynthesize-{report.run_id}: {report.n_queue} queued → {report.n_filled} filled, "
          f"{report.n_dropped} dropped, {report.skipped} skipped{errs}, ${report.cost_usd:.4f}{tail}")


if __name__ == "__main__":
    main()
