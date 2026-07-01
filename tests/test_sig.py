"""sig tests: the statement-signature layer (dream v3 §2.1/§3.1) is pure stdlib math, so the suite
is deterministic and offline. The load-bearing check is HASH STABILITY — shingle hashes are
blake2b(digest_size=8) because shingle sets get persisted on event blobs, so the committed golden
(`tests/golden/stmt_sig_pins.json`) pins exact shingle/simhash/entropy values; a drift there means
every stored signature silently forks. Also under test: the band cascade's edge semantics
(`classify`, §3.1 — including the folded [J_HIGH, J_CROSS)@subj==0 POSSIBLE band), the band-report
math on a synthetic pair set, the stratified label sampler, and the READ-ONLY CLI over a seeded
temp store (band report / sample-pairs / score-gold). Run: `python tests/test_sig.py`."""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-sig-")

from ratchet import blobstore, config, sig  # noqa: E402

config.ensure_layout()

GOLDEN = Path(__file__).resolve().parent / "golden" / "stmt_sig_pins.json"


# --- 1. golden pins: exact values guard hash stability (blake2b, not salted hash()) ----------

assert GOLDEN.exists(), f"missing golden file — commit it: {GOLDEN}"
golden = json.loads(GOLDEN.read_text())
assert golden["shingle_k"] == sig.SHINGLE_K, "the pinned k must match the module knob"
for pin in golden["pins"]:
    s = sig.stmt_sig(pin["text"])
    assert sig.normalize(pin["text"]) == pin["normalized"], f"normalize drifted: {pin['text']!r}"
    assert s["shingles"] == pin["shingles"], \
        f"shingle hashes drifted for {pin['text']!r} — blake2b stability is load-bearing (persisted sets)"
    assert s["simhash"] == pin["simhash"], f"simhash drifted: {pin['text']!r}"
    assert s["entropy"] == pin["entropy"], f"entropy drifted: {pin['text']!r}"
    assert list(sig.lsh_bands(s["simhash"])) == pin["lsh_bands"], f"LSH bands drifted: {pin['text']!r}"
short = [p for p in golden["pins"] if len(p["normalized"]) < sig.SHINGLE_K]
assert short and all(len(p["shingles"]) == 1 for p in short), \
    "the golden covers the shorter-than-k single-shingle path"

print(f"OK — golden pins: {len(golden['pins'])} fixed strings re-hash byte-identically "
      "(shingles, simhash, entropy, LSH bands)")


# --- 2. normalize: casefold + punctuation→space + whitespace collapse, dumb by design --------

assert sig.normalize("Always commit with JJ, never Git!") == "always commit with jj never git"
assert sig.normalize("a--b   c\t\nd") == "a b c d", "punctuation and whitespace runs collapse to single spaces"
assert sig.normalize("λ Wörk ✓") == "λ wörk", "unicode alnum survives; symbols map to spaces"
assert sig.normalize("") == "" and sig.normalize("!!! ---") == "", "no content → empty"
assert sig.normalize("STRASSE") == sig.normalize("strasse"), "casefold, not lower"

# --- 3. char_shingles: k-windows over the normalized text; short text is its own shingle -----

assert len(sig.char_shingles("abcdef")) == 3, "len-6 text, k=4 → 3 windows"
assert sig.char_shingles("OK") == sig.char_shingles("ok"), "shingles are over the NORMALIZED text"
assert len(sig.char_shingles("ok")) == 1, "shorter-than-k text signs as one whole-text shingle"
assert sig.char_shingles("") == frozenset() and sig.char_shingles("?!") == frozenset()
assert all(0 <= h < 2**64 for h in sig.char_shingles("some text here")), "shingle hashes are 64-bit ints"

# --- 4. simhash + hamming: near text → near hashes -------------------------------------------

