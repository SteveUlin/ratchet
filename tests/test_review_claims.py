"""review × dream-v3 claims (S5, design §6/§2.2, ADR-0028): the human gate reads the CLAIM graph.
Exercised OFFLINE with fake Completers (no network); every store is real blobs (transcript → cleaned →
event → claim + edges), so the trust chain under test is genuine. The load-bearing checks:

  THE FEED — a mature claim reaches `pending` as a card (kind "claim") with the WHY-PENDING badge
    (why=null until synthesize; never withheld, §6), its scope, and the bar rationale (ADR-0027).
  THE AUDIT CARD (§6.3, the v2-failure detector) — every corroboration's VERIFIED quote renders beside
    the match key the resolver persisted (stmt_sim, candidates_shown count, model); the ⚠ `disjoint`
    flag fires ONLY when an edge's subject shares no repo/file with the claim's other evidence; the
    corroboration STORY narrates recurrence ("again in session … — recurred after N days").
  CONTESTED-NEAR-BAR (§6.6) — a claim a live contradicts edge pushed under the bar surfaces in
    `contested` (with the contradicting quotes as ground truth); once re-matured it re-enters `pending`
    flagged `contested`.
  DERIVED MERGE SUGGESTIONS (§2.2/§6.5) — a residue-band pair of live ACTIVE claims renders titles +
    quotes (NEVER the stmt_sim number), rides the pending cards capped at SUGGEST_MAX, folds out past
    SUGGEST_TTL_DAYS without new evidence, and DISAPPEARS after the compound reject-merge (pair form —
    nothing reopens, the pair is never asked again). `merge_claims` re-points the loser's edges onto
    the winner (match keys preserved, `repointed_from` audit) and refuses a reject-merge'd pair.
  REJECT-MERGE (edge form) — review's verb hands the edge id to resolve's ONE compound decision writer:
    retraction (support decrements), reopen (the event re-enters the working set), pair-block.
  ACCEPT — a claim accepts through the same door: the concept bakes EXACTLY the re-validated spans from
    the live-edge fold (the stored claim blob carries no evidence at all).
  LEGACY BESIDE — v2 takeaways still queue next to claims (one importance order); on an id collision
    the claim view is preferred; legacy accept still mints.

Run: `python tests/test_review_claims.py` (throwaway dirs)."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-review-claims-")

from ratchet import blobstore, chunk, config, dream, glean, resolve, review, sig  # noqa: E402
from ratchet.completer import Completion  # noqa: E402
from ratchet.dream import working_set  # noqa: E402

# --- fixtures: summaries crafted into the measured bands (verified below with sig.jaccard) ----------
JJ_SEED = "always commit with jj and never use git for version control"
JJ_PARA = "version control goes through jj, so avoid reaching for git commands"       # ~0.20 vs seed
JJ_PARA2 = "prefer jj for version control work instead of git commands"               # ~0.23 vs union
JJ_CONTRA = "never use jj for version control here, git commands work better"         # ~0.31 vs union
PYTEST = "run the test suite with python -m pytest from the repo root, not from tests/"
RUFF = "run the linter with python -m ruff from the repo root before committing"      # ~0.35 vs PYTEST

M_HI = {"surprise": 0.9, "insight": 0.3}
M_MID = {"surprise": 0.2, "insight": 0.7}


def sim_of(a, b):
    return sig.jaccard(sig.char_shingles(a), sig.char_shingles(b))


# the fixtures prove what they claim: every merge/contradiction path rides the residue band.
_union = sig.char_shingles(JJ_SEED) | sig.char_shingles(JJ_PARA)
assert sig.J_MAYBE <= sim_of(JJ_SEED, JJ_PARA) < sig.J_HIGH, f"{sim_of(JJ_SEED, JJ_PARA):.4f}"
assert sig.J_MAYBE <= sig.jaccard(sig.char_shingles(JJ_PARA2), _union) < sig.J_HIGH
assert sig.J_MAYBE <= sig.jaccard(sig.char_shingles(JJ_CONTRA), _union) < sig.J_HIGH
assert sig.J_MAYBE <= sim_of(PYTEST, RUFF) < sig.J_HIGH, f"{sim_of(PYTEST, RUFF):.4f}"
for s in (JJ_SEED, JJ_PARA, JJ_PARA2, JJ_CONTRA, PYTEST, RUFF):
    assert sig.entropy(s) >= sig.H_MIN, f"fixture under the entropy gate: {s!r}"


def days_ago(n):
    """A DYNAMIC valid-time n days back — fixed dates would age the fixtures out of maturity as
    calendar time passes (the recency-weighted gate is real)."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}


