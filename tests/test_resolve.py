"""resolve (dream v3) tests: the statement-first entity resolver, exercised OFFLINE with FAKE
Completers (no network, no API key) so the suite is deterministic. The design-doc §9 goldens:

  G1 — the motivating Zig/JAX/NEP-50 trio: three DISTINCT lessons → 3 separate claims, 0 matured,
       **0 LLM calls** (the $0 rejection layer settles every pair; the fake completer raises if
       touched). This is the fix for v2's over-merge.
  G2 — the jj-not-git lesson across 2 sessions from 2 REPOS, PARAPHRASED into the ~0.2-similarity
       zone (a verbatim fixture would pass via near-dup collision and prove nothing): the
       rare-shingle channel surfaces the candidate → residue → the fake picks same-as → ONE claim,
       2-session support, scope derives `cross-cutting`, matures at the single bar.
  G3 — retraction IS the split: fold(claim − edge) == fold(claim had the event never matched) —
       support decrements, the claim un-matures, byte-exact.
  G4 — the measured trap: two DIFFERENT lessons sharing vocabulary at stmt_sim ~0.35 → residue →
       the fake answers none → 2 claims; and a WRONG same-as verdict leaves a full audit trail
       (by:"llm" + candidates_shown on the edge — the review card's material).
  G5 — reject-merge: ONE compound decision retracts the edge, reopens the event (epoch bumps past
       the done-marker), permanently blocks the pair; re-resolve seeds the event's own claim with
       ZERO LLM calls.

Plus: budget-deferral (residue events defer unmarked under --max-usd while $0 events complete),
idempotent re-tick, --reset-v2 on a seeded v2 store, the reopen-aware + wall-clock-bounded
forget, and SITTING COALESCING (§14): same-repo sessions within COALESCE_HOURS count as one
sitting for support/maturity and the cascade gate — a /clear-split afternoon cannot fake
2-session maturity. Run: `python tests/test_resolve.py`."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-resolve-")

from ratchet import blobstore, block, chunk, config, dream, glean, resolve, sig  # noqa: E402
from ratchet.completer import Completion  # noqa: E402
from ratchet.dream import working_set  # noqa: E402
from ratchet.resolve import (  # noqa: E402
    candidate_ids, claim_pool, current_claims, high_confidence_view, is_active, _clean_verdict)

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# --- fixtures: summaries crafted into the measured similarity bands (verified with sig.jaccard) ----
# G1 — three genuinely distinct lessons (the v2 over-merge trio); pairwise stmt_sim < J_MAYBE.
ZIG = "zig struct types are anonymous by default; assign them to a const to name them"
JAXL = "jax autodiff silently fails on functions with side effects; keep traced code pure"
NEP = "numpy nep-50 promotion keeps python scalars weak so float32 arrays stay float32"
# G2 — the SAME lesson, paraphrased into the residue band (~0.20 — where real paraphrases measure).
JJ_SEED = "always commit with jj and never use git for version control"
JJ_PARA = "version control goes through jj, so avoid reaching for git commands"
# G4 — two DIFFERENT lessons sharing vocabulary (~0.35 — the measured-trap shape; corpus max 0.311).
PYTEST = "run the test suite with python -m pytest from the repo root, not from tests/"
RUFF = "run the linter with python -m ruff from the repo root before committing"
# §10–§13 (the structural gates): a third jj paraphrase in the residue band against BOTH tellings
# (so a thin candidate would compete if not gated), a healthy-length line carrying the JJ_SEED
# summary verbatim (the overlapping-chunk dup case: same summary, different bytes), and a noise
# quote — the measured "[assistant]"/frontmatter family, failing the QUOTE_MIN_CHARS arm.
JJ_PARA2 = "use jj rather than git when committing changes in version control"
DUP_LINE = "jj is the only vcs here: always commit with jj and never use git for version control"
NOISE = "node_type: memory"

M_HI = {"surprise": 0.9, "insight": 0.3}
M_MID = {"surprise": 0.2, "insight": 0.7}


def sim_of(a, b):
    return sig.jaccard(sig.char_shingles(a), sig.char_shingles(b))


# the fixtures prove what they claim: G1 below the band, G2/G4 inside it, entropies over the gate.
for a, b in ((ZIG, JAXL), (ZIG, NEP), (JAXL, NEP)):
    assert sim_of(a, b) < sig.J_MAYBE, f"G1 fixture drifted into the residue band: {sim_of(a, b):.3f}"
assert sig.J_MAYBE <= sim_of(JJ_SEED, JJ_PARA) < 0.31, \
    f"G2 fixture must sit in the paraphrase zone (~0.2): {sim_of(JJ_SEED, JJ_PARA):.4f}"
assert sig.J_MAYBE <= sim_of(PYTEST, RUFF) < sig.J_HIGH, \
    f"G4 fixture must sit in the residue band: {sim_of(PYTEST, RUFF):.4f}"
for s in (ZIG, JAXL, NEP, JJ_SEED, JJ_PARA, JJ_PARA2, PYTEST, RUFF):
    assert sig.entropy(s) >= sig.H_MIN, f"fixture under the entropy gate: {s!r}"
# the gate fixtures prove what they claim: PARA2 in the residue band against both jj tellings
# (below DUP_EXACT — a paraphrase, not a duplicate); NOISE under the length arm of the floor.
for a in (JJ_SEED, JJ_PARA):
    assert sig.J_MAYBE <= sim_of(a, JJ_PARA2) < resolve.DUP_EXACT, \
        f"JJ_PARA2 drifted out of the residue band vs {a!r}: {sim_of(a, JJ_PARA2):.4f}"
assert resolve.thin_quote(NOISE) and len(NOISE) < resolve.QUOTE_MIN_CHARS, "NOISE must fail the floor"
for q in (ZIG, JAXL, NEP, JJ_SEED, JJ_PARA, JJ_PARA2, DUP_LINE, PYTEST, RUFF):
    assert not resolve.thin_quote(q), f"healthy fixture reads thin: {q!r}"
assert resolve.thin_quote(None) and resolve.thin_quote("[assistant]"), \
    "a missing quote and the measured noise anchor both fail the floor"


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}


def make_session(sid, line, *, repo=None, mtime=None):
    """A real transcript → cleaned blob → chunkset, with a controlled REPO (origin cwd basename —
    the subject_key's repo facet) and an optional VALID-TIME (origin mtime — what sitting coalescing
    and recency weighting read; omitted = undated, which never coalesces and weighs 1.0).
    Multibyte filler forces byte≠char offsets (span math under test)."""
    records = [rec("u0", None, "user", message={"role": "user", "content": f"session {sid} kickoff"})]
    parent = "u0"
    for i in range(4):
        body = f"step {i}: " + ("λ wörk ✓ " * 20)
        if i == 2:
            body = line
        records.append(rec(f"{sid}a{i}", parent, "assistant", message=amsg(f"{sid}M{i}", body)))
        parent = f"{sid}a{i}"
    blob = "\n".join(json.dumps(r) for r in records) + "\n"
    origin = {"session_id": sid}
    if repo:
        origin["cwd"] = f"/home/sulin/{repo}"
    if mtime:
        origin["mtime"] = mtime
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid, origin_ref=origin)
    cs, _, _ = chunk.materialize(raw_h, budget=600)
    return cs


class GleanFake:
    """Points at the numbered prompt line carrying each durable line (ADR-0026) and emits a
    CONTROLLED summary per line — resolve signs SUMMARIES, so the fixture's stmt_sims are exact."""
    def __init__(self, lines, *, summaries=None, markers=None, confidence=0.85):
        self.lines, self.summaries = lines, summaries or {}
        self.markers, self.confidence = markers or {}, confidence

    def __call__(self, system, user):
        line_of = {}
        for row in user.splitlines():
            num, sep, body = row.partition("| ")
            if sep and num.strip().isdigit():
                line_of[int(num)] = body
        cands = []
        for ln in self.lines:
            hit = next((n for n, body in line_of.items() if ln in body), None)
            if hit is not None:
                cands.append({"lines": {"from": hit, "to": hit},
                              "summary": self.summaries.get(ln, ln),
                              "markers": self.markers.get(ln, M_MID), "confidence": self.confidence})
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class ResolveFake:
    """Scripted residue verdicts (Haiku's seat), in call order; counts calls and keeps the prompts
    it saw so a test can assert the comparative-with-none framing reached the model."""
    def __init__(self, verdicts=(), *, cost=0.001):
        self.verdicts, self.cost, self.calls, self.prompts = list(verdicts), cost, 0, []

    def __call__(self, system, user):
        self.prompts.append((system, user))
        v = self.verdicts[self.calls] if self.calls < len(self.verdicts) else "none"
        self.calls += 1
        return Completion(text=json.dumps({"verdict": v}), model="resolve-fake", cost_usd=self.cost)