S1 = "Always commit with jj, never git, in every repository."
S2 = "Always commit with jj, never git, in every repo."
S3 = "JAX autodiff requires pure functions with no side effects."
sh1, sh2, sh3 = (sig.char_shingles(s) for s in (S1, S2, S3))
h1, h2, h3 = (sig.simhash(s) for s in (sh1, sh2, sh3))
assert sig.simhash(frozenset()) == 0, "empty set → 0 (deterministic sentinel)"
assert sig.hamming(h1, h1) == 0 and sig.hamming(0, 2**64 - 1) == 64
assert sig.hamming(h1, h2) < sig.hamming(h1, h3), "near-identical wording lands nearer than a different lesson"
assert 0 <= h1 < 2**64, "simhash is a 64-bit int"

# --- 5. jaccard: exact, empty-safe in the no-merge direction ---------------------------------

assert sig.jaccard(frozenset(), frozenset()) == 0.0, "empty∩empty is evidence of nothing → 0.0, never 1.0"
assert sig.jaccard(sh1, frozenset()) == 0.0
assert sig.jaccard(frozenset({1, 2}), frozenset({2, 3})) == 1 / 3
assert sig.jaccard(sh1, sh1) == 1.0 and sig.jaccard(frozenset({1}), frozenset({2})) == 0.0
SIM_12, SIM_13, SIM_23 = sig.jaccard(sh1, sh2), sig.jaccard(sh1, sh3), sig.jaccard(sh2, sh3)
assert SIM_12 > sig.J_CROSS, f"the near-dup pair clears J_CROSS ({SIM_12:.4f})"
assert max(SIM_13, SIM_23) < sig.J_MAYBE, "different lessons land under J_MAYBE (the v2 fixture property)"

# --- 6. entropy: the triviality-gate signal ---------------------------------------------------

assert sig.entropy("") == 0.0 and sig.entropy("aaaa") == 0.0, "no character diversity → 0 bits"
assert sig.entropy("ok") == 1.0, "two equiprobable chars → exactly 1 bit"
assert sig.entropy("OK !") == sig.entropy("ok"), "entropy is over the NORMALIZED text"
assert sig.entropy(S1) > sig.H_MIN > sig.entropy("ok"), \
    "H_MIN separates a real sentence from a degenerate statement"

# --- 7. lsh_bands: 4 × 16-bit split, MSB first, lossless --------------------------------------

b = sig.lsh_bands(h1)
assert len(b) == 4 and all(0 <= x <= 0xFFFF for x in b), "four 16-bit bands"
assert (b[0] << 48) | (b[1] << 32) | (b[2] << 16) | b[3] == h1, "bands reconstruct the simhash (lossless)"
flipped = sig.lsh_bands(h1 ^ 1)
assert flipped[:3] == b[:3] and flipped[3] != b[3], "a 1-bit flip perturbs exactly one band → 3 still collide"

# --- 8. stmt_sig: the persisted, JSON-serializable shape --------------------------------------

ss = sig.stmt_sig(S1)
assert set(ss) == {"simhash", "shingles", "entropy"}
assert ss["shingles"] == sorted(ss["shingles"]), "shingles sorted → canonical-json stable across re-stamps"
assert json.loads(json.dumps(ss)) == ss, "JSON round-trips exactly (ints + float)"
assert frozenset(ss["shingles"]) == sh1 and ss["simhash"] == h1 and ss["entropy"] == sig.entropy(S1)

print("OK — pure math: normalize/shingles/simhash/hamming/jaccard/entropy/lsh_bands/stmt_sig "
      "(empty-safe in the no-merge direction; bands lossless)")


# --- 9. classify: the §3.1 band edges, subj as the SOFT bar selector ---------------------------

EPS = 1e-9
assert sig.J_MAYBE < sig.J_HIGH < sig.J_CROSS, "the knob ordering §3.1 assumes"
assert sig.classify(sig.J_HIGH, 1) == "match", "at the bar, subject overlap → $0 local merge"
assert sig.classify(sig.J_HIGH - EPS, 1) == "possible", "just under → the LLM residue"
assert sig.classify(sig.J_CROSS, 0) == "match-cross", "disjoint subjects demand the stricter bar"
assert sig.classify(sig.J_CROSS - EPS, 0) == "possible", "under J_CROSS with subj==0 → residue"
assert sig.classify(sig.J_HIGH, 0) == "possible", \
    "the folded band: [J_HIGH, J_CROSS)@subj==0 is POSSIBLE (no-merge default), never a free merge"