def make_session(sid, line, *, repo=None, mtime=None):
    """A real transcript → cleaned blob → chunkset, with a controlled REPO (subject facet) and an
    optional MTIME (the session's valid-time — the story's dates and the TTL knob read it).
    Multibyte filler keeps byte≠char offsets genuine."""
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
    """Points at the numbered prompt line carrying each durable line (ADR-0026); the summary is the
    line itself, so the claim signatures are exactly the fixture sims."""
    def __init__(self, lines, *, markers=None, confidence=0.85):
        self.lines, self.markers, self.confidence = lines, markers or {}, confidence

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
                cands.append({"lines": {"from": hit, "to": hit}, "summary": ln,
                              "markers": self.markers.get(ln, M_MID), "confidence": self.confidence})
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class ResolveFake:
    """Scripted residue verdicts (Haiku's seat), in call order."""
    def __init__(self, verdicts=(), *, cost=0.001):
        self.verdicts, self.cost, self.calls = list(verdicts), cost, 0

    def __call__(self, system, user):
        v = self.verdicts[self.calls] if self.calls < len(self.verdicts) else "none"
        self.calls += 1
        return Completion(text=json.dumps({"verdict": v}), model="resolve-fake", cost_usd=self.cost)


class NeverCalled:
    def __call__(self, system, user):
        raise AssertionError("the residue completer must not be called on the $0 path")


def use_store(prefix):
    d = tempfile.mkdtemp(prefix=f"ratchet-test-review-claims-{prefix}-")
    os.environ["RATCHET_DATA_DIR"] = d
    return config.ensure_layout()


def seed_events(specs, root):
    """[(session_id, line, markers, confidence, repo, mtime)] — real sessions → glean events whose
    summary IS the line (the signature source)."""
    for sid, line, markers, conf, repo, mtime in specs:
        cs = make_session(sid, line, repo=repo, mtime=mtime)
        glean.run([cs], GleanFake([line], markers={line: markers}, confidence=conf),
                  model="fake", root=root)


def seed_takeaway(root, *, id, title, why, evidence, support):
    """A v2-shape takeaway blob, ingested directly (the legacy feed under test)."""
    sessions_seen = [f"sess-{id}-{i}" for i in range(support.get("sessions", 0))]
    rec_ = {"id": id, "title": title, "why": why, "relation": {"kind": "new", "concept_id": None},
            "cites": [e.get("event_id") for e in evidence], "evidence": evidence, "support": support,
            "sessions_seen": sessions_seen, "markers": {k: 0.0 for k in glean.MARKER_KINDS},
            "confidence": 0.8, "last_seen": "2024-01-01T00:00:00+00:00"}
    blobstore.ingest(blobstore.canonical_json(rec_), source_kind="takeaway", source_id=id,
                     origin_ref={"stage": "dream", "model": "seed"}, root=root)
    return id


def the_claim(root):
    pool = resolve.claim_pool(root)
    assert len(pool) == 1, f"expected exactly one claim, got {len(pool)}"
    return pool[0]


# === 1. THE FEED + WHY-PENDING BADGE + AUDIT CARD + STORY (cross-repo → ⚠) ==========================

RA = use_store("a")
seed_events([("a-s1", JJ_SEED, M_HI, 0.85, "alpha", days_ago(15)),
             ("a-s2", JJ_PARA, M_MID, 0.85, "beta", days_ago(0))], RA)
fake_a = ResolveFake(["same-as-1"])
resolve.run(fake_a, model="fake", forget=False, root=RA)
assert fake_a.calls == 1, "the paraphrase reached the residue call"
claim_a = the_claim(RA)
para_eid = [e for e in claim_a["cites"] if e != claim_a["seed_event"]][0]