class NeverCalled:
    """The G1 assertion in Completer form: the $0 path must never reach the LLM."""
    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        raise AssertionError("the residue completer must not be called on the $0 path")


def use_store(prefix):
    d = tempfile.mkdtemp(prefix=f"ratchet-test-resolve-{prefix}-")
    os.environ["RATCHET_DATA_DIR"] = d
    return config.ensure_layout()


def seed_events(specs, root):
    """`specs` = [(session_id, line, summary, markers, confidence, repo[, mtime])] — real sessions →
    cleaned blobs → glean events with controlled summaries (the signature source), repos (the
    subject), and optional valid-times (the sitting-coalescing timeline; absent = undated)."""
    for sid, line, summary, markers, conf, repo, *rest in specs:
        cs = make_session(sid, line, repo=repo, mtime=rest[0] if rest else None)
        glean.run([cs], GleanFake([line], summaries={line: summary}, markers={line: markers},
                                  confidence=conf), model="fake", root=root)


def by_summary(root):
    return {rv.event.get("summary"): rv for rv in working_set(root)}


def edge_content(event_id, verb, claim_id, root):
    h = blobstore.latest_version(resolve.edge_id(event_id, verb, claim_id), root)
    return json.loads(blobstore.get(h, root)) if h else None


def load_golden(name):
    g = json.loads((GOLDEN_DIR / name).read_text())
    return {k: v for k, v in g.items() if not k.startswith("_")}


def _diff(expected, actual):
    lines = ["  key                                  expected              actual"]
    for k in sorted(set(expected) | set(actual)):
        e, a = expected.get(k, "∅"), actual.get(k, "∅")
        mark = "" if e == a else "   <-- MISMATCH"
        lines.append(f"  {k:<36} {str(e):<21} {str(a)}{mark}")
    return "\n".join(lines)


def check_golden(name, actual):
    golden = load_golden(name)
    assert actual == golden, f"\n{name} end-state mismatch:\n{_diff(golden, actual)}"


# === 0. UNITS: verdict coercion + candidate devaluation + empty-subject seed-only ====================

# _clean_verdict: strict — anything not an exact verdict coerces to none (abstention default).
assert _clean_verdict({"verdict": "none"}, 3) == ("none", None)
assert _clean_verdict({"verdict": "same-as-2"}, 3) == ("same-as", 2)
assert _clean_verdict({"verdict": "CONTRADICTS-1"}, 3) == ("contradicts", 1)
assert _clean_verdict({"verdict": "same-as", "candidate": 3}, 3) == ("same-as", 3)
assert _clean_verdict({"verdict": "same-as-4"}, 3) == ("none", None), "out-of-range N → none"
assert _clean_verdict({"verdict": "same-as-0"}, 3) == ("none", None), "zero N → none"
assert _clean_verdict({"verdict": "yes"}, 3) == ("none", None), "prose → none (never over-merge on garbage)"
assert _clean_verdict({}, 3) == ("none", None) and _clean_verdict(None, 3) == ("none", None)
assert _clean_verdict({"verdict": "same-as", "candidate": True}, 3) == ("none", None), "bool is not a number"

# candidate indexes: FACET_DF_MAX devalues a saturating facet (df>=2 AND over the fraction), keeps
# df=1; the rare-shingle channel reaches candidates the facet channel lost; empty subject = seed-only.
_va = {"id": "c-a", "subject": {"repos": ["mono"], "files": ["a.py"]},
       "stmt_shingles": sig.char_shingles(JJ_SEED), "stmt_entropy": 4.0}
_vb = {"id": "c-b", "subject": {"repos": ["mono"], "files": ["b.py"]},
       "stmt_shingles": sig.char_shingles(NEP), "stmt_entropy": 4.0}