assert sig.classify(sig.J_CROSS, 1) == "match", "a cross-high statement WITH subject overlap is a local match"
assert sig.classify(sig.J_MAYBE, 0) == sig.classify(sig.J_MAYBE, 1) == "possible", "the residue floor is inclusive"
assert sig.classify(sig.J_MAYBE - EPS, 1) == "non-match", "under the floor → $0 non-match, any subject"
assert sig.classify(0.0, 1) == "non-match" and sig.classify(1.0, 1) == "match"
# the escape hatch: per-call threshold overrides (what --score-gold turns)
assert sig.classify(0.4, 1, j_high=0.3) == "match", "an override moves the bar"
assert sig.classify(0.4, 0, j_cross=0.35) == "match-cross"
assert sig.classify(0.2, 1, j_maybe=0.1) == "possible" and sig.classify(0.05, 1, j_maybe=0.1) == "non-match"

# sim_band: the subject-blind histogram row key, same edges
assert sig.sim_band(sig.J_CROSS) == "cross" and sig.sim_band(sig.J_CROSS - EPS) == "high"
assert sig.sim_band(sig.J_HIGH) == "high" and sig.sim_band(sig.J_HIGH - EPS) == "residue"
assert sig.sim_band(sig.J_MAYBE) == "residue" and sig.sim_band(sig.J_MAYBE - EPS) == "non"

print("OK — classify/sim_band: §3.1 band edges exact (inclusive floors, folded subj==0 band → "
      "possible, overrides work)")


# --- 10. band_histogram: the report's math on a synthetic in-memory pair set -------------------

synthetic = [
    (0.90, True, 4.0, 4.0),    # cross band, same-project  → classify: match        (no LLM)
    (0.90, False, 4.0, 4.0),   # cross band, cross-project → match-cross            (no LLM)
    (0.60, True, 4.0, 4.0),    # high band, same-project   → match                  (no LLM)
    (0.60, False, 4.0, 4.0),   # high band, cross-project  → POSSIBLE (folded band) → adjudicated
    (0.30, True, 4.0, 4.0),    # residue, same-project     → possible               → adjudicated
    (0.30, False, 4.0, 2.0),   # residue, but one side under H_MIN → possible yet NEVER adjudicated
    (0.10, True, 4.0, 4.0),    # non-match
]
hist = sig.band_histogram(synthetic)
assert hist["total"] == 7
assert hist["bands"] == {"cross": {"same_project": 1, "cross_project": 1},
                         "high": {"same_project": 1, "cross_project": 1},
                         "residue": {"same_project": 1, "cross_project": 1},
                         "non": {"same_project": 1, "cross_project": 0}}, hist["bands"]
assert hist["residue_adjudications"] == 2, \
    "exactly the folded-band pair + the clean residue pair reach the LLM (the entropy-gated one never)"
# overrides flow through: raising J_MAYBE above 0.30 drops the residue pairs to non-match
hist2 = sig.band_histogram(synthetic, j_maybe=0.4)
assert hist2["bands"]["non"] == {"same_project": 2, "cross_project": 1} and \
       hist2["residue_adjudications"] == 1, "only the folded high-band pair remains adjudicable"

print("OK — band_histogram: synthetic pairs land in exact bands; projected adjudications honor "
      "the subject proxy AND the entropy gate; overrides flow through")


# --- 11. stratified_sample: label budget weighted toward the residue + the edges ---------------

recs = []
for band, sims in (("non", [0.01 + 0.015 * i for i in range(10)]),
                   ("residue", [0.26 + 0.028 * i for i in range(10)]),
                   ("high", [0.555 + 0.014 * i for i in range(10)]),
                   ("cross", [0.71 + 0.028 * i for i in range(10)])):
    recs += [{"id": f"{band}{i}", "stmt_sim": round(s, 4)} for i, s in enumerate(sims)]