q = review.pending(RA)
assert len(q) == 1, f"the mature claim reaches the queue: {len(q)}"
t = q[0]
assert t["kind"] == "claim" and t["takeaway_id"] == claim_a["id"], "the card is the claim view"
assert t["why"] is None and t["why_pending"] is True, \
    "why=null (synthesize hasn't run) → the WHY-PENDING badge, and the claim is NOT withheld (§6)"
assert t["title"] == JJ_SEED, "the provisional title is the seed event's summary"
assert t["mature"] and "≥ bar" in t["rationale"], "the bar standing rides the card (ADR-0027)"
assert t["scope"] == "cross-cutting" and t["subject"]["repos"] == ["alpha", "beta"], \
    "scope is SHOWN (soft signal, §3.4), never a veto"
assert {e["quote"] for e in t["evidence"]} == {JJ_SEED, JJ_PARA}, "both tellings re-validate"
assert all(e["verified"] for e in t["evidence"])
assert t["contested"] is False and t["merge_suggestions"] == []

audit = t["audit"]
assert audit is not None, "an llm-merged claim ALWAYS gets the audit card (§6.3)"
by_quote = {r["quote"]: r for r in audit["corroborations"]}
assert set(by_quote) == {JJ_SEED, JJ_PARA}, "every corroboration renders its verified quote"
seed_row, llm_row = by_quote[JJ_SEED], by_quote[JJ_PARA]
assert seed_row["by"] == "seed" and seed_row["match"] is None, "the seed edge carries no llm match key"
m = llm_row["match"]
assert llm_row["by"] == "llm" and m is not None, "the llm merge renders its match key"
assert abs(m["stmt_sim"] - sim_of(JJ_SEED, JJ_PARA)) < 1e-6, "stmt_sim is the recorded pair similarity"
assert m["candidates_shown"] == 1 and m["model"] == "fake", "candidates_shown is a COUNT; model recorded"
assert llm_row["edge_id"] == resolve.edge_id(para_eid, "corroborates", claim_a["id"]), \
    "the edge id rides the card — the --reject-merge handle"
assert llm_row["disjoint"] is True and audit["disjoint"] is True, \
    "⚠ fires: repo beta shares no repo/file with the claim's other evidence"
story = audit["story"]
assert len(story) == 2 and story[0].startswith("seen in session a-s1") and "repo alpha" in story[0]
assert story[1].startswith("again in session a-s2") and "repo beta" in story[1]
assert "recurred after 15 day(s)" in story[1], f"the recurrence narrative carries the gap: {story[1]}"
print("OK 1 — a mature claim queues as a card: why-pending badge (never withheld), scope shown, and the")
print("       audit card renders every quote beside its match key, ⚠ on the disjoint subject, with the")
print("       recurrence story.")


# === 2. ACCEPT: the concept bakes EXACTLY the re-validated spans from the live-edge fold ============

assert dream.load_concepts(RA) == [], "no concepts before the accept"
cid = review.accept(claim_a["id"], RA, assessment="both quotes state the jj-not-git lesson")
assert cid.startswith("c-")
concepts = dream.load_concepts(RA)
assert [c["id"] for c in concepts] == [cid], "the accepted claim is now a concept (the loop closes)"
c = concepts[0]
assert c["title"] == JJ_SEED and c["statement"] == "", "provisional title; why-pending → empty statement"
assert {e["event_id"] for e in c["evidence"]} == set(claim_a["cites"]), \
    "the concept's evidence == the claim's live-edge citations (the stored claim blob has NO evidence)"
for e in c["evidence"]:
    data = blobstore.get(e["cleaned_hash"], RA).encode("utf-8")
    assert data[e["byte_start"]:e["byte_end"]].decode() in (JJ_SEED, JJ_PARA), \
        "every baked span re-resolves to a verbatim quote (re-validated at accept)"
assert review.pending(RA) == [], "the accepted claim leaves the queue"
assert blobstore.latest_decision(claim_a["id"], RA)["verb"] == "accept"
assert [v["id"] for v in resolve.high_confidence_view(RA)] == [claim_a["id"]], \
    "accepted ∧ mature → the trusted view (§5)"
