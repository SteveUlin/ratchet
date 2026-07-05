"""sig — the statement-signature layer for dream v3 (design §2.1/§3.1): pure, embedding-free
identity math over the LESSON TEXT, plus a READ-ONLY measuring CLI over the real store.

    glean → events (each gains a stmt_sig) → resolve (pairwise match cascade) → claims …

A statement's signature is three deterministic features of its normalized summary:

  simhash   — SimHash (Charikar) over CHARACTER 4-shingles. Char-shingles, not word n-grams: a
              ~10-word summary has almost no word 3-grams, and lexical-over-short-text sparsity is
              exactly what sank dream v1 (TF-IDF over short quotes, ADR-0010). Character windows
              keep signal on short strings; the simhash's 16-bit LSH bands give cheap candidate
              blocking (§3.1 step 1).
  shingles  — the char-shingle set itself, for EXACT Jaccard on the small candidate residue
              (§3.1 step 2). Blocking recalls; Jaccard decides the band.
  entropy   — Shannon entropy of the normalized summary, the triviality gate: a low-signal
              statement may SEED a claim but never MERGE into one (§3.1 step 0) — "ok" matching
              "ok" is not evidence of the same lesson.

The band cascade (`classify`, §3.1) is Fellegi–Sunter record linkage in miniature: deterministic
signals settle the clear cases at $0 (MATCH / NON-MATCH); only the possible-match residue goes to
an LLM, as a yes/no on ONE pair. Subject overlap is a SOFT scope signal — it lowers the statement
bar (J_HIGH vs J_CROSS), never vetoes (§3.4).

Honesty about limits: even char-shingles are lexical. The matcher is trusted only for the CLEAR
bands; genuine paraphrase lands in the residue and the LLM owns it. The thresholds below are
UNTUNED (design §Open-questions) — this module's CLI is the measuring instrument that earns them:
`--band-report` shows where the real corpus's pairs fall, `--sample-pairs` drafts a hand-label
file (stratified band × session-relation, noise-floor pairs excluded), `--score-gold` scores
threshold candidates against the labels, split by session-relation — the CROSS-session rows are
the headline, because post-gates that is the only place adjudication acts. The CLI never writes a
blob.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter
from pathlib import Path

from . import blobstore, config

SHINGLE_K = 4                 # char-shingle width (§2.1). 4 keeps signal on short summaries: wide
                              # enough that shared shingles mean shared wording, narrow enough that
                              # a ~100-char summary still yields ~100 of them.

# --- knobs: named, explained, CLI-overridable (the design-philosophy rule, ADR-0025/0026/0027).
# All four are UNTUNED initial guesses pending the band report + a hand-labeled gold set (design
# §Open-questions); every one is a --flag on this module's CLI so a candidate can be scored without
# an edit.
H_MIN = 3.0                   # UNTUNED — the entropy floor (bits/char-distribution) below which a
                              # statement may SEED a claim but never MERGE (§3.1 step 0). Degenerate
                              # statements ("ok", "fix bug") sit well under 3 bits — few distinct
                              # chars — while a real one-sentence lesson sits ~3.8–4.3; matching two
                              # low-signal strings is not evidence of the same lesson. Reviewer's
                              # knob: --h-min.
J_MAYBE = 0.12                # TUNED on the 60-pair hand-labeled gold set (2026-07-03,
                              # --score-gold) — the floor of the LLM-residue band. Below it a pair
                              # is NON-MATCH at $0. Set LOW (recall-first): the costly error is a
                              # same-lesson pair stranded in non-match — it silently seeds a
                              # duplicate claim nobody adjudicates — while a too-low floor merely
                              # buys visible Haiku calls (the band report projects exactly how
                              # many). The gold set showed 0.18 stranded 9/21 true matches, ALL
                              # scoring 0.142–0.170: real paraphrases live at 0.14–0.24, so the
                              # floor must sit below 0.14. 0.12 captures every labeled match with
                              # margin at ≤390 adjudications ceiling on the 805-event corpus
                              # (band report; the blocked real count is far lower). --j-maybe.
J_HIGH = 0.55                 # UNTUNED — the $0 deterministic MERGE bar when subjects overlap
                              # (subj > 0). High enough that only near-identical wording merges for
                              # free; paraphrase falls to the residue where the LLM decides with a
                              # no-merge default. Deliberately far above the ambiguity zone the
                              # band report exposed (the corpus's top pair, 0.31, is two DIFFERENT
                              # lessons sharing vocabulary — char-shingles cannot separate same
                              # from different at 0.2–0.35, so no $0 merge may live there). --j-high.
J_CROSS = 0.70                # UNTUNED — the stricter statement bar when subjects are DISJOINT
                              # (subj == 0): no shared scope corroborates the match, so the
                              # statement alone must carry identity (§3.1/§3.4). Must exceed J_HIGH
                              # — cross-subject sameness is weaker evidence of one lesson. --j-cross.

# Stratified-sampling weights for `--sample-pairs`: most of the label budget goes to the residue
# band (the open question is whether the LLM residue carries the bulk of identity work) and the
# rest to the band edges, where a threshold nudge flips verdicts. Named so a different budget split
# is one edit; the within-band edge-first rule lives in `stratified_sample`.
SAMPLE_WEIGHTS = {"residue": 0.5, "high": 0.2, "cross": 0.1, "non": 0.2}
CROSS_WEIGHT = 0.7            # the SECOND stratification axis: the fraction of each band's quota
                              # drawn from CROSS-session pairs. Post-gates the adjudicator only
                              # ever sees cross-session pairs (resolve's same-session gate skips
                              # the rest) and maturity counts DISTINCT sessions — so cross-session
                              # recall is the only recall that feeds maturity, and it earns the
                              # label majority. Same-session pairs keep the remainder: they never
                              # reach the LLM, but they exercise the gates themselves (dup
                              # fast-path, same-session skip). Round 1 ignored this axis on a
                              # single-project-era corpus and left the headline cross-session
                              # recall resting on n=4. Reviewer's knob: --cross-weight.


# --- pure signature math ----------------------------------------------------------------------

def normalize(text: str) -> str:
    """The one canonical text form every signature is computed over — deterministic and dumb by
    design: casefold, map every non-alphanumeric character (punctuation AND whitespace) to a
    space, collapse runs to single spaces, strip. Dumb is load-bearing: shingle sets get persisted
    on event blobs, so any cleverness here (stemming, stopwords) would fork signatures on its next
    tweak. Unicode-aware via str.casefold/isalnum, both stable stdlib."""
    folded = text.casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in folded).split())


def _shingle_hash(shingle: str) -> int:
    """One char-shingle → a 64-bit int via blake2b(digest_size=8). blake2b, NOT builtin hash():
    shingle sets get persisted on events later, so the hash function's stability across Python
    versions and processes is load-bearing — builtin hash() is salted per-process (PYTHONHASHSEED),
    so its values are meaningless the moment they touch disk."""
    return int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")


def char_shingles(text: str, k: int = SHINGLE_K) -> frozenset[int]:
    """The character k-shingle set of the NORMALIZED text, each shingle hashed to a 64-bit int.
    A normalized text shorter than k still signs — the whole text is its one shingle — so a tiny
    statement gets a signature rather than a silent empty set (the entropy gate, not an empty set,
    is what keeps it from merging). Empty text → the empty set."""
    norm = normalize(text)
    if not norm:
        return frozenset()
    if len(norm) < k:
        return frozenset((_shingle_hash(norm),))
    return frozenset(_shingle_hash(norm[i:i + k]) for i in range(len(norm) - k + 1))


def simhash(shingles: frozenset[int]) -> int:
    """Charikar SimHash over the 64-bit shingle hashes: per bit position, count set-vs-unset
    across the set; the result bit is 1 where set wins (strictly — a tie or empty set yields 0,
    deterministic). Near-identical shingle sets land at small Hamming distance, which is what the
    LSH bands block on. Empty set → 0."""
    if not shingles:
        return 0
    counts = [0] * 64
    for h in shingles:
        for bit in range(64):
            counts[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(64):
        if counts[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit simhashes — the LSH-side similarity."""
    return (a ^ b).bit_count()


def jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    """Exact Jaccard over two shingle sets — the band cascade's stmt_sim (§3.1 step 2). Two EMPTY
    sets → 0.0, not 1.0: an absent statement is evidence of nothing, and 0.0 is the no-merge-safe
    direction (mirrors the entropy gate's discipline)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def entropy(text: str) -> float:
    """Shannon entropy in bits over the CHARACTERS of the normalized text — the triviality-gate
    signal (§3.1 step 0). Short/degenerate statements have few distinct characters → low entropy;
    a real one-sentence lesson lands ~4 bits. Empty → 0.0."""
    norm = normalize(text)
    if not norm:
        return 0.0
    n = len(norm)
    return -sum((c / n) * math.log2(c / n) for c in Counter(norm).values())


def lsh_bands(sh: int) -> tuple[int, int, int, int]:
    """Split a 64-bit simhash into 4 × 16-bit bands (most-significant band first) — the candidate
    BLOCKING key (§3.1 step 1): two statements are LSH-candidates iff they collide in ANY band,
    i.e. their simhashes agree on 16 contiguous bits. Recall-first blocking: 4×16 tolerates up to
    3 stray bits per non-colliding band, catching near-dups without indexing every pair."""
    return tuple((sh >> shift) & 0xFFFF for shift in (48, 32, 16, 0))


def stmt_sig(summary: str) -> dict:
    """The persisted statement signature of one event summary (§2.1) — JSON-serializable, stamped
    at glean time by S1 of the design. `shingles` is sorted so the blob's canonical-json is stable
    (a set has no order; a re-stamp must re-hash identically)."""
    sh = char_shingles(summary)
    return {"simhash": simhash(sh), "shingles": sorted(sh), "entropy": entropy(summary)}


# --- the band cascade (§3.1): match / match-cross / possible / non-match ------------------------

def classify(stmt_sim: float, subj: float, *, j_maybe: float | None = None,
             j_high: float | None = None, j_cross: float | None = None) -> str:
    """The Fellegi–Sunter band verdict for ONE candidate pair (§3.1 step 2). `subj` is the soft
    subject-overlap score: subj > 0 (shared repo/file) lowers the statement bar to J_HIGH; subj == 0
    demands J_CROSS — the statement alone must carry identity, and the claim is scope-tagged
    cross-cutting (§3.4). Everything from J_MAYBE up to the applicable bar is POSSIBLE — the LLM
    residue, a yes/no on this pair with a no-merge default. Note the design's band table leaves
    [J_HIGH, J_CROSS) at subj == 0 unstated; folding it into POSSIBLE is the no-merge-default
    reading (a strong-but-not-cross-high statement with no shared scope earns an adjudication,
    never a free merge). Thresholds default to the module knobs; keywords override per call (the
    escape hatch --score-gold uses to score candidates)."""
    j_maybe = J_MAYBE if j_maybe is None else j_maybe
    j_high = J_HIGH if j_high is None else j_high
    j_cross = J_CROSS if j_cross is None else j_cross
    bar = j_high if subj > 0 else j_cross
    if stmt_sim >= bar:
        return "match" if subj > 0 else "match-cross"
    if stmt_sim >= j_maybe:
        return "possible"
    return "non-match"


def sim_band(stmt_sim: float, *, j_maybe: float | None = None, j_high: float | None = None,
             j_cross: float | None = None) -> str:
    """The subject-blind histogram band of one stmt_sim — the report's row key: `cross` (≥ J_CROSS),
    `high` ([J_HIGH, J_CROSS)), `residue` ([J_MAYBE, J_HIGH) — the LLM-residue band), `non`
    (< J_MAYBE). `classify` adds the subject dimension; this is the raw similarity histogram."""
    j_maybe = J_MAYBE if j_maybe is None else j_maybe
    j_high = J_HIGH if j_high is None else j_high
    j_cross = J_CROSS if j_cross is None else j_cross
    if stmt_sim >= j_cross:
        return "cross"
    if stmt_sim >= j_high:
        return "high"
    if stmt_sim >= j_maybe:
        return "residue"
    return "non"


BAND_ORDER = ("cross", "high", "residue", "non")
BAND_LABELS = {
    "cross":   ">= J_CROSS        (merge at any subject)",
    "high":    "[J_HIGH, J_CROSS) (merge iff subj > 0)",
    "residue": "[J_MAYBE, J_HIGH) ** THE LLM-RESIDUE BAND **",
    "non":     "<  J_MAYBE        (non-match)",
}


def band_histogram(pairs, *, j_maybe: float | None = None, j_high: float | None = None,
                   j_cross: float | None = None, h_min: float | None = None) -> dict:
    """The band-report's pure math, so a test can drive it with synthetic pairs. `pairs` iterates
    `(stmt_sim, same_project, entropy_a, entropy_b)`. Returns per-band counts split same-project /
    cross-project, plus the projected LLM adjudications a full resolve run would make: pairs whose
    `classify` verdict (project as the subject proxy — ADR-0022's `project_of` hop) is `possible`
    AND both sides clear the entropy gate (a sub-H_MIN statement never MERGES, so it is never
    adjudicated either)."""
    h_min = H_MIN if h_min is None else h_min
    bands = {b: {"same_project": 0, "cross_project": 0} for b in BAND_ORDER}
    adjudications = 0
    total = 0
    for sim, same_project, ent_a, ent_b in pairs:
        total += 1
        split = "same_project" if same_project else "cross_project"
        bands[sim_band(sim, j_maybe=j_maybe, j_high=j_high, j_cross=j_cross)][split] += 1
        verdict = classify(sim, 1.0 if same_project else 0.0,
                           j_maybe=j_maybe, j_high=j_high, j_cross=j_cross)
        if verdict == "possible" and min(ent_a, ent_b) >= h_min:
            adjudications += 1
    return {"total": total, "bands": bands, "residue_adjudications": adjudications}


def stratified_sample(pair_records: list[dict], n: int, *, j_maybe: float | None = None,
                      j_high: float | None = None, j_cross: float | None = None,
                      cross_weight: float | None = None, seed: int = 0) -> list[dict]:
    """Draw ≤ n pairs for hand-labeling, stratified on TWO axes: band × session-relation.

    Band quotas follow `SAMPLE_WEIGHTS` (residue-heavy — that band is the open question); within
    each band, `cross_weight` of the quota comes from CROSS-session pairs and the remainder from
    same-session ones (see CROSS_WEIGHT: cross-session recall is the only recall that feeds
    maturity, same-session pairs merely exercise the gates). Each cell's quota is spent
    EDGE-FIRST: the pairs nearest a threshold are the informative ones (a small threshold move
    flips their verdict), so half the cell takes edge-nearest and the rest is a seeded-random draw
    from the cell's remainder (deterministic — a re-run drafts the same file). Shortfall in any
    cell (fewer pairs than quota) spills to the whole pool by edge-nearness, so the label budget
    is spent even on a lopsided corpus. Each record needs a `stmt_sim`; `same_session` is read if
    present — a record without the tag counts as cross-session, the axis's conservative direction
    (an unknowable session cannot demonstrate sameness) — and everything else rides through
    untouched."""
    j_maybe = J_MAYBE if j_maybe is None else j_maybe
    j_high = J_HIGH if j_high is None else j_high
    j_cross = J_CROSS if j_cross is None else j_cross
    cross_weight = CROSS_WEIGHT if cross_weight is None else cross_weight
    rng = random.Random(seed)
    edges = (j_maybe, j_high, j_cross)

    def edge_dist(r: dict) -> float:
        return min(abs(r["stmt_sim"] - e) for e in edges)

    by_cell: dict[tuple[str, bool], list[dict]] = {}
    for r in pair_records:
        band = sim_band(r["stmt_sim"], j_maybe=j_maybe, j_high=j_high, j_cross=j_cross)
        by_cell.setdefault((band, bool(r.get("same_session"))), []).append(r)

    chosen: list[dict] = []
    leftover: list[dict] = []
    for band in BAND_ORDER:
        band_quota = round(n * SAMPLE_WEIGHTS[band])
        cross_quota = round(band_quota * cross_weight)   # cell split; cells sum to the band quota
        for same_session, cell_quota in ((False, cross_quota), (True, band_quota - cross_quota)):
            pool = by_cell.get((band, same_session), [])
            take = min(cell_quota, len(pool))
            ranked = sorted(pool, key=edge_dist)
            take_edge = min((take + 1) // 2, take)
            picked = ranked[:take_edge]
            rest = ranked[take_edge:]
            rng.shuffle(rest)
            picked += rest[:take - take_edge]
            chosen += picked
            leftover += rest[take - take_edge:]
    if len(chosen) < n and leftover:                    # spill unused quota, edge-nearest first
        leftover.sort(key=edge_dist)
        chosen += leftover[:n - len(chosen)]
    return chosen[:n]


# --- the corpus read: events + their subject proxy (READ-ONLY over the store) -------------------

def load_corpus(root: Path | None = None) -> list[dict]:
    """Every current event (latest version per event_id, `latest_by_kind`) with its signature
    computed FRESH from the summary, its PROJECT resolved as the subject proxy — the same
    `blobstore.project_of` lineage hop the --source filters ride (ADR-0022) — and its SESSION
    resolved via the same cleaned→raw lineage (`blobstore.session_of`, the hop resolve's
    same-session gate keys on): the sampler's second stratification axis. Fresh-computed, not
    read off the blob: pre-S1 events carry no stmt_sig, and the measuring instrument must see the
    whole corpus either way. Malformed/absent blobs are skipped, never fatal."""
    root = root or config.data_root()
    proj_cache: dict = {}
    sess_cache: dict = {}     # separate caches: both key on cleaned_hash, but hold different values
    out: list[dict] = []
    for sid, h in sorted(blobstore.latest_by_kind("event", root).items()):
        try:
            ev = json.loads(blobstore.get(h, root))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(ev, dict):
            continue
        summary = str(ev.get("summary", ""))
        out.append({
            "id": ev.get("id", sid),
            "summary": summary,
            "shingles": char_shingles(summary),
            "entropy": entropy(summary),
            "project": blobstore.project_of(ev.get("cleaned_hash"), root, proj_cache),
            "session": blobstore.session_of(ev.get("cleaned_hash"), root, sess_cache),
            "cleaned_hash": ev.get("cleaned_hash"),
            "evidence": ev.get("evidence") or [],
        })
    return out


def _quote_of(ev: dict, blobs: dict, root: Path) -> str | None:
    """The event's verbatim quote, resolved from its first evidence span against the cleaned blob
    (the event stores a POINTER, never text — ADR-0026), span re-validated at this read boundary
    like every consumer. Gone blob / bad span / undecodable bytes → None (the label file simply
    lacks the quote; the summary still carries the pair)."""
    ch = ev.get("cleaned_hash")
    sp = ev["evidence"][0] if ev["evidence"] and isinstance(ev["evidence"][0], dict) else None
    if not ch or sp is None:
        return None
    try:
        data = blobs.get(ch)
        if data is None:
            data = blobstore.get(ch, root).encode("utf-8")
            blobs[ch] = data
    except (FileNotFoundError, OSError):
        return None
    span = blobstore.validate_span(data, sp.get("byte_start"), sp.get("byte_end"))
    if span is None:
        return None
    try:
        return data[span[0]:span[1]].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _all_pairs(corpus: list[dict]):
    """All unordered event pairs with their exact Jaccard and the same-project proxy. O(n²): a
    corpus of hundreds of events yields low-hundreds-of-thousands of pairs — pure-Python fine; an
    UNRESOLVABLE project on either side counts as cross-project (an unknowable origin cannot
    demonstrate overlap — the empty-subject discipline of §3.1 step 0, mirrored)."""
    for i in range(len(corpus)):
        a = corpus[i]
        for j in range(i + 1, len(corpus)):
            b = corpus[j]
            same = a["project"] is not None and a["project"] == b["project"]
            yield a, b, jaccard(a["shingles"], b["shingles"]), same


# --- CLI: the measuring instrument (band report / label-file draft / gold scoring) --------------

GOLD_LABELS = ("same", "different", "contradicts")


def _thresholds_line(j_maybe, j_high, j_cross, h_min) -> str:
    return (f"J_MAYBE={j_maybe:g} J_HIGH={j_high:g} J_CROSS={j_cross:g} H_MIN={h_min:g}")


def _band_report(root: Path, *, j_maybe: float, j_high: float, j_cross: float,
                 h_min: float) -> None:
    corpus = load_corpus(root)
    pairs = [(sim, same, a["entropy"], b["entropy"]) for a, b, sim, same in _all_pairs(corpus)]
    hist = band_histogram(pairs, j_maybe=j_maybe, j_high=j_high, j_cross=j_cross, h_min=h_min)
    low_h = sum(1 for e in corpus if e["entropy"] < h_min)
    print(f"band report — {len(corpus)} events, {hist['total']} pairs "
          f"({_thresholds_line(j_maybe, j_high, j_cross, h_min)})\n")
    print(f"  {'band':45s} {'total':>7s} {'same-proj':>10s} {'cross-proj':>11s}")
    for band in BAND_ORDER:
        row = hist["bands"][band]
        total = row["same_project"] + row["cross_project"]
        print(f"  {BAND_LABELS[band]:45s} {total:>7d} {row['same_project']:>10d} "
              f"{row['cross_project']:>11d}")
    print(f"\n  projected residue-LLM adjudications for a full resolve run: "
          f"{hist['residue_adjudications']} pair(s)")
    print("    (classify()=='possible' with project as the subject proxy; both sides >= H_MIN —")
    print("     an all-pairs UPPER bound: resolve compares events to CLAIMS inside LSH/subject")
    print("     blocks, so the real count is lower)")
    print(f"  events under H_MIN (SEED-only, never MERGE): {low_h} of {len(corpus)}")


def _sample_pairs(root: Path, n: int, out: Path, *, j_maybe: float, j_high: float,
                  j_cross: float, h_min: float, cross_weight: float) -> None:
    from . import resolve   # lazy: resolve imports sig at module load; both are settled here

    corpus = load_corpus(root)
    blobs: dict[str, bytes] = {}
    # Resolve each event's quote ONCE, then apply the production noise floor (`resolve.thin_quote`
    # — the same gate resolve runs): a thin-quote event is seed-only and NEVER reaches
    # adjudication, so labeling its pairs would spend gold-set budget on verdicts the resolver
    # never asks for. Excluded up front and counted, so the draft says what it left out.
    quotes = {e["id"]: _quote_of(e, blobs, root) for e in corpus}
    thin = {eid for eid, q in quotes.items() if resolve.thin_quote(q)}
    records = []
    excluded = 0
    for a, b, sim, same in _all_pairs(corpus):
        if a["id"] in thin or b["id"] in thin:
            excluded += 1
            continue
        records.append({
            "event_a": a["id"], "event_b": b["id"],
            "summary_a": a["summary"], "summary_b": b["summary"],
            "quote_a": quotes[a["id"]], "quote_b": quotes[b["id"]],
            "stmt_sim": round(sim, 4), "same_project": same,
            # the session-relation tag (each side's cleaned→raw session identity): an unresolvable
            # session on either side counts as CROSS — the same discipline as `_all_pairs`' project
            "same_session": a["session"] is not None and a["session"] == b["session"],
            "repo_a": a["project"], "repo_b": b["project"],
            "label": None,
        })
    sample = stratified_sample(records, n, j_maybe=j_maybe, j_high=j_high, j_cross=j_cross,
                               cross_weight=cross_weight)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": ("Hand-label each pair's `label`: 'same' (the same underlying lesson), "
                     "'different' (distinct lessons), or 'contradicts' (one overturns the other). "
                     "Leave null to skip. Score candidates with `python -m ratchet.sig "
                     "--score-gold <this file>` (+ --j-* overrides)."),
        "thresholds": {"j_maybe": j_maybe, "j_high": j_high, "j_cross": j_cross, "h_min": h_min},
        "sampling": {"cross_weight": cross_weight, "weights": SAMPLE_WEIGHTS,
                     "noise_floor_excluded_pairs": excluded,
                     "thin_quote_events": len(thin)},
        "pairs": sample,
    }
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    n_same = sum(1 for p in sample if p["same_session"])
    by_band = Counter(sim_band(p["stmt_sim"], j_maybe=j_maybe, j_high=j_high, j_cross=j_cross)
                      for p in sample)
    repos = {r for p in sample for r in (p["repo_a"], p["repo_b"]) if r}
    print(f"wrote {len(sample)} pair(s) to {out}")
    print(f"  stratified band x session-relation (weights {SAMPLE_WEIGHTS}, "
          f"cross_weight={cross_weight:g}, edge-first; "
          f"{_thresholds_line(j_maybe, j_high, j_cross, h_min)})")
    print(f"  session-relation: {len(sample) - n_same} cross-session / {n_same} same-session")
    print("  bands: " + "  ".join(f"{band}={by_band.get(band, 0)}" for band in BAND_ORDER))
    print(f"  distinct repos touched: {len(repos)}")
    print(f"  noise floor: excluded {excluded} pair(s) touching {len(thin)} thin-quote event(s) "
          f"(resolve.thin_quote — those never reach adjudication)")


def _score_split(labeled: list[dict], *, j_maybe: float, j_high: float, j_cross: float,
                 h_min: float) -> None:
    """Print the recall/precision table + summary lines for ONE session-relation split of the
    labeled pairs — the body `_score_gold` runs per split."""
    per_band: dict[str, Counter] = {b: Counter() for b in ("match", "match-cross", "possible", "non-match")}
    same_total = 0
    same_blocked = 0
    for p in labeled:
        verdict = classify(p["stmt_sim"], 1.0 if p.get("same_project") else 0.0,
                           j_maybe=j_maybe, j_high=j_high, j_cross=j_cross)
        per_band[verdict][p["label"]] += 1
        if p["label"] == "same":
            same_total += 1
            # the entropy gate blocks a MERGE regardless of band — a `same` pair with a sub-H_MIN
            # side is stranded by the triviality gate, not the thresholds; surface it separately.
            if min(entropy(p.get("summary_a", "")), entropy(p.get("summary_b", ""))) < h_min:
                same_blocked += 1
    print(f"  {'band':12s} {'n':>5s} {'same':>6s} {'diff':>6s} {'contra':>7s} {'precision(same)':>16s}")
    for band in ("match", "match-cross", "possible", "non-match"):
        c = per_band[band]
        nb = sum(c.values())
        prec = f"{c['same'] / nb:.2f}" if nb else "-"
        print(f"  {band:12s} {nb:>5d} {c['same']:>6d} {c['different']:>6d} "
              f"{c['contradicts']:>7d} {prec:>16s}")
    det_same = per_band["match"]["same"] + per_band["match-cross"]["same"]
    stranded = per_band["non-match"]["same"]
    residue = sum(per_band["possible"].values())
    print(f"  deterministic recall of `same` (match|match-cross): "
          f"{det_same}/{same_total}" + (f" = {det_same / same_total:.2f}" if same_total else ""))
    print(f"  `same` STRANDED in non-match (the silent-duplicate error): "
          f"{stranded}/{same_total}" + (f" = {stranded / same_total:.2f}" if same_total else ""))
    print(f"  residue fraction (pairs an LLM must adjudicate): {residue}/{len(labeled)} "
          f"= {residue / len(labeled):.2f}")
    print(f"  `same` pairs blocked by the entropy gate (either side < H_MIN): {same_blocked}")


def _score_gold(path: Path, *, j_maybe: float, j_high: float, j_cross: float,
                h_min: float) -> None:
    obj = json.loads(path.read_text(encoding="utf-8"))
    pairs = obj["pairs"] if isinstance(obj, dict) else obj
    labeled = [p for p in pairs if p.get("label") in GOLD_LABELS]
    if not labeled:
        print(f"no labeled pairs in {path} (label each pair 'same'/'different'/'contradicts')")
        return
    # Split by session-relation: post-gates the adjudicator only ever sees CROSS-session pairs
    # (resolve's same-session gate), and maturity counts distinct sessions — so the cross-session
    # table is the headline; same-session rows measure the gates, not the adjudicator. A pair
    # without the tag (a pre-round-2 label file) lands in UNTAGGED rather than polluting either.
    groups: dict[bool | None, list[dict]] = {False: [], True: [], None: []}
    for p in labeled:
        tag = p.get("same_session")
        groups[tag if isinstance(tag, bool) else None].append(p)
    print(f"gold scoring — {len(labeled)} labeled pair(s) "
          f"({_thresholds_line(j_maybe, j_high, j_cross, h_min)})")
    sections = (
        (False, "CROSS-SESSION — the headline: the only pairs adjudication acts on post-gates"),
        (True, "SAME-SESSION — exercises the gates (dup fast-path / same-session skip), "
               "never adjudicated"),
        (None, "UNTAGGED — file predates session tagging (resample to tag)"),
    )
    for tag, title in sections:
        rows = groups[tag]
        if not rows and tag is not False:
            continue                  # an empty same-session/untagged split has nothing to say; an
                                      # empty CROSS-SESSION split stays LOUD — the headline is missing
        print(f"\n  == {title} — {len(rows)} labeled pair(s)")
        if not rows:
            print("     (none — the headline recall is UNMEASURED; resample with --cross-weight)")
            continue
        _score_split(rows, j_maybe=j_maybe, j_high=j_high, j_cross=j_cross, h_min=h_min)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="sig",
        description="Statement-signature measuring instrument (dream v3 §2.1/§3.1) — READ-ONLY "
                    "over the store: band report, hand-label sampling, gold scoring.")
    ap.add_argument("--band-report", action="store_true",
                    help="all-pairs Jaccard histogram over the current events' summaries, split "
                         "same/cross-project, + the projected LLM-residue adjudications")
    ap.add_argument("--sample-pairs", type=int, metavar="N",
                    help="draft N pairs for hand-labeling, stratified band x session-relation "
                         "(toward the residue band, the threshold edges, and cross-session pairs); "
                         "pairs failing the noise floor (resolve.thin_quote) are excluded")
    ap.add_argument("--cross-weight", type=float, default=CROSS_WEIGHT,
                    help=f"--sample-pairs only: fraction of each band's quota drawn from "
                         f"CROSS-session pairs (default {CROSS_WEIGHT}) — post-gates, adjudication "
                         f"only acts cross-session, so that recall is the headline")
    ap.add_argument("--out", type=Path,
                    help="label-file path for --sample-pairs "
                         "(default: <data_root>/tuning/pairs_to_label.json)")
    ap.add_argument("--score-gold", type=Path, metavar="PATH",
                    help="score the bands against a hand-labeled pairs file (re-run with --j-* "
                         "overrides to score threshold candidates)")
    ap.add_argument("--j-maybe", type=float, default=J_MAYBE,
                    help=f"residue-band floor (default {J_MAYBE}; below it: non-match at $0)")
    ap.add_argument("--j-high", type=float, default=J_HIGH,
                    help=f"deterministic merge bar when subjects overlap (default {J_HIGH})")
    ap.add_argument("--j-cross", type=float, default=J_CROSS,
                    help=f"stricter merge bar for disjoint subjects (default {J_CROSS}; "
                         f"must exceed --j-high)")
    ap.add_argument("--h-min", type=float, default=H_MIN,
                    help=f"entropy floor — below it a statement seeds but never merges "
                         f"(default {H_MIN})")
    args = ap.parse_args(argv)

    if not (args.j_maybe < args.j_high < args.j_cross):
        ap.error(f"thresholds must order J_MAYBE < J_HIGH < J_CROSS (§3.1) — got "
                 f"{args.j_maybe} / {args.j_high} / {args.j_cross}")
    if not (0.0 <= args.cross_weight <= 1.0):
        ap.error(f"--cross-weight is a fraction of each band's quota — must be in [0, 1], "
                 f"got {args.cross_weight}")
    modes = sum(bool(m) for m in (args.band_report, args.sample_pairs is not None,
                                  args.score_gold is not None))
    if modes != 1:
        ap.error("pick exactly one of --band-report / --sample-pairs N / --score-gold PATH")

    root = config.data_root()
    kw = dict(j_maybe=args.j_maybe, j_high=args.j_high, j_cross=args.j_cross, h_min=args.h_min)
    if args.band_report:
        _band_report(root, **kw)
    elif args.sample_pairs is not None:
        out = args.out or (root / "tuning" / "pairs_to_label.json")
        _sample_pairs(root, args.sample_pairs, out, cross_weight=args.cross_weight, **kw)
    else:
        _score_gold(args.score_gold, **kw)


if __name__ == "__main__":
    main()