picked = sig.stratified_sample(recs, 10)
assert len(picked) == 10 and len({r["id"] for r in picked}) == 10, "n unique pairs"
by_band = {}
for r in picked:
    by_band.setdefault(sig.sim_band(r["stmt_sim"]), []).append(r["id"])
assert {k: len(v) for k, v in by_band.items()} == {"residue": 5, "high": 2, "cross": 1, "non": 2}, \
    f"quota follows SAMPLE_WEIGHTS: {by_band}"
assert picked == sig.stratified_sample(recs, 10), "seeded → a re-run drafts the identical file"
# a starved band spills its quota to the others (edge-nearest first) — the sample stays full-size
thin = [r for r in recs if sig.sim_band(r["stmt_sim"]) != "residue"][:12] + \
       [{"id": "residue0", "stmt_sim": 0.3}]
spill = sig.stratified_sample(thin, 10)
assert len(spill) == 10, "shortfall in one band spills — the label budget is spent, not wasted"

print("OK — stratified_sample: SAMPLE_WEIGHTS quotas, deterministic under the seed, "
      "starved bands spill")


# --- 12. the CLI over a seeded temp store: READ-ONLY band report / sample / score --------------

def seed_event(eid: str, project: str, summary: str, quote: str) -> None:
    """One event the way the pipeline lays it down: raw transcript (origin_ref.project — what
    `project_of` resolves) → derived cleaned blob → event blob whose evidence span points at the
    quote's bytes (what `--sample-pairs` resolves as quote_a/b)."""
    raw_h, _ = blobstore.ingest(f"transcript for {eid}\n{quote}\n", source_kind="transcript",
                                source_id=f"sess-{eid}", origin_ref={"project": project,
                                                                     "session_id": f"sess-{eid}"})
    cleaned = f"[assistant]\n{quote}\n"
    ch, _ = blobstore.put_derived(cleaned, source_kind="transcript", derived_from=raw_h,
                                  produced_by="weave", render_version="r1", fmt="text/cleaned")
    start = len("[assistant]\n".encode())
    ev = {"id": eid, "cleaned_hash": ch,
          "evidence": [{"byte_start": start, "byte_end": start + len(quote.encode())}],
          "summary": summary, "markers": {}, "relevance": "novel", "confidence": 0.9,
          "supersedes": None}
    blobstore.ingest(blobstore.canonical_json(ev), source_kind="event", source_id=eid,
                     origin_ref={"stage": "test"})


Q1, Q2, Q3 = "use jj here, not git", "jj not git, again", "autodiff needs purity"
seed_event("e1", "projA", S1, Q1)
seed_event("e2", "projA", S2, Q2)
seed_event("e3", "projB", S3, Q3)

corpus = sig.load_corpus()
assert [e["id"] for e in corpus] == ["e1", "e2", "e3"], "latest_by_kind('event'), sorted by id"
assert [e["project"] for e in corpus] == ["projA", "projA", "projB"], \
    "the subject proxy resolves through cleaned→raw origin_ref.project (ADR-0022)"
assert corpus[0]["shingles"] == sh1 and corpus[0]["entropy"] == sig.entropy(S1), \
    "signatures computed fresh from the summary"

def run_cli(argv) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        sig.main(argv)
    return buf.getvalue()

# band report: pair(e1,e2) is same-project ≥ J_CROSS; both e3 pairs are cross-project non-match.
report = run_cli(["--band-report"])
assert "3 events, 3 pairs" in report, report
rows = {band: next(l for l in report.splitlines() if sig.BAND_LABELS[band] in l)
        for band in sig.BAND_ORDER}