print("OK 2 — accept mints the concept from the live-edge fold's re-validated spans; the claim leaves")
print("       the queue and enters the trusted view.")


# === 3. ⚠ IS SPECIFIC: a same-repo merge shows NO disjoint flag; edge reject-merge reopens ==========

RB = use_store("b")
seed_events([("b-s1", JJ_SEED, M_HI, 0.85, "alpha", None),
             ("b-s2", JJ_PARA, M_MID, 0.85, "alpha", None)], RB)
resolve.run(ResolveFake(["same-as-1"]), model="fake", forget=False, root=RB)
claim_b = the_claim(RB)
tb = review.pending(RB)[0]
assert tb["scope"] == "local", "same repo → local scope"
assert tb["audit"] is not None and tb["audit"]["disjoint"] is False, "no ⚠ on shared-subject evidence"
assert all(r["disjoint"] is False for r in tb["audit"]["corroborations"]), \
    "⚠ fires ONLY on disjoint subjects — same-repo llm merges are audited but not flagged"

para_b = [e for e in claim_b["cites"] if e != claim_b["seed_event"]][0]
body = review.reject_merge(resolve.edge_id(para_b, "corroborates", claim_b["id"]),
                           reason="different lessons", root=RB)
assert body["verb"] == "reject-merge" and body["target"] == para_b, \
    "the edge form targets the EVENT (retraction + reopen + pair-block, one blob)"
after_b = the_claim(RB)
assert after_b["support"] == {"events": 1, "sessions": 1}, "the edge fold reads it as retraction"
assert {rv.id for rv in working_set(RB)} == {para_b}, "the working-set fold reads it as reopen"
assert review.pending(RB) == [], "the un-merged claim drops under the bar"
inc_b = review.incubating(RB)
assert [(r["takeaway_id"], r["kind"]) for r in inc_b] == [(claim_b["id"], "claim")], \
    "…and shows in incubating as a claim (never silently lost)"
print("OK 3 — ⚠ is specific to disjoint subjects; review --reject-merge (edge form) retracts, reopens")
print("       the event, and the claim un-matures into incubating.")


# === 4. CONTESTED-NEAR-BAR: a live contradicts edge must not silently suppress a claim (§6.6) =======

RC = use_store("c")
seed_events([("c-s1", JJ_SEED, M_HI, 0.85, "alpha", None)], RC)
resolve.run(NeverCalled(), model="fake", forget=False, root=RC)
seed_events([("c-s2", JJ_PARA, M_MID, 0.85, "beta", None)], RC)
fake_c2 = ResolveFake(["same-as-1"])
resolve.run(fake_c2, model="fake", forget=False, root=RC)
assert fake_c2.calls == 1
seed_events([("c-s3", JJ_CONTRA, M_MID, 0.85, "alpha", None)], RC)
fake_c3 = ResolveFake(["contradicts-1"])
resolve.run(fake_c3, model="fake", forget=False, root=RC)
assert fake_c3.calls == 1, "the contradiction reached the residue call"
claim_c = the_claim(RC)
assert claim_c["support"]["sessions"] == 2 and claim_c["contradictions"]["sessions"] == 1

assert review.pending(RC) == [], "net 1 (2 support − 1 contra) sits under the 1.5 bar — out of pending"
rows = review.contested(RC)
assert len(rows) == 1 and rows[0]["claim_id"] == claim_c["id"], \
    "…but the contested listing surfaces it (within one session of the bar)"
r = rows[0]
assert r["mature"] is False and r["contradictions"] == {"events": 1, "sessions": 1}
assert r["contradicting"] == [JJ_CONTRA], "the contradicting quote is re-validated ground truth"
assert review.contested(RC, maturity=99.0) == [], "the window tracks the bar — far-below claims stay out"

seed_events([("c-s4", JJ_PARA2, M_MID, 0.85, "gamma", None)], RC)
fake_c4 = ResolveFake(["same-as-1"])
resolve.run(fake_c4, model="fake", forget=False, root=RC)
assert fake_c4.calls == 1
qc = review.pending(RC)
assert len(qc) == 1 and qc[0]["contested"] is True, \
    "re-matured (net 2) → back in pending, FLAGGED contested"