_idx = resolve.build_indexes([_va, _vb])
got = candidate_ids(_idx, frozenset(), {"repo": "mono", "files": []})
assert got == set(), f"the repo facet on 2/2 active claims is DEVALUED (contributes nothing): {got}"
got = candidate_ids(_idx, frozenset(), {"repo": None, "files": ["a.py"]})
assert got == {"c-a"}, f"a df=1 file facet stays discriminative: {got}"
got = candidate_ids(_idx, sig.char_shingles(JJ_PARA), {"repo": None, "files": []})
assert got == {"c-a"}, f"the rare-shingle channel surfaces the ~0.2 paraphrase: {got}"
_only = resolve.build_indexes([_va])
got = candidate_ids(_only, frozenset(), {"repo": "mono", "files": []})
assert got == {"c-a"}, f"a df=1 facet is kept even at 100% of a 1-claim pool: {got}"
got = candidate_ids(_only, frozenset(), {"repo": None, "files": []})
assert got == set(), "an EMPTY subject key contributes nothing via subject (seed-only, §3.1 step 0)"

# _residue_user (prompt v2): candidates carry their OWN verbatim evidence — the replay audit traced
# recall AND false-accept to candidate invisibility (80-char titles, no candidate quote). A noise
# seed_quote renders VERBATIM (visibly noise); a missing one renders the explicit absence marker;
# titles render at the summary scale (RESIDUE_TITLE_MAX), past dream's 80-char clip.
_rv0 = dream.ResolvedEvent(event={"id": "e-obs", "summary": "an observation"},
                           quote="the observation's verbatim quote", span=(0, 32), session_id="s-obs")
_long_title = ("a lesson whose full statement runs well past the old eighty-character clip because "
               "claim titles are event summaries and the measured corpus runs 92-240 characters")
_noise_q = "[assistant]\n---\ntitle: a frontmatter fragment"
_u0 = resolve._residue_user(_rv0, [
    {"id": "c-noise", "title": _long_title, "why": None, "seed_quote": _noise_q},
    {"id": "c-bare", "title": "a candidate with no resolvable evidence", "why": "a synthesized why"},
])
assert f'"""{_noise_q}"""' in _u0, "a noise seed_quote renders VERBATIM — visibly noise to the matcher"
assert "(no verbatim evidence resolvable)" in _u0, "a missing seed_quote renders the explicit absence marker"
assert _long_title in _u0, "candidate titles render at the summary scale (80 cut every candidate mid-sentence)"
assert "a synthesized why" in _u0, "a synthesized why still rides the candidate line"
print("OK §0 — verdict coercion is strict-to-none; facet devaluation (df>=2 over the fraction) with the")
print("        df=1 guard; rare shingles recall the paraphrase; empty subject is seed-only; the residue")
print("        prompt shows candidate evidence verbatim (or its explicit absence).")


# === G1. the Zig/JAX/NEP-50 trio: 3 claims, 0 matured, 0 LLM calls ==================================

R1 = use_store("g1")
seed_events([("g1-s1", ZIG, ZIG, M_HI, 0.85, "taro"),
             ("g1-s2", JAXL, JAXL, M_MID, 0.85, "taro"),
             ("g1-s3", NEP, NEP, M_MID, 0.85, "taro")], R1)
fake1 = NeverCalled()
rep1 = resolve.run(fake1, model="fake", forget=False, root=R1)
pool1 = claim_pool(R1)
by_title = {c["title"]: c for c in pool1}
assert set(by_title) == {ZIG, JAXL, NEP}, "each lesson seeds its OWN claim, titled by its summary"
for c in pool1:
    e = edge_content(c["seed_event"], "corroborates", c["id"], R1)
    assert e["active"] and e["match"]["by"] == "seed", "every seed edge is by:'seed' with an audit key"
    assert c["support"] == {"events": 1, "sessions": 1}
assert working_set(R1) == [], "every event consolidated"
check_golden("resolve_g1_trio.json", {
    "claims": len(pool1),
    "matured": len(current_claims(R1)),
    "llm_calls": fake1.calls,
    "minted": rep1.n_minted,
    "corroborated": rep1.n_corroborated,
    "contradicted": rep1.n_contradicted,
    "residue_calls": rep1.n_residue_calls,
})
print("OK G1 — the v2 over-merge trio: 3 separate claims, 0 matured, ZERO LLM calls — the $0 rejection")
print("        layer settles every pair; the spurious '2 sessions' cannot form.")


# === G2. the paraphrased jj-not-git lesson across 2 repos: rare-shingle → residue → ONE claim ========

R2 = use_store("g2")
seed_events([("g2-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha"),
             ("g2-s2", JJ_PARA, JJ_PARA, M_MID, 0.85, "beta")], R2)
fake2 = ResolveFake(["same-as-1"])
rep2 = resolve.run(fake2, model="fake", forget=False, root=R2)
pool2 = claim_pool(R2)
assert len(pool2) == 1 and fake2.calls == 1, "one claim, one residue call"
claim2 = pool2[0]
para_id = claim2["cites"][0] if claim2["cites"][0] != claim2["seed_event"] else claim2["cites"][1]
e2 = edge_content(para_id, "corroborates", claim2["id"], R2)
assert e2["match"]["by"] == "llm" and e2["match"]["candidates_shown"] == [claim2["id"]], \
    "the merge edge carries the audit match key (by:llm + candidates_shown)"
assert e2["match"]["prompt_version"] == resolve.PROMPT_VERSION and e2["match"]["model"] == "fake"
system2, user2 = fake2.prompts[0]
assert "NONE is the expected answer" in system2 and "same-as-N" in system2, \
    "the residue prompt states the abstention default"
assert f"[1] id={claim2['id']}" in user2, "candidates render as a numbered list with their statements"
assert f'quote (verbatim): """{claim2["title"]}"""' in user2, \
    "the candidate's seed quote rides the residue prompt — evidence on BOTH sides (prompt v2)"
assert claim2["seed_quote"] == claim2["title"], \
    "the fold derives seed_quote from the seed event's re-validated bytes (title == summary == line here)"
# the evidence entries fold in dream's shape — review.resolve_evidence reads them unchanged.
from ratchet import review  # noqa: E402
resolved = review.resolve_evidence(claim2, R2)
assert len(resolved) == 2 and all(r["verified"] for r in resolved), \
    "the folded evidence re-validates through review.resolve_evidence unchanged"
assert {r["quote"] for r in resolved} == {JJ_SEED, JJ_PARA}, "both tellings anchor to verbatim bytes"
check_golden("resolve_g2_paraphrase.json", {
    "claims": 1,
    "support_events": claim2["support"]["events"],
    "support_sessions": claim2["support"]["sessions"],
    "scope": claim2["scope"],
    "repos": claim2["subject"]["repos"],
    "matured": len(current_claims(R2)),
    "llm_calls": fake2.calls,
    "stmt_sim": round(e2["match"]["stmt_sim"], 4),
    "by": e2["match"]["by"],
})