assert rows["cross"].split()[-3:] == ["1", "1", "0"], f"the near-dup same-project pair: {rows['cross']}"
assert rows["high"].split()[-3:] == ["0", "0", "0"]
assert rows["residue"].split()[-3:] == ["0", "0", "0"]
assert rows["non"].split()[-3:] == ["2", "0", "2"], f"both e3 pairs, cross-project: {rows['non']}"
assert "residue-LLM adjudications for a full resolve run: 0 pair(s)" in report
assert "events under H_MIN (SEED-only, never MERGE): 0 of 3" in report
# threshold overrides re-band the same pairs: J_CROSS above the near-dup sim pushes it to `high`,
# where same-project still merges — but a raised J_MAYBE=0.9... keep it observable: J_CROSS=0.9
report_hi = run_cli(["--band-report", "--j-cross", "0.9"])
rows_hi = next(l for l in report_hi.splitlines() if sig.BAND_LABELS["high"] in l)
assert rows_hi.split()[-3:] == ["1", "1", "0"], "an override re-bands the pair (the escape hatch works)"

# the report never writes: blob count unchanged (READ-ONLY is the contract)
n_blobs_before = sum(1 for _ in blobstore.iter_meta())

# sample-pairs: drafts the hand-label file with the full schema; quotes resolve from spans
out_path = Path(os.environ["RATCHET_DATA_DIR"]) / "tuning" / "pairs_to_label.json"
msg = run_cli(["--sample-pairs", "3"])
assert str(out_path) in msg and out_path.exists(), "default out = <data_root>/tuning/pairs_to_label.json"
payload = json.loads(out_path.read_text())
assert payload["thresholds"] == {"j_maybe": sig.J_MAYBE, "j_high": sig.J_HIGH,
                                 "j_cross": sig.J_CROSS, "h_min": sig.H_MIN}
assert len(payload["pairs"]) == 3, "all 3 pairs fit the budget"
for p in payload["pairs"]:
    assert set(p) == {"event_a", "event_b", "summary_a", "summary_b", "quote_a", "quote_b",
                      "stmt_sim", "same_project", "label"}
    assert p["label"] is None and isinstance(p["same_project"], bool)
    assert p["quote_a"] in (Q1, Q2, Q3) and p["quote_b"] in (Q1, Q2, Q3), \
        "quotes resolve VERBATIM from the evidence spans (never stored on the event)"
assert sum(1 for _ in blobstore.iter_meta()) == n_blobs_before, "the CLI wrote NO blob (read-only)"

# score-gold: label the drafted pairs, score the default thresholds, then a candidate override
for p in payload["pairs"]:
    p["label"] = "same" if p["stmt_sim"] >= 0.7 else "different"
out_path.write_text(json.dumps(payload))
score = run_cli(["--score-gold", str(out_path)])
assert "3 labeled pair(s)" in score
assert "deterministic recall of `same` (match|match-cross): 1/1 = 1.00" in score, score
assert "STRANDED in non-match (the silent-duplicate error): 0/1 = 0.00" in score
assert "residue fraction (pairs an LLM must adjudicate): 0/3 = 0.00" in score
# a candidate that drags the residue floor to ~0: the two `different` pairs become LLM residue
score2 = run_cli(["--score-gold", str(out_path), "--j-maybe", "0.01", "--j-high", "0.02"])
assert "residue fraction (pairs an LLM must adjudicate): 2/3 = 0.67" in score2, \
    "threshold candidates re-band the SAME labels — the tuning loop"

# guard rails: exactly one mode; the knob ordering J_MAYBE < J_HIGH < J_CROSS is enforced
for bad in ([], ["--band-report", "--score-gold", str(out_path)],
            ["--band-report", "--j-high", "0.9"]):
    try:
        with redirect_stderr(io.StringIO()):
            sig.main(bad)
        raise AssertionError(f"argv {bad} must be rejected")
    except SystemExit:
        pass

print("OK — CLI over a seeded store: band report exact (split by project proxy), overrides re-band,")
print("     sample-pairs drafts the label schema with span-resolved quotes (no blob written),")
print("     score-gold reports recall/stranded/residue and re-scores under threshold candidates.")

print("\nall sig tests passed.")