assert qc[0]["support"]["sessions"] == 3
assert review.contested(RC)[0]["mature"] is True, "…and still visible in the contested listing"
print("OK 4 — a contradiction pushes the claim out of pending but into --contested (quotes shown);")
print("       fresh corroboration re-matures it into pending with the contested flag.")


# === 5. DERIVED MERGE SUGGESTIONS: residue pair → titles+quotes (no number), TTL, reject-merge ======

RD = use_store("d")
seed_events([("d-s1", PYTEST, M_HI, 0.85, "gamma", days_ago(10)),
             ("d-s2", RUFF, M_MID, 0.85, "gamma", days_ago(10))], RD)
fake_d = ResolveFake(["none"])
resolve.run(fake_d, model="fake", forget=False, root=RD)
assert fake_d.calls == 1, "the shared vocabulary reached the residue; the honest NONE kept 2 claims"
pool_d = {c["title"]: c for c in resolve.claim_pool(RD)}
assert set(pool_d) == {PYTEST, RUFF}
pair_d = sorted((pool_d[PYTEST]["id"], pool_d[RUFF]["id"]))

suggs = review.merge_suggestions(RD)
assert len(suggs) == 1 and suggs[0]["pair"] == pair_d, "the residue-band pair is suggested"
sides = {s["claim_id"]: s for s in suggs[0]["claims"]}
assert {sides[pool_d[PYTEST]["id"]]["quote"], sides[pool_d[RUFF]["id"]]["quote"]} == {PYTEST, RUFF}, \
    "each side renders its title + a verified quote"
assert "stmt_sim" not in json.dumps(suggs), \
    "NEVER the raw stmt_sim number — it is noise in the residue band (§6.5)"
assert review.merge_suggestions(RD, ttl_days=5.0) == [], \
    "10-day-old evidence folds out past a 5-day TTL — a suggestion cannot linger forever (§2.2)"

qd = review.pending(RD, maturity=0.5)              # the reviewer's knob lowers the bar (ADR-0027)
assert len(qd) == 2 and all(len(card["merge_suggestions"]) == 1 for card in qd), \
    "the suggestion rides BOTH claims' pending cards (capped at SUGGEST_MAX)"

body = review.reject_merge(f"{pair_d[0]},{pair_d[1]}", reason="pytest and ruff are different tools",
                           root=RD)
assert body["pair"] == pair_d and body["target"] is None and body["edge_id"] is None, \
    "the pair form is pair-block ONLY — no event, nothing to retract or reopen"
assert working_set(RD) == [], "…and indeed nothing reopened"
assert review.merge_suggestions(RD) == [], "a dismissed suggestion is NEVER asked again"
assert all(card["merge_suggestions"] == [] for card in review.pending(RD, maturity=0.5))
try:
    review.merge_claims(pair_d[0], pair_d[1], RD)
    assert False, "merging a reject-merge'd pair must be refused (the standing human verdict)"
except ValueError:
    pass
print("OK 5 — the derived suggestion renders quotes not numbers, rides the cards, folds out past the")
print("       TTL, and one pair-form reject-merge kills it for good (merge_claims refuses the pair).")


# === 6. merge_claims: confirm = edge re-pointing; loser folds out; audit trail survives =============

RG = use_store("g")
seed_events([("g-s1", PYTEST, M_HI, 0.85, "gamma", None),
             ("g-s2", RUFF, M_MID, 0.85, "gamma", None)], RG)
resolve.run(ResolveFake(["none"]), model="fake", forget=False, root=RG)
pool_g = {c["title"]: c for c in resolve.claim_pool(RG)}
winner_id, loser_id = pool_g[PYTEST]["id"], pool_g[RUFF]["id"]
loser_seed = pool_g[RUFF]["seed_event"]

res = review.merge_claims(loser_id, winner_id, RG, reason="one lesson: run tools from the repo root")
assert res == {"loser": loser_id, "winner": winner_id, "moved_edges": 1}
merged = the_claim(RG)                             # the loser folded out of the pool
assert merged["id"] == winner_id and merged["support"] == {"events": 2, "sessions": 2}
assert {e["quote"] for e in review.resolve_evidence(merged, RG)} == {PYTEST, RUFF}, \
    "the winner's fold unions both claims' evidence (dream.merge's union, restated as edges)"