# idempotent re-tick: everything consolidated → zero events, zero LLM work, nothing re-written.
n_blobs_before = sum(1 for _ in blobstore.iter_meta(R2))
rerun = resolve.run(NeverCalled(), model="fake", forget=False, root=R2)
assert rerun.n_events == 0 and rerun.processed == 0, "a re-tick over a drained store no-ops"
assert sum(1 for _ in blobstore.iter_meta(R2)) == n_blobs_before, "and writes nothing"
print("OK G2 — the ~0.2 paraphrase across 2 repos: rare shingles surface the candidate, the residue call")
print("        merges it, scope derives cross-cutting, 2-session support matures; audit key on the edge;")
print("        re-tick no-ops.")


# === G3. retraction IS the split: fold(claim − edge) == fold(had-never-matched) ======================

R3 = use_store("g3")
seed_events([("g3-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha")], R3)
resolve.run(NeverCalled(), model="fake", forget=False, root=R3)
fold_never = claim_pool(R3)[0]                          # the claim as if the paraphrase never matched
seed_events([("g3-s2", JJ_PARA, JJ_PARA, M_MID, 0.85, "beta")], R3)
fake3 = ResolveFake(["same-as-1"])
resolve.run(fake3, model="fake", forget=False, root=R3)
fold_merged = claim_pool(R3)[0]
assert fold_merged["support"] == {"events": 2, "sessions": 2} and fold_merged["scope"] == "cross-cutting"
matured_before = len(current_claims(R3))
assert matured_before == 1, "corroborated across 2 fresh sessions → mature"
para3 = [c for c in fold_merged["cites"] if c != fold_merged["seed_event"]][0]
resolve.retract_edge(para3, "corroborates", fold_merged["id"], root=R3, run_id="g3-retract")
fold_after = claim_pool(R3)[0]
assert fold_after == fold_never, \
    "the testable §2.2 property: fold(claim after retraction) == fold(claim had the event never matched)"
assert not edge_content(para3, "corroborates", fold_merged["id"], R3)["active"], \
    "the edge blob survives its own retraction (active:false — the audit history stays)"
check_golden("resolve_g3_retraction.json", {
    "support_before": fold_merged["support"]["sessions"],
    "support_after": fold_after["support"]["sessions"],
    "matured_before": matured_before,
    "matured_after": len(current_claims(R3)),
    "scope_before": fold_merged["scope"],
    "scope_after": fold_after["scope"],
    "fold_equals_never_matched": fold_after == fold_never,
})
print("OK G3 — retraction is the split: support decrements, scope and signature shrink, the claim")
print("        un-matures, and the fold is byte-exactly the never-matched fold.")


# === G4. the measured trap: different lessons at ~0.35 → residue answers NONE → 2 claims =============

G4_SPECS = [("g4-s1", PYTEST, PYTEST, M_HI, 0.85, "gamma"),
            ("g4-s2", RUFF, RUFF, M_MID, 0.85, "gamma")]
R4 = use_store("g4")
seed_events(G4_SPECS, R4)
fake4 = ResolveFake(["none"])
rep4 = resolve.run(fake4, model="fake", forget=False, root=R4)
pool4 = claim_pool(R4)
assert {c["title"] for c in pool4} == {PYTEST, RUFF}, "an honest NONE keeps the two lessons apart"
assert fake4.calls == 1, "the shared vocabulary DID reach the residue call (the trap was live)"

# the WRONG verdict: a fake that merges them anyway — the audit trail must exist for the review card.
R4b = use_store("g4-wrong")
seed_events(G4_SPECS, R4b)
fake4b = ResolveFake(["same-as-1"])
resolve.run(fake4b, model="fake", forget=False, root=R4b)
pool4b = claim_pool(R4b)
assert len(pool4b) == 1, "the wrong merge fused the two lessons (this is what the audit card catches)"
wrong = pool4b[0]
ruff_id = [c for c in wrong["cites"] if c != wrong["seed_event"]][0]
e4 = edge_content(ruff_id, "corroborates", wrong["id"], R4b)
assert e4["match"]["by"] == "llm" and e4["match"]["candidates_shown"] == [wrong["id"]] \
    and e4["match"]["prompt_version"] == resolve.PROMPT_VERSION, \
    "the wrong merge carries by:llm + candidates_shown — the reviewer sees exactly what the model saw"
check_golden("resolve_g4_trap.json", {
    "stmt_sim": round(sim_of(PYTEST, RUFF), 4),
    "honest_claims": len(pool4),
    "honest_llm_calls": fake4.calls,
    "honest_minted": rep4.n_minted,
    "wrong_claims": len(pool4b),
    "wrong_by": e4["match"]["by"],
    "wrong_candidates_shown": len(e4["match"]["candidates_shown"]),
    "wrong_stmt_sim": round(e4["match"]["stmt_sim"], 4),
})
print("OK G4 — the measured trap: shared vocabulary reaches the residue, an honest NONE keeps 2 claims;")
print("        a wrong same-as leaves by:llm + candidates_shown on the edge — the audit trail exists.")


# === G5. reject-merge: ONE compound decision — retract + reopen + pair-block, epoch past the marker ==

