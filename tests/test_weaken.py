"""weaken-path tests: the SYMMETRIC contradiction half of dream (ADR-0012), exercised OFFLINE with FAKE
Completers (no network, no API key) so the suite is deterministic. dream was STRENGTHEN-only — a takeaway
could accrue support but never be demoted by contradicting evidence. The weaken path adds ExpeL's downvote
in dream's additive-sufficient-statistic style, WITHOUT ever auto-deleting (the field's cautionary
anti-pattern is mem0's hard-DELETE on a contradiction). The load-bearing checks:

  ROUTE COERCION — `_clean_route`/`route` gain a 4th verdict: a `weaken` of a KNOWN catalog id passes
    through; a `weaken` of an UNKNOWN/null id → `noop` (NEVER mint a 'negative' takeaway), ASYMMETRIC to a
    `strengthen` of an unknown id → `new`; the list-number → id mapping works for weaken too.
  WEAKEN_SUPPORT — NO LLM (cost 0.0 always); appends the contradicting event to contradicted_by/
    contradiction_evidence IDEMPOTENTLY (dedup by event_id → a re-weaken is a byte-identical no-op, so
    sessions can't inflate); recomputes the contradictions BIRCH stat; leaves support/why/title UNTOUCHED.
  NET DEMOTION (the golden scenario) — A new → B strengthen (mature) → C weaken un-graduates the takeaway
    by NET distinct-session entrenchment (support.sessions − contradictions.sessions < the bar): it LEAVES
    current_takeaways, APPEARS in contradicted_takeaways, STAYS in catalog (never deleted), the contradiction
    evidence is span-verified. Expected-vs-actual against a committed golden file with a legible diff.

Run: `python tests/test_weaken.py`."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-weaken-")

from ratchet import blobstore, chunk, config, dream, glean  # noqa: E402
from ratchet.completer import Completion  # noqa: E402
from ratchet.dream import (  # noqa: E402
    apply, catalog, contradicted_takeaways, current_takeaways, net_sessions, route, synthesize_new,
    takeaway_content, update_contradictions, working_set, _clean_route, _contradiction_stats)

GOLDEN = Path(__file__).resolve().parent / "golden" / "weaken_end_state.json"

# Three durable lines across THREE distinct sessions: A and B are the same lesson (B strengthens A's
# takeaway); C CONTRADICTS it. Multibyte filler in the session body forces byte≠char offsets so the
# contradiction span math is genuinely exercised end to end.
LESSON_A = "always commit with jj and never use git for version control"
LESSON_B = "remember to commit with jj, never git, for version control here"
CONTRA_C = "actually git is required here because the remote tooling rejects jj pushes outright"
LESSON_D = "stick with jj for commits; keep git off this workflow entirely from now"

M_MID = {"surprise": 0.2, "insight": 0.7}
M_HI = {"surprise": 0.9, "insight": 0.3}     # a contradiction is a surprise — already top-of-queue salience


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}


def make_session(sid, line):
    records = [rec("u0", None, "user", message={"role": "user", "content": f"session {sid} kickoff"})]
    parent = "u0"
    for i in range(4):
        body = f"step {i}: " + ("λ wörk ✓ " * 20)
        if i == 2:
            body = f"step 2: {line} — " + ("λ wörk ✓ " * 20)
        records.append(rec(f"{sid}a{i}", parent, "assistant", message=amsg(f"{sid}M{i}", body)))
        parent = f"{sid}a{i}"
    blob = "\n".join(json.dumps(r) for r in records) + "\n"
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid, origin_ref={"session_id": sid})
    cs, _, _ = chunk.materialize(raw_h, budget=600)
    return cs


class GleanFake:
    def __init__(self, lines, *, markers=None, confidence=0.85):
        self.lines, self.markers, self.confidence = lines, markers or {}, confidence

    def __call__(self, system, user):
        cands = [{"quote": ln, "summary": f"machine summary of: {ln[:24]}",
                  "markers": self.markers.get(ln, M_MID), "confidence": self.confidence} for ln in self.lines]
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class SynthFake:
    """Titles the takeaway from the observation's own quote (first words) — a genuine trust chain. `calls`
    counts LLM invocations so the NO-LLM weaken path is checkable."""
    def __init__(self, *, why="the durable underlying principle worth keeping", confidence=0.8, cost=0.01):
        self.why, self.confidence, self.cost, self.calls = why, confidence, cost, 0

    def __call__(self, system, user):
        self.calls += 1
        m = re.search(r'quote: """(.*?)"""', user, re.S)
        quote = m.group(1) if m else ""
        title = " ".join(quote.split()[:4]) or "a takeaway"
        return Completion(text=json.dumps({"title": title, "why": self.why, "confidence": self.confidence}),
                          model="synth-fake", cost_usd=self.cost)


class EchoRoute:
    """Echoes a FIXED route verdict — drives an explicit per-event decision through the REAL route() path
    (incl. the list-number→id mapping + _clean_route coercion)."""
    def __init__(self, decision=None, takeaway_id=None, *, raw=None, cost=0.0007):
        self.decision, self.takeaway_id, self.raw, self.cost, self.calls = decision, takeaway_id, raw, cost, 0

    def __call__(self, system, user):
        self.calls += 1
        text = self.raw if self.raw is not None else json.dumps(
            {"decision": self.decision, "takeaway_id": self.takeaway_id})
        return Completion(text=text, model="route-fake", cost_usd=self.cost)


def use_store(prefix):
    d = tempfile.mkdtemp(prefix=f"ratchet-test-weaken-{prefix}-")
    os.environ["RATCHET_DATA_DIR"] = d
    return config.ensure_layout()


def seed_events(specs, root):
    for sid, line, markers, conf in specs:
        cs = make_session(sid, line)
        glean.run([cs], GleanFake([line], markers={line: markers}, confidence=conf), model="fake", root=root)


def by_quote(root):
    return {rv.quote: rv for rv in working_set(root)}


def _diff(expected, actual):
    """A legible key-by-key diff for the golden end-state assertion (the golden's whole point is a readable
    mismatch). Marks every diverging key so the cause is obvious at a glance."""
    lines = ["  key                              expected            actual"]
    for k in sorted(set(expected) | set(actual)):
        e, a = expected.get(k, "∅"), actual.get(k, "∅")
        mark = "" if e == a else "   <-- MISMATCH"
        lines.append(f"  {k:<32} {str(e):<19} {str(a)}{mark}")
    return "\n".join(lines)


# === 1. ROUTE COERCION: the 4th verdict, ASYMMETRIC unknown-id handling, list-number mapping ==========

R1 = use_store("route")
seed_events([("w-rt", LESSON_A, M_MID, 0.85)], R1)
rv = working_set(R1)[0]
cat_one = [{"id": "t-x", "title": "always commit with jj", "why": "sulin uses jj"}]

# _clean_route: a weaken of a KNOWN id passes through; an UNKNOWN/null id → noop (never mint a negative).
assert _clean_route({"decision": "weaken", "takeaway_id": "t-x"}, {"t-x"}) == \
    {"decision": "weaken", "takeaway_id": "t-x"}, "a weaken of a KNOWN id is kept"
assert _clean_route({"decision": "weaken", "takeaway_id": "t-ghost"}, {"t-x"}) == \
    {"decision": "noop", "takeaway_id": None}, "a weaken of an UNKNOWN id → noop (no negative takeaway)"
assert _clean_route({"decision": "weaken", "takeaway_id": None}, {"t-x"}) == \
    {"decision": "noop", "takeaway_id": None}, "a weaken with a null id → noop"
# the ASYMMETRY: strengthen of an unknown id → new (still a lesson); weaken of an unknown id → noop.
assert _clean_route({"decision": "strengthen", "takeaway_id": "t-ghost"}, {"t-x"})["decision"] == "new", \
    "the asymmetry: a strengthen of an unknown id is still a lesson → new"

# route(): a weaken of a real catalog id passes through; the router may name the list NUMBER → mapped to id.
assert route(rv, cat_one, EchoRoute("weaken", "t-x"))[0] == {"decision": "weaken", "takeaway_id": "t-x"}, \
    "a weaken of a real catalog id passes through route()"
assert route(rv, cat_one, EchoRoute("weaken", 1))[0] == {"decision": "weaken", "takeaway_id": "t-x"}, \
    "the router may answer with the list NUMBER for a weaken too — mapped back to the id"
assert route(rv, cat_one, EchoRoute("weaken", "t-ghost"))[0] == {"decision": "noop", "takeaway_id": None}, \
    "a weaken of an unknown id is coerced to noop in the full route() path"
print("OK §1 — route: the 4th `weaken` verdict; known id passes, unknown/null → noop (asymmetric to "
      "strengthen→new); the list-number maps back to the id for weaken too.")


# === 2. WEAKEN_SUPPORT: NO LLM, leaves support/why/title untouched, idempotent byte-stable no-op =======

R2 = use_store("unit")
seed_events([("w-a", LESSON_A, M_MID, 0.85), ("w-c", CONTRA_C, M_HI, 0.85)], R2)
wm = by_quote(R2)
rvA, rvC = wm[LESSON_A], wm[CONTRA_C]
tk, _ = synthesize_new(rvA, SynthFake(), [], known_concept_ids=set(), model="fake", run_id="r")
assert tk["contradictions"] == {"events": 0, "sessions": 0} and tk["contradicted_by"] == [], "born with the empty contradiction stat"

nv, cost = update_contradictions(tk, rvC)
assert cost == 0.0, "update_contradictions is ALWAYS NO LLM (cost 0.0)"
assert nv["support"] == tk["support"] and nv["why"] == tk["why"] and nv["title"] == tk["title"], \
    "a contradiction RECORDS the conflict — it does NOT rewrite support/why/title"
assert nv["contradictions"] == {"events": 1, "sessions": 1} and nv["contradicted_by"] == [rvC.id], \
    "the contradicting event is recorded; the stat counts one distinct event + session"
ce = nv["contradiction_evidence"][0]
data = blobstore.get(ce["cleaned_hash"], R2).encode("utf-8")
assert data[ce["byte_start"]:ce["byte_end"]].decode() == CONTRA_C == ce["quote"], \
    "the contradiction evidence is a ROBUST ANCHOR — span + verbatim quote resolving to real bytes (trust chain extends)"
assert ce["session_id"] == rvC.session_id, "the session rides WITH the contradiction evidence (powers the session count)"

# a re-weaken of the SAME event is a byte-identical no-op: contradictions never inflate (the closed op).
nv2, cost2 = update_contradictions(nv, rvC)
assert nv2["contradictions"] == {"events": 1, "sessions": 1} and cost2 == 0.0, "re-weakening an already-recorded event does NOT inflate"
assert blobstore.canonical_json(takeaway_content(nv2)) == blobstore.canonical_json(takeaway_content(nv)), \
    "the no-op re-version is byte-identical (a TimeMap no-op, no churn)"
# session dedup is derived from the evidence list, NOT its length: two DISTINCT contradiction events sharing
# one session count as {events:2, sessions:1} (a refactor counting len(contradiction_evidence) would miss it).
_shared = {"contradicted_by": ["e1", "e2"],
           "contradiction_evidence": [{"event_id": "e1", "session_id": "s1"},
                                      {"event_id": "e2", "session_id": "s1"}]}
assert _contradiction_stats(_shared) == {"events": 2, "sessions": 1}, \
    "two distinct contradiction events sharing one session → 2 events / 1 distinct session"
# a no-contradiction takeaway has net == support (the net change is invisible until a contradiction arrives).
assert net_sessions(tk) == tk["support"]["sessions"], "net == support when there is no contradiction"
print("OK §2 — update_contradictions: NO LLM; records the contradiction (span-verified) WITHOUT touching "
      "support/why/title; a re-weaken is a byte-identical no-op; session-dedup counts distinct sessions; "
      "net == support absent a contradiction.")


# === 3. THE NET-DEMOTION SCENARIO (golden-file expected vs actual) ===================================

assert GOLDEN.exists(), f"missing golden file — commit it: {GOLDEN}"
golden = json.loads(GOLDEN.read_text())
golden = {k: v for k, v in golden.items() if not k.startswith("_")}

R3 = use_store("scenario")
# A and B same lesson across distinct sessions s1/s2; C a contradiction from session s3.
seed_events([("w-s1", LESSON_A, M_MID, 0.85), ("w-s2", LESSON_B, M_MID, 0.85),
             ("w-s3", CONTRA_C, M_HI, 0.85)], R3)
wm = by_quote(R3)
rvA, rvB, rvC = wm[LESSON_A], wm[LESSON_B], wm[CONTRA_C]
assert len({rvA.session_id, rvB.session_id, rvC.session_id}) == 3, "the three events come from DISTINCT sessions"
synth = SynthFake()
KW = dict(known_concept_ids=set(), drift_threshold=1.0, model="fake", run_id="weaken-scenario", root=R3)

# A → new: mint T {support 1/1, contradictions 0/0}.
rrA, _ = route(rvA, catalog(R3), EchoRoute("new"))
nA, _, tkT = apply(rvA, rrA, {}, synth, [], **KW)
assert nA == 1 and tkT["support"] == {"events": 1, "sessions": 1} and tkT["contradictions"] == {"events": 0, "sessions": 0}

# B → strengthen T (distinct session): support 2/2 → T MATURES (net 2 >= MATURITY_SESSIONS=2).
cat = catalog(R3)
rrB, _ = route(rvB, cat, EchoRoute("strengthen", tkT["id"]))
nB, _, tkB = apply(rvB, rrB, {t["id"]: t for t in cat}, synth, [], **KW)
assert nB == 1 and tkB["id"] == tkT["id"] and tkB["support"] == {"events": 2, "sessions": 2}
assert tkT["id"] in {t["id"] for t in current_takeaways(R3)}, "after B, T is mature (net 2 >= the bar) — reviewable"

# C → weaken T (distinct, contradicting): contradictions 1/1 → net = 2-1 = 1 < 2 → T un-graduates.
cat = catalog(R3)
rrC, _ = route(rvC, cat, EchoRoute("weaken", tkB["id"]))
nC, costC, nvC = apply(rvC, rrC, {t["id"]: t for t in cat}, synth, [], **KW)
assert nC == 1 and costC == 0.0 and nvC["id"] == tkT["id"], "the weaken versions the SAME id (no churn), NO LLM"
assert synth.calls == 1, "exactly ONE synth call all scenario: A's `new`; B's drift gate is cheap; the weaken NEVER calls"

# the contradiction event left the working set, incorporated AS a contradiction (consolidated decision).
assert rvC.id not in {e.id for e in working_set(R3)}, "the contradicting event leaves the working set"
assert blobstore.latest_decision(rvC.id, R3)["decision"] == "weaken", "its consolidated decision records decision='weaken'"

# Build the ACTUAL end-state from the store (catalog reads the latest committed version = nvC).
T = tkT["id"]
final = [t for t in catalog(R3) if t["id"] == T][0]
fce = final["contradiction_evidence"][0]
fdata = blobstore.get(fce["cleaned_hash"], R3).encode("utf-8")
actual = {
    "support": final["support"],
    "contradictions": final["contradictions"],
    "net_sessions": net_sessions(final),
    "support_evidence_count": len(final["evidence"]),
    "contradiction_evidence_count": len(final["contradiction_evidence"]),
    "title_unchanged_by_weaken": final["title"] == tkB["title"],
    "why_unchanged_by_weaken": final["why"] == tkB["why"],
    "contradiction_evidence_span_verified": fdata[fce["byte_start"]:fce["byte_end"]].decode() == fce["quote"],
    "in_catalog": T in {t["id"] for t in catalog(R3)},
    "in_current_takeaways": T in {t["id"] for t in current_takeaways(R3)},
    "in_contradicted_takeaways": T in {t["id"] for t in contradicted_takeaways(R3)},
}
assert actual == golden, f"\nweaken end-state mismatch (expected == golden):\n{_diff(golden, actual)}"

# the blob is RETAINED — demotion is quarantine, never deletion (re-graduatable if corroboration returns).
assert blobstore.has(blobstore.latest_version(T, R3), R3), "the contested takeaway's blob remains (no deletion)"
print("OK §3 — golden net-demotion: A new → B strengthen (mature) → C weaken un-graduates T by NET "
      "entrenchment (2−1 < 2); T leaves current_takeaways, enters contradicted_takeaways, stays in catalog "
      "(never deleted), contradiction span-verified. actual == golden.")


# === 4. RE-GRADUATION: corroboration returning lifts the contested takeaway back over the net bar ======

R4 = use_store("regraduate")
seed_events([("rg-s1", LESSON_A, M_MID, 0.85), ("rg-s2", LESSON_B, M_MID, 0.85),
             ("rg-s3", CONTRA_C, M_HI, 0.85), ("rg-s4", LESSON_D, M_MID, 0.85)], R4)
wm = by_quote(R4)
syn = SynthFake()
KW4 = dict(known_concept_ids=set(), drift_threshold=1.0, model="fake", run_id="regraduate", root=R4)
_, _, t1 = apply(wm[LESSON_A], {"decision": "new", "takeaway_id": None}, {}, syn, [], **KW4)
apply(wm[LESSON_B], {"decision": "strengthen", "takeaway_id": t1["id"]},
      {t["id"]: t for t in catalog(R4)}, syn, [], **KW4)
apply(wm[CONTRA_C], {"decision": "weaken", "takeaway_id": t1["id"]},
      {t["id"]: t for t in catalog(R4)}, syn, [], **KW4)
assert t1["id"] not in {t["id"] for t in current_takeaways(R4)}, "contested → un-graduated (net 2−1 = 1 < 2)"
# a NEW corroborating session (rg-s4, a distinct fourth session) strengthens support to 3 → net = 3−1 = 2.
apply(wm[LESSON_D], {"decision": "strengthen", "takeaway_id": t1["id"]},
      {t["id"]: t for t in catalog(R4)}, syn, [], **KW4)
final4 = [t for t in catalog(R4) if t["id"] == t1["id"]][0]
assert final4["support"]["sessions"] == 3 and final4["contradictions"]["sessions"] == 1, "support 3 sessions vs 1 contradiction"
assert net_sessions(final4) == 2, "net = 3 − 1 = 2"
assert t1["id"] in {t["id"] for t in current_takeaways(R4)}, "RE-GRADUATED: net back at the bar → reviewable again"
print("OK §4 — re-graduation: a returning corroboration lifts support to 3 sessions; net = 3−1 = 2 crosses "
      "the bar again → the once-contested takeaway re-enters review (quarantine, not deletion).")


# === 5. THE BLOCK: DreamBlock.process counts the weaken + FOLDS it into the on-instance catalog ========

class SeqRoute:
    """A scripted decision per CALL (new, then strengthen, then weaken), naming the first catalog id in the
    prompt for strengthen/weaken — drives DreamBlock.process through the weaken path so its n_weakened
    counter and on-instance fold (so the NEXT event routes against the UPDATED contradiction stats) are
    exercised, plus RunReport.n_weakened."""
    def __init__(self, seq, *, cost=0.0005):
        self.seq, self.cost, self.calls = list(seq), cost, 0

    def __call__(self, system, user):
        dec = self.seq[self.calls] if self.calls < len(self.seq) else "noop"
        self.calls += 1
        ids = re.findall(r"\[\d+\] id=(\S+?):", user)
        tid = ids[0] if (dec in ("strengthen", "weaken") and ids) else None
        return Completion(text=json.dumps({"decision": dec, "takeaway_id": tid}),
                          model="route-fake", cost_usd=self.cost)


R5 = use_store("block")
seed_events([("bw-s1", LESSON_A, M_MID, 0.85), ("bw-s2", LESSON_B, M_MID, 0.85),
             ("bw-s3", CONTRA_C, M_HI, 0.85)], R5)
blk = dream.DreamBlock(SeqRoute(["new", "strengthen", "weaken"]), SynthFake(),
                       route_model="fake", synth_model="fake", forget=False)
# drive process() in an EXPLICIT A,B,C order (test_dream §6 pattern) so the scripted sequence lines up; the
# salience queue would otherwise serve the high-surprise contradiction first.
wm = by_quote(R5)
order = [wm[LESSON_A], wm[LESSON_B], wm[CONTRA_C]]
blk.items(R5)                                          # loads concepts + the (empty) on-instance catalog
for rv in order:
    blk.process(rv, root=R5, run_id="block-weaken")
assert (blk.n_new, blk.n_strengthened, blk.n_weakened) == (1, 1, 1), \
    f"process tallies one new, one strengthen, one weaken: {(blk.n_new, blk.n_strengthened, blk.n_weakened)}"
# the on-instance fold: after the weaken, the in-memory catalog carries the UPDATED contradiction stat.
folded = blk._cat_by_id[list(blk._cat_by_id)[0]]
assert folded["contradictions"] == {"events": 1, "sessions": 1}, "the weakened version folded into the on-instance catalog"
# RunReport surfaces n_weakened through the block.
rep = dream.run(SeqRoute(["noop"]), SynthFake(), route_model="fake", synth_model="fake", forget=False, root=R5)
assert hasattr(rep, "n_weakened") and rep.n_weakened == 0, "RunReport exposes n_weakened (0 on a no-weaken run)"
print("OK §5 — the Block: process() counts the weaken and folds the contradicted version into the "
      "on-instance catalog; RunReport exposes n_weakened.")


print("\nall weaken tests passed.")