moved = json.loads(blobstore.get(
    blobstore.latest_version(resolve.edge_id(loser_seed, "corroborates", winner_id), RG), RG))
assert moved["active"] and moved["match"]["repointed_from"] == loser_id, \
    "the re-pointed edge keeps its match key + records where it came from (the audit trail)"
old = json.loads(blobstore.get(
    blobstore.latest_version(resolve.edge_id(loser_seed, "corroborates", loser_id), RG), RG))
assert not old["active"], "the loser's edge is retracted (invalidate-don't-delete)"
d = blobstore.latest_decision(loser_id, RG)
assert d["verb"] == "merge" and d["into"] == winner_id, "ONE merge decision drops the loser from the pool"
assert [c["id"] for c in resolve.current_claims(RG)] == [winner_id], "the merged claim matures (2 sessions)"
assert review.pending(RG)[0]["takeaway_id"] == winner_id
print("OK 6 — merge_claims re-points the loser's live edges (match keys preserved, repointed_from")
print("       stamped), retracts the originals, and one merge decision folds the loser out.")


# === 7. LEGACY BESIDE CLAIMS: one queue, two feeds, claims preferred on an id collision =============

RH = use_store("h")
seed_events([("h-s1", JJ_SEED, M_HI, 0.85, "alpha", None),
             ("h-s2", JJ_PARA, M_MID, 0.85, "alpha", None)], RH)
resolve.run(ResolveFake(["same-as-1"]), model="fake", forget=False, root=RH)
claim_h = the_claim(RH)
legacy_ev = [{k: e[k] for k in ("event_id", "cleaned_hash", "byte_start", "byte_end")}
             for e in claim_h["evidence"]]
seed_takeaway(RH, id="legacy-1", title="a legacy v2 takeaway", why="the v2 synthesis",
              evidence=legacy_ev, support={"events": 2, "sessions": 2})

qh = review.pending(RH)
kinds = {t["takeaway_id"]: t["kind"] for t in qh}
assert kinds == {claim_h["id"]: "claim", "legacy-1": "takeaway"}, \
    f"the queue is the UNION — claim and legacy takeaway side by side: {kinds}"
legacy_card = [t for t in qh if t["takeaway_id"] == "legacy-1"][0]
assert legacy_card["why"] == "the v2 synthesis" and "why_pending" not in legacy_card, \
    "the legacy card keeps its v2 shape (no claim-only fields)"
assert all(e["verified"] for e in legacy_card["evidence"]), "legacy evidence still re-validates"

# the id-collision dedup: a takeaway blob under the CLAIM's id must not shadow the claim view.
seed_takeaway(RH, id=claim_h["id"], title="a shadow takeaway", why="must not surface",
              evidence=legacy_ev, support={"events": 2, "sessions": 2})
qh2 = review.pending(RH)
assert len(qh2) == 2, "one id, one card — no duplicate"
same_id = [t for t in qh2 if t["takeaway_id"] == claim_h["id"]][0]
assert same_id["kind"] == "claim" and same_id["title"] == JJ_SEED, \
    "on an id collision the CLAIM view is preferred (the takeaway under that id never shadows it)"

seed_takeaway(RH, id="legacy-inc", title="still accruing", why="one session so far",
              evidence=legacy_ev, support={"events": 1, "sessions": 1})
inc_h = review.incubating(RH)
assert ("legacy-inc", "takeaway") in {(r["takeaway_id"], r["kind"]) for r in inc_h}, \
    "a legacy takeaway still incubates beside claims"

lcid = review.accept("legacy-1", RH, assessment="legacy accept still works")
assert lcid.startswith("c-") and "legacy-1" not in {t["takeaway_id"] for t in review.pending(RH)}, \
    "the legacy accept path is untouched"
print("OK 7 — legacy v2 takeaways queue, incubate, and accept beside claims; on an id collision the")
print("       claim view wins (after --reset-v2 the legacy arm simply empties).")

print("\nall review-claims tests passed.")