R5 = use_store("g5")
seed_events([("g5-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "delta"),
             ("g5-s2", JJ_PARA, JJ_PARA, M_MID, 0.85, "delta")], R5)
fake5 = ResolveFake(["same-as-1"])
resolve.run(fake5, model="fake", forget=False, root=R5)
c5 = claim_pool(R5)[0]
eid5 = [c for c in c5["cites"] if c != c5["seed_event"]][0]
assert c5["support"]["sessions"] == 2 and working_set(R5) == []

resolve.reject_merge(eid5, edge=resolve.edge_id(eid5, "corroborates", c5["id"]),
                     reason="different lessons", root=R5)
after = {c["id"]: c for c in claim_pool(R5)}[c5["id"]]
assert after["support"] == {"events": 1, "sessions": 1}, "the edge fold reads the decision as retraction"
reopened = {rv.id for rv in working_set(R5)}
assert reopened == {eid5}, "the working-set fold reads the SAME decision as reopen"

blk5 = resolve.ResolveBlock(NeverCalled(), model="fake", forget=False)
items5 = list(blk5.items(R5))
assert blk5.key(items5[0]) == f"{eid5}#e1", "the done-marker key carries the epoch — the driver re-admits"
fake5b = NeverCalled()
rep5 = resolve.run(fake5b, model="fake", forget=False, root=R5)
assert rep5.processed == 1 and rep5.skipped == 0, "the reopened event re-enters past its old marker"
pool5 = claim_pool(R5)
assert len(pool5) == 2 and fake5b.calls == 0, \
    "the pair-block removed the only candidate → the event seeds its own claim at $0"
new5 = [c for c in pool5 if c["id"] != c5["id"]][0]
assert new5["seed_event"] == eid5 and new5["id"] == dream.mint_takeaway_id(eid5)
assert not any(e["event_id"] == eid5 for c in pool5 if c["id"] == c5["id"]
               for e in [{"event_id": x} for x in c["cites"]]), "the pair never re-forms"
check_golden("resolve_g5_reject_merge.json", {
    "support_after_reject": after["support"]["sessions"],
    "reopened": sorted(reopened) == sorted([eid5]),
    "epoch_key_suffix": blk5.key(items5[0]).endswith("#e1"),
    "reprocessed": rep5.processed,
    "claims_after": len(pool5),
    "rerun_llm_calls": fake5b.calls,
    "pair_reformed": eid5 in {c["id"]: c for c in pool5}[c5["id"]]["cites"],
})
print("OK G5 — reject-merge: one compound decision retracts the edge, reopens the event (epoch-keyed")
print("        past the done-marker), blocks the pair forever; re-resolve seeds its own claim, 0 calls.")


# === 6. BUDGET DEFERRAL: paid residue work defers unmarked; $0 events complete (§7.2) ================

R6 = use_store("budget")
seed_events([("bd-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha")], R6)
resolve.run(NeverCalled(), model="fake", forget=False, root=R6)      # tick 1: the claim exists
seed_events([("bd-s2", JJ_PARA, JJ_PARA, M_HI, 0.85, "beta"),        # residue-bound (paid)
             ("bd-s3", NEP, NEP, M_MID, 0.85, "alpha")], R6)         # $0 (novel)
fake6 = ResolveFake(["same-as-1"])
rep6 = resolve.run(fake6, model="fake", max_usd=0.0, forget=False, root=R6)
assert fake6.calls == 0 and rep6.n_deferred == 1, "the residue event DEFERS under a $0 cap — no call"
assert rep6.n_minted == 1, "the $0 event still completes — paid work never starves free work"
para6 = by_summary(R6).get(JJ_PARA)
assert para6 is not None, "the deferred event is still in the working set (no verdict, no decision)"
assert blobstore.latest_decision(para6.id, R6) is None, "…and carries no consolidated decision"
done6 = block.done_index("resolve", R6)
assert not any(k[0] == para6.id for k in done6), "…and NO processed marker — it retries next tick"
fake6b = ResolveFake(["same-as-1"])
rep6b = resolve.run(fake6b, model="fake", max_usd=1.0, forget=False, root=R6)
assert fake6b.calls == 1 and rep6b.n_corroborated == 1, "next tick, under budget, the deferral resolves"
assert working_set(R6) == []
print("OK §6 — budget deferral: at the cap a residue event raises out unmarked (retried next tick)")
print("        while the $0 event completes; the next funded tick drains the deferral.")


# === 7. --reset-v2: retire every live v2 takeaway, reopen every consolidated event, idempotent =======

class RouteNew:
    def __call__(self, system, user):
        return Completion(text=json.dumps({"decision": "new", "takeaway_id": None}),
                          model="route-fake", cost_usd=0.0005)

class SynthFake:
    def __call__(self, system, user):
        import re as _re
        m = _re.search(r'quote: """(.*?)"""', user, _re.S)
        title = " ".join((m.group(1) if m else "").split()[:4]) or "a takeaway"
        return Completion(text=json.dumps({"title": title, "why": "a durable principle",
                                           "confidence": 0.8}), model="synth-fake", cost_usd=0.01)


R7 = use_store("reset-v2")
seed_events([("rv-s1", ZIG, ZIG, M_MID, 0.85, "taro"),
             ("rv-s2", NEP, NEP, M_MID, 0.85, "taro")], R7)
dream.run(RouteNew(), SynthFake(), route_model="fake", synth_model="fake", forget=False, root=R7)
assert len(dream.catalog(R7)) == 2 and working_set(R7) == [], "a real v2 store: takeaways + consolidated events"

retired_d, reopened_d = resolve.reset_v2(R7, dry_run=True)
assert len(retired_d) == 2 and len(reopened_d) == 2
assert len(dream.catalog(R7)) == 2 and working_set(R7) == [], "--dry-run writes NOTHING"

retired, reopened = resolve.reset_v2(R7)
assert sorted(retired) == sorted(retired_d) and sorted(reopened) == sorted(reopened_d)
assert dream.catalog(R7) == [], "every v2 takeaway retired (blob + history stay)"
assert {rv.id for rv in working_set(R7)} == set(reopened), "every consolidated event reopened"
retire_dec = blobstore.latest_decision(retired[0], R7)
assert retire_dec["verb"] == "retire" and "ADR-0028" in retire_dec["reason"], "the reason cites ADR-0028"
assert resolve.reset_v2(R7) == ([], []), "a second reset finds nothing to do (idempotent)"

rep7 = resolve.run(NeverCalled(), model="fake", forget=False, root=R7)
assert rep7.n_minted == 2 and working_set(R7) == [], "resolve drains the reopened backlog into claims"
assert len(claim_pool(R7)) == 2 and dream.catalog(R7) == []
assert resolve.reset_v2(R7, dry_run=True) == ([], []), \
    "a reset AFTER the drain reopens nothing — resolve-consolidated events are v3's own verdicts, " \
    "not v2 damage (producer.stage guard)"
print("OK §7 — reset-v2: dry-run previews, the real pass retires 2 takeaways (reason cites ADR-0028)")
print("        + reopens 2 events, a second pass no-ops, and resolve re-consolidates them into claims;")
print("        a reset after the drain reopens nothing (producer.stage guard).")


# === 8. FORGET (§7.3): resolve-stage residency, restarted by reopen, wall-clock-bounded ==============

R8 = use_store("forget")
seed_events([("fg-low", "an incidental throwaway line nobody will need again later",
              "an incidental throwaway line nobody will need again later", {}, 0.1, "taro"),
             ("fg-re", "another incidental line that was torn out of a merge by review",
              "another incidental line that was torn out of a merge by review", {}, 0.1, "taro")], R8)
low8, re8 = (by_summary(R8)["an incidental throwaway line nobody will need again later"],
             by_summary(R8)["another incidental line that was torn out of a merge by review"])
for rid in ("fr-1", "fr-2", "fr-3"):                    # three resolve-stage runs postdating both events
    block.write_processed("resolve", "dummy-evt", (("prompt_version", resolve.PROMPT_VERSION),
                                                   ("model", "fake")),
                          n_outputs=0, cost_usd=0.0, run_id=rid, extra={}, root=R8)
assert resolve.forget(R8, tau=2, run_id="fg-1") == [], \
    "FORGET_MIN_DAYS holds: cycle count alone never stales a fresh event (cheap ticks can't accelerate)"
# a reopen AFTER those runs restarts the residency clock: only the never-reopened event may stale.
resolve.reject_merge(re8.id, edge=f"{re8.id}|corroborates|t-x", reason="test reopen", root=R8)
staled8 = resolve.forget(R8, tau=2, min_days=0.0, run_id="fg-2")
assert staled8 == [low8.id], \
    f"residency counts only runs postdating the reopen — the reopened event survives: {staled8}"
assert {rv.id for rv in working_set(R8)} == {re8.id}
print("OK §8 — forget: the wall-clock minimum blocks fresh staleing outright; a reopen restarts the")
print("        residency clock so a freshly reopened straggler is never mass-staled.")


# === 9. THE TRUSTED VIEW: accept facet ∧ mature ∧ not retired (§5) ===================================

R9 = use_store("view")
seed_events([("hv-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha"),
             ("hv-s2", JJ_PARA, JJ_PARA, M_MID, 0.85, "beta"),
             ("hv-s3", NEP, NEP, M_MID, 0.85, "alpha")], R9)
resolve.run(ResolveFake(["same-as-1"]), model="fake", forget=False, root=R9)
mature9 = current_claims(R9)
assert len(mature9) == 1 and mature9[0]["support"]["sessions"] == 2, "one claim matured at the single bar"
assert high_confidence_view(R9) == [], "mature but UN-accepted → not trusted (review is the gate)"
review._record("accept", mature9[0]["id"], R9, reviewer="sulin")
hv = high_confidence_view(R9)
assert [c["id"] for c in hv] == [mature9[0]["id"]], "accepted ∧ mature → trusted"
review._record("retire", mature9[0]["id"], R9, reviewer="sulin")
assert high_confidence_view(R9) == [], "a retire drops it from the view (invalidate-don't-delete)"
# the ACTIVE view is a predicate, not a status: a zero-evidence claim folds out of candidacy.
now9 = config.now()
vt9 = dream._session_valid_times(R9)
assert all(is_active(c, now=now9, valid_times=vt9) for c in claim_pool(R9)), "fresh claims are active"
assert not is_active({"sessions_seen": [], "contradiction_evidence": []}, now=now9, valid_times=vt9), \
    "a claim with no live evidence folds out of the ACTIVE view (the pool's drain)"
print("OK §9 — high_confidence_view = accepted ∧ mature ∧ not retired; the ACTIVE view drains")
print("        evidence-less claims out of candidacy.")


# === 10. SAME-SESSION GATE: a same-session pair never reaches the LLM; the escape hatch restores it ==

# Two paraphrased tellings from ONE session (same sid → same session_id on both events; the two
# transcripts differ in bytes, so both events exist). Without the gate this is a residue call —
# the escape-hatch twin below proves it — but a same-session yes buys zero distinct-session support.
SS_SPECS = [("ss-1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha"),
            ("ss-1", JJ_PARA, JJ_PARA, M_MID, 0.85, "alpha")]
R10 = use_store("same-session")
seed_events(SS_SPECS, R10)
fake10 = NeverCalled()
rep10 = resolve.run(fake10, model="fake", forget=False, root=R10)
pool10 = claim_pool(R10)
assert {c["title"] for c in pool10} == {JJ_SEED, JJ_PARA}, "each telling seeds its OWN claim"
assert fake10.calls == 0 and rep10.n_residue_calls == 0, \
    "the same-session candidate is auto non-match — the LLM is never consulted"
assert rep10.n_minted == 2 and working_set(R10) == [], "both events consolidated at $0"

R10b = use_store("same-session-adj")
seed_events(SS_SPECS, R10b)
fake10b = ResolveFake(["same-as-1"])
rep10b = resolve.run(fake10b, model="fake", forget=False, same_session_adjudicate=True, root=R10b)
assert fake10b.calls == 1 and rep10b.n_corroborated == 1, \
    "--same-session-adjudicate restores adjudication for the same pair (the escape hatch)"
c10b = claim_pool(R10b)[0]
assert c10b["support"] == {"events": 2, "sessions": 1}, \
    "and the merge it buys adds NO session support — the gate's whole rationale"
assert current_claims(R10b) == [], "a same-session merge cannot mature a claim"
print("OK §10 — same-session gate: the paraphrase pair seeds two claims with ZERO LLM calls; the")
print("        escape hatch re-adjudicates it, and the merge it buys adds no distinct-session support.")


# === 11. EXACT-DUP FAST PATH: identical summaries corroborate by:'det' — no LLM, same-session too ====

R11 = use_store("dup")
seed_events([("dup-1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha"),
             ("dup-1", DUP_LINE, JJ_SEED, M_MID, 0.85, "alpha")], R11)   # same summary, new bytes
fake11 = NeverCalled()
rep11 = resolve.run(fake11, model="fake", forget=False, root=R11)
pool11 = claim_pool(R11)
assert len(pool11) == 1 and fake11.calls == 0, "the duplicate folds into its claim at $0 — no twin"
c11 = pool11[0]
assert c11["support"] == {"events": 2, "sessions": 1}, "same-session dedup adds an event, not a session"
dup_eid = [e for e in c11["cites"] if e != c11["seed_event"]][0]
e11 = edge_content(dup_eid, "corroborates", c11["id"], R11)
assert e11["match"]["by"] == "det" and e11["match"]["model"] is None \
    and e11["match"]["candidates_shown"] == [], \
    "the dup edge is by:'det' — deterministic, nothing shown to any model"
assert e11["match"]["stmt_sim"] == 1.0, "shingle-set identity scores 1.0 on the audit key"
assert rep11.n_corroborated == 1 and rep11.n_residue_calls == 0
assert current_claims(R11) == [], "dedup never fakes maturity — one session is one session"
print("OK §11 — exact-dup fast path: the overlapping-chunk duplicate corroborates deterministically")
print("        (by:'det', stmt_sim 1.0, zero LLM), same-session included; maturity is untouched.")


# === 12. NOISE-QUOTE GATE: a thin event seeds flagged; a thin candidate leaves adjudication ==========

R12 = use_store("noise")
seed_events([("nz-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha")], R12)
resolve.run(NeverCalled(), model="fake", forget=False, root=R12)        # the healthy claim exists
# a NOISE-quoted event with a HEALTHY summary in the residue band vs the healthy claim: without the
# gate this is a residue call; with it the event is seed-only — it never corroborates the claim.
seed_events([("nz-s2", NOISE, JJ_PARA, M_MID, 0.85, "beta")], R12)
fake12 = NeverCalled()
rep12 = resolve.run(fake12, model="fake", forget=False, root=R12)
assert fake12.calls == 0 and rep12.n_minted == 1, "the thin event mints at $0 — never a corroboration"
pool12 = {c["title"]: c for c in claim_pool(R12)}
assert set(pool12) == {JJ_SEED, JJ_PARA}, "two claims: the healthy one untouched, the thin one seeded"
thin12, healthy12 = pool12[JJ_PARA], pool12[JJ_SEED]
assert thin12["thin_evidence"] is True and thin12["seed_quote"] == NOISE, \
    "the thin seed folds thin_evidence: true (derived, not stored) — the review badge"
assert healthy12["thin_evidence"] is False and healthy12["support"] == {"events": 1, "sessions": 1}
# candidate side (the measured [16] failure): a healthy cross-session paraphrase arrives; the thin
# claim sits in its residue band (fixture-asserted) but leaves adjudication — only the healthy
# claim is shown, and the pair adjudicates exactly as G2 did (the regression pin).
seed_events([("nz-s3", JJ_PARA2, JJ_PARA2, M_MID, 0.85, "gamma")], R12)
fake12b = ResolveFake(["same-as-1"])
rep12b = resolve.run(fake12b, model="fake", forget=False, root=R12)
assert fake12b.calls == 1 and rep12b.n_corroborated == 1, \
    "the healthy pair still adjudicates — the gate drops thin candidates, never healthy ones"
merged12 = [c for c in claim_pool(R12) if c["title"] == JJ_SEED][0]
para2_eid = [e for e in merged12["cites"] if e != merged12["seed_event"]][0]
e12 = edge_content(para2_eid, "corroborates", healthy12["id"], R12)
assert e12["match"]["by"] == "llm" and e12["match"]["candidates_shown"] == [healthy12["id"]], \
    "the thin claim never entered the residue prompt — the model saw the healthy candidate only"
assert merged12["support"] == {"events": 2, "sessions": 2}, "cross-session corroboration lands"
assert [c for c in claim_pool(R12) if c["title"] == JJ_PARA][0]["support"]["events"] == 1, \
    "the thin claim neither corroborates nor gets corroborated"
print("OK §12 — noise-quote gate: the thin event seeds a flagged claim at $0 (never corroborating the")
print("        healthy one); the thin candidate leaves adjudication while the healthy cross-session")
print("        pair merges exactly as before (G2 regression pin).")


# === 13. --audit-thin: the read-only listing of noise-seeded claims ==================================

import contextlib  # noqa: E402
import io  # noqa: E402

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    resolve.main(["--audit-thin"])                     # env still points at R12 (use_store set it)
out13 = buf.getvalue()
assert "1 live claim(s)" in out13, f"exactly the one noise seed lists:\n{out13}"
assert thin12["id"] in out13 and JJ_PARA in out13 and repr(NOISE) in out13, \
    "the listing carries id, title, and the failing quote rendered"
assert healthy12["id"] not in out13 and merged12["id"] not in out13, "healthy claims never list"
print("OK §13 — --audit-thin lists the noise-seeded claim (id, title, quote verbatim) and nothing else.")


# === 14. SITTING COALESCING: a /clear-split afternoon is ONE sitting, never 2-session maturity =======
# The cheapest fake-maturity path (ADR-0028 backlog, closed): a /clear or crash-restart writes 2+
# transcript files, so one sitting's work reads as 2 "distinct sessions" and matures a claim at the
# ~2-session bar. `dream.coalesce_sessions` groups same-repo sessions whose valid-times sit within
# COALESCE_HOURS into one sitting — for the COUNT (net_entrenchment, support/contradiction symmetric)
# and, via the SAME helper (`dream.same_sitting`), for the cascade's gate.

from datetime import datetime, timedelta, timezone  # noqa: E402

_NOW14 = datetime.now(timezone.utc)


def ago(**kw):
    return (_NOW14 - timedelta(**kw)).isoformat()


T_4H, T_2H, T_3D, T_NOW = ago(hours=4), ago(hours=2), ago(days=3), ago()

# -- units: the grouping itself (pure, no store) --
_vt = {"a": T_4H, "b": T_2H, "c": T_3D, "u": None}
_rp = {"a": "alpha", "b": "alpha", "c": "alpha", "u": "alpha"}
g = dream.coalesce_sessions({"a", "b", "c", "u"}, _vt, _rp)
assert sorted(map(sorted, g)) == [["a", "b"], ["c"], ["u"]], \
    f"same-repo 2h-apart pair is ONE sitting; 3-days-apart and undated stay distinct: {g}"
g = dream.coalesce_sessions({"a", "b"}, _vt, {"a": "alpha", "b": "beta"})
assert sorted(map(sorted, g)) == [["a"], ["b"]], "different repos 2h apart stay distinct (context switch)"
g = dream.coalesce_sessions({"a", "b"}, _vt, {"a": "alpha", "b": None})
assert sorted(map(sorted, g)) == [["a"], ["b"]], "an unhomed session never coalesces (recall-safe)"
_chain_vt = {"p": ago(hours=20), "q": ago(hours=10), "r": ago(hours=0)}
_chain_rp = {s: "alpha" for s in _chain_vt}
assert len(dream.coalesce_sessions(set(_chain_vt), _chain_vt, _chain_rp)) == 1, \
    "the greedy chain: adjacent 10h gaps merge even though the sitting spans 20h"
assert dream.coalesce_sessions({"a", "b"}, _vt, _rp, hours=0) == [["a"], ["b"]], \
    "hours=0 is OFF — every session its own group (the escape hatch)"
assert dream._sitting_valid_time(["a", "b"], _vt) == T_2H, "a sitting's valid-time is its LATEST member's"
assert dream._sitting_valid_time(["u"], _vt) is None, "a lone undated session stays undated (weight 1.0)"

# same_sitting — the gate's helper IS the count's helper.
assert dream.same_sitting("b", {"a"}, _vt, _rp), "2h same-repo → same sitting"
assert not dream.same_sitting("c", {"a", "b"}, _vt, _rp), "3 days away → its own sitting"
assert not dream.same_sitting("b", {"a"}, _vt, {"a": "alpha", "b": "beta"}), "repo jump → distinct"
assert dream.same_sitting("a", {"a"}, _vt, {}, hours=0), "exact same id is same-sitting at ANY hours"
assert not dream.same_sitting("b", {"a"}, _vt, _rp, hours=0), "hours=0 restores the plain same-session gate"

# net_entrenchment counts SITTINGS, recency reads each group's LATEST valid-time, symmetric for
# contradictions (ADR-0012), and a view with no _session_repos (a v2 takeaway) never coalesces.
_now14 = config.now()
_v = {"sessions_seen": ["a", "b"], "contradiction_evidence": [], "_session_repos": _rp}
assert dream.net_entrenchment(_v, _now14, valid_times=_vt) == dream.recency_weight(T_2H, _now14), \
    "one split sitting counts ONCE, weighted by its latest valid-time"
assert dream.net_entrenchment(_v, _now14, valid_times=_vt, coalesce_hours=0) == \
    sum(dream.recency_weight(_vt[s], _now14) for s in ["a", "b"]), \
    "coalesce_hours=0 restores the per-session sum byte-exact"
_vc = {"sessions_seen": ["c"], "contradiction_evidence": [{"session_id": "a"}, {"session_id": "b"}],
       "_session_repos": _rp}
assert dream.net_entrenchment(_vc, _now14, valid_times=_vt) == \
    dream.recency_weight(T_3D, _now14) - dream.recency_weight(T_2H, _now14), \
    "contradicting sessions coalesce identically — a split sitting can't fake a 2-session overturn"
_v2 = {"sessions_seen": ["a", "b"], "contradiction_evidence": []}
assert dream.net_entrenchment(_v2, _now14, valid_times=_vt) == \
    sum(dream.recency_weight(_vt[s], _now14) for s in ["a", "b"]), \
    "no _session_repos (a v2 takeaway) → nothing coalesces — v2 counting is untouched"
print("OK §14a — coalesce_sessions: same-repo-within-window merges (greedy chain), repo jumps and")
print("         undated/unhomed sessions stay distinct, hours=0 is off; the count and the gate share")
print("         one helper; contradictions group symmetrically; v2 takeaways never coalesce.")

# -- the cascade gate: a split-sitting pair never reaches the LLM (the same-session gate, widened) --
ST_SPECS = [("st-1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha", T_4H),
            ("st-2", JJ_PARA, JJ_PARA, M_MID, 0.85, "alpha", T_2H)]
R14 = use_store("sitting-gate")
seed_events(ST_SPECS, R14)
fake14 = NeverCalled()
rep14 = resolve.run(fake14, model="fake", forget=False, root=R14)
assert fake14.calls == 0 and rep14.n_minted == 2 and working_set(R14) == [], \
    "the split-sitting candidate is auto non-match — the LLM is never consulted (the gate, coalesced)"
assert {c["title"] for c in claim_pool(R14)} == {JJ_SEED, JJ_PARA}, "each telling seeds its OWN claim"

# -- the count: even a merge bought via the escape hatch adds no distinct-SITTING support --
R14b = use_store("sitting-count")
seed_events(ST_SPECS, R14b)
fake14b = ResolveFake(["same-as-1"])
rep14b = resolve.run(fake14b, model="fake", forget=False, same_session_adjudicate=True, root=R14b)
assert fake14b.calls == 1 and rep14b.n_corroborated == 1, "the escape hatch restores adjudication"
c14 = claim_pool(R14b)[0]
assert c14["support"] == {"events": 2, "sessions": 2}, \
    "the RAW audit count still says 2 sessions (like net_sessions, kept for the human)"
vt14 = dream._session_valid_times(R14b)
assert c14["_session_repos"] == {"st-1": "alpha", "st-2": "alpha"}, "the fold derives the repo map"
assert len(dream.coalesce_sessions(c14["sessions_seen"], vt14, c14["_session_repos"])) == 1, \
    "…but the two transcripts are ONE sitting"
now14 = config.now()
assert dream.net_entrenchment(c14, now14, valid_times=vt14) == dream.recency_weight(T_2H, now14), \
    "support counts one sitting, weighted by the sitting's latest valid-time"
assert current_claims(R14b) == [], "a split sitting can NOT mature a claim — the fake-maturity path is closed"
assert [c["id"] for c in current_claims(R14b, coalesce_hours=0)] == [c14["id"]], \
    "--coalesce-hours 0 restores the old per-session counting (the claim matures again)"

# -- multi-day recurrence still matures (the existing behavior, now pinned WITH dates) --
R14c = use_store("sitting-days")
seed_events([("st-1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha", T_3D),
             ("st-2", JJ_PARA, JJ_PARA, M_MID, 0.85, "alpha", T_NOW)], R14c)
fake14c = ResolveFake(["same-as-1"])
resolve.run(fake14c, model="fake", forget=False, root=R14c)
assert fake14c.calls == 1, "3 days apart is NOT one sitting — the pair adjudicates normally"
assert len(current_claims(R14c)) == 1, "same-repo recurrence across days matures (2 real sittings)"

# -- same-day cross-repo work stays distinct (a repo jump is a genuine context switch) --
R14d = use_store("sitting-repos")
seed_events([("st-1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha", T_4H),
             ("st-2", JJ_PARA, JJ_PARA, M_MID, 0.85, "beta", T_2H)], R14d)
fake14d = ResolveFake(["same-as-1"])
resolve.run(fake14d, model="fake", forget=False, root=R14d)
assert fake14d.calls == 1, "different repos 2h apart adjudicate — no gate"
assert len(current_claims(R14d)) == 1, "and the merge matures: two repos = two genuine contexts"
print("OK §14b — the /clear-split sitting: the widened gate settles the pair at $0; a hatch-bought")
print("         merge counts ONE sitting and cannot mature (hours=0 restores the old count); the same")
print("         lesson across days or across repos still matures exactly as before.")


print("\nall resolve tests passed.")
