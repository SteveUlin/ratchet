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
  THE KIND FACET (ADR-0029) — synthesize's proposed kind rides the card (`claim_kind`); accept records
    it on the DECISION (default = the proposal; --kind overrides — the reviewer's call is
    authoritative); `set_kind` re-kinds an existing concept and outranks the accept in the fold; a
    concept with no kind anywhere reads behavioral (the legacy default).
  THE SCOPE AXIS (ADR-0030) — kind's mirror, derivation-proposed instead of LLM-proposed: the card's
    `scope_repo` derives from the live evidence's subject keys (one repo → its label; 2+/none →
    global); accept records the scope on the DECISION (default = the derivation; --scope overrides,
    open vocabulary, blank refused); `set_scope` re-scopes an existing concept, outranks the accept,
    latest wins, valid targets only; a concept with no scope anywhere reads global.
  THE REFRESH VERB — a why-pending accept mints statement ''; when synthesize later fills the claim's
    why, the concept does NOT follow automatically (the gate is the trust source) — `refresh` is the
    reviewer's re-snapshot: prose moves, evidence/kind/scope/validity/claim-decisions all hold; a
    no-op refuses; retired/unknown/orphaned targets refuse.

Run: `python tests/test_review_claims.py` (throwaway dirs)."""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-review-claims-")

from ratchet import blobstore, chunk, concepts, config, dream, glean, resolve, review, sig, synthesize  # noqa: E402
from ratchet.completer import Completion  # noqa: E402
from ratchet.events import working_set  # noqa: E402

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
assert t["claim_kind"] is None, "no kind proposed yet — the typology arrives with synthesize's prose"
assert t["title"] == JJ_SEED, "the provisional title is the seed event's summary"
assert t["mature"] and "≥ bar" in t["rationale"], "the bar standing rides the card (ADR-0027)"
assert t["scope"] == "cross-cutting" and t["subject"]["repos"] == ["alpha", "beta"], \
    "scope is SHOWN (soft signal, §3.4), never a veto"
assert t["scope_repo"] == "global", \
    "evidence spanning 2 repos derives scope_repo=global — a multi-repo lesson is de facto general (ADR-0030)"
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

assert concepts.load_concepts(RA) == [], "no concepts before the accept"
cid = review.accept(claim_a["id"], RA, assessment="both quotes state the jj-not-git lesson")
assert cid.startswith("c-")
cs = concepts.load_concepts(RA)
assert [c["id"] for c in cs] == [cid], "the accepted claim is now a concept (the loop closes)"
c = cs[0]
assert c["title"] == JJ_SEED and c["statement"] == "", "provisional title; why-pending → empty statement"
assert {e["event_id"] for e in c["evidence"]} == set(claim_a["cites"]), \
    "the concept's evidence == the claim's live-edge citations (the stored claim blob has NO evidence)"
for e in c["evidence"]:
    data = blobstore.get(e["cleaned_hash"], RA).encode("utf-8")
    assert data[e["byte_start"]:e["byte_end"]].decode() in (JJ_SEED, JJ_PARA), \
        "every baked span re-resolves to a verbatim quote (re-validated at accept)"
assert review.pending(RA) == [], "the accepted claim leaves the queue"
assert c["kind"] == "behavioral", "no proposal, no override → the legacy default kind"
d2 = blobstore.latest_decision(claim_a["id"], RA)
assert d2["verb"] == "accept" and d2["kind"] == "behavioral", \
    "the accept DECISION records the confirmed kind — the authoritative record (ADR-0029)"
assert d2["scope"] == "global" and c["scope"] == "global", \
    "…and the confirmed scope (ADR-0030): the two-repo derivation proposed global, the accept recorded it"
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
assert tb["scope_repo"] == "alpha", \
    "every quote in ONE repo derives scope_repo=that repo — the lesson never left home (ADR-0030)"
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
             ("d-s2", RUFF, M_MID, 0.85, "gamma", days_ago(9))], RD)   # a DAY apart: two same-repo
# sessions at the SAME instant would coalesce into one SITTING (temporal.COALESCE_HOURS) and the gate
# would settle the pair at $0 — this section needs the residue call to fire, so the fixture keeps
# the sessions genuinely distinct (>12h). Still both >5 days old for the TTL fold-out below.
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


# === 8. THE KIND FACET (ADR-0029): propose → card → accept confirms → set_kind re-kinds =============

RK = use_store("k")
seed_events([("k-s1", PYTEST, M_HI, 0.85, "gamma", None),
             ("k-s2", RUFF, M_MID, 0.85, "gamma", None)], RK)
resolve.run(ResolveFake(["none"]), model="fake", forget=False, root=RK)
pool_k = {c["title"]: c for c in resolve.claim_pool(RK)}
pid, rid = pool_k[PYTEST]["id"], pool_k[RUFF]["id"]


class SynthKind:
    """Scripted Sonnet prose carrying a PROPOSED kind — the synthesize side of the facet."""
    def __init__(self, payload):
        self.payload = payload

    def __call__(self, system, user):
        assert '"kind": "behavioral"|"reference"' in system, "the split is defined in the contract"
        return Completion(text=json.dumps(self.payload), model="synth-fake", cost_usd=0.01)


for the_id, line in ((pid, PYTEST), (rid, RUFF)):
    synthesize.run(SynthKind({"title": line[:60], "why": f"a fact about {line[:24]} one would look up.",
                              "kind": "reference", "confidence": 0.8}),
                   model="fake", claim=the_id, root=RK)

def kinds_of(root):
    return {c["id"]: c["kind"] for c in concepts.load_concepts(root)}

# (a) the card carries the proposal — reference, the non-default worth a line.
cards = {t["takeaway_id"]: t for t in review.pending(RK, maturity=0.5)}
assert cards[pid]["claim_kind"] == "reference" and cards[rid]["claim_kind"] == "reference", \
    "the pending card shows synthesize's proposed kind (claim_kind — `kind` is the queue-source tag)"

# (b) accept DEFAULT follows the proposal; the decision is the authoritative record.
cidp = review.accept(pid, RK, assessment="a lookup fact, faithfully quoted")
assert kinds_of(RK)[cidp] == "reference", "accept default = the proposed kind"
dp = blobstore.latest_decision(pid, RK)
assert dp["verb"] == "accept" and dp["kind"] == "reference"

# (c) --kind OVERRIDES the proposal (the reviewer's call beats the model's) — through the CLI wiring.
buf = io.StringIO()
with redirect_stdout(buf):
    review.main(["--accept", rid, "--kind", "behavioral", "--json"])
out = json.loads(buf.getvalue())
cidr = out["concept"]
assert out["kind"] == "behavioral" and kinds_of(RK)[cidr] == "behavioral", \
    "--kind behavioral overrides a reference proposal"

# (d) an out-of-vocabulary override is REFUSED, never coerced — a reviewer's typo is an error.
try:
    review.accept(rid, RK, kind="mechanism")
    assert False, "an invalid explicit kind must raise"
except ValueError:
    pass

# (e) set_kind re-kinds an EXISTING concept and OUTRANKS the accept; the latest set_kind wins.
review.set_kind(cidp, "behavioral", RK, reason="it shapes conduct after all")
assert kinds_of(RK)[cidp] == "behavioral", "set_kind > the accept's kind (the later, deliberate call)"
buf = io.StringIO()
with redirect_stdout(buf):
    review.main(["--set-kind", cidp, "reference", "--reason", "no — lookup material"])
assert "re-kinded → reference" in buf.getvalue()
assert kinds_of(RK)[cidp] == "reference", "the LATEST set_kind wins (append-only, latest decision in force)"

# (f) guards: closed vocabulary; valid targets only (a decision on a retired concept would resurrect it).
for bad_call in (lambda: review.set_kind(cidp, "mechanism", RK),
                 lambda: review.set_kind("c-nope", "reference", RK)):
    try:
        bad_call()
        assert False, "set_kind must refuse an invalid kind / unknown concept"
    except ValueError:
        pass
review.retire(cidr, RK, reason="retired to prove the guard")
try:
    review.set_kind(cidr, "reference", RK)
    assert False, "set_kind on a retired concept must refuse — re-kinding is never an accidental un-retire"
except ValueError:
    pass
assert cidr not in kinds_of(RK), "…and the retired concept stayed retired"

# (g) LEGACY: a concept with no kind anywhere — no proposal, no accept-kind, no set_kind — reads behavioral.
legacy = {"id": "c-legacy", "title": "old concept", "statement": "predates the typology",
          "evidence": [], "source_takeaway": "t-old"}
blobstore.ingest(blobstore.canonical_json(legacy), source_kind="concept", source_id="c-legacy",
                 origin_ref={"stage": "test"}, root=RK)
assert kinds_of(RK)["c-legacy"] == "behavioral", \
    "legacy concepts default behavioral — they keep shaping conduct until the reviewer says otherwise"
print("OK 8 — kind: proposed on the card, confirmed on the accept decision (default = proposal, --kind")
print("       overrides), re-kinded via set_kind (latest wins, valid targets only), legacy → behavioral.")


# === 9. THE SCOPE AXIS (ADR-0030): derive → card → accept confirms → set_scope re-scopes ============
# The kind facet's mirror image: the proposal comes from the EVIDENCE (deterministic — no LLM), the
# reviewer's decision is authoritative, and the vocabulary is OPEN (free-text repo labels; blank refused).

RS = use_store("s")
seed_events([("s-s1", PYTEST, M_HI, 0.85, "claude-bus", None),   # one-repo evidence → its label
             ("s-s2", RUFF, M_MID, 0.85, None, None)], RS)       # no-repo evidence → global
resolve.run(ResolveFake(["none"]), model="fake", forget=False, root=RS)
pool_s = {c["title"]: c for c in resolve.claim_pool(RS)}
pid_s, rid_s = pool_s[PYTEST]["id"], pool_s[RUFF]["id"]

def scopes_of(root):
    return {c["id"]: c["scope"] for c in concepts.load_concepts(root)}

# (a) the DERIVATION proposes on the card: one repo → that repo's label; no repo → global.
cards_s = {t["takeaway_id"]: t for t in review.pending(RS, maturity=0.5)}
assert cards_s[pid_s]["scope_repo"] == "claude-bus", \
    "all live evidence in one repo → the card proposes that repo's label"
assert cards_s[rid_s]["scope_repo"] == "global", \
    "evidence with no repo at all → global (a lesson with no known home applies everywhere)"

# (b) accept DEFAULT follows the derivation; the decision is the authoritative record.
cids = review.accept(pid_s, RS, assessment="a claude-bus-local lesson, faithfully quoted")
assert scopes_of(RS)[cids] == "claude-bus", "accept default = the derived scope"
ds = blobstore.latest_decision(pid_s, RS)
assert ds["verb"] == "accept" and ds["scope"] == "claude-bus"

# (c) --scope OVERRIDES the derivation (open vocabulary — any repo label) — through the CLI wiring.
buf = io.StringIO()
with redirect_stdout(buf):
    review.main(["--accept", rid_s, "--scope", "claude-bus", "--json"])
outs = json.loads(buf.getvalue())
cidr_s = outs["concept"]
assert outs["scope"] == "claude-bus" and scopes_of(RS)[cidr_s] == "claude-bus", \
    "--scope claude-bus overrides a global derivation (pinning narrower is what the override is for)"

# (d) scope is an OPEN vocabulary — free text is allowed; only an explicit BLANK is refused.
try:
    review.accept(rid_s, RS, scope="   ")
    assert False, "an explicit empty --scope must raise — nothing can live at an unnamable place"
except ValueError:
    pass

# (e) set_scope re-scopes an EXISTING concept and OUTRANKS the accept; the latest set_scope wins.
review.set_scope(cids, "global", RS, reason="it generalizes after all")
assert scopes_of(RS)[cids] == "global", "set_scope > the accept's scope (the later, deliberate call)"
buf = io.StringIO()
with redirect_stdout(buf):
    review.main(["--set-scope", cids, "my.weird_repo-2", "--reason", "no — it is repo-local"])
assert "re-scoped → my.weird_repo-2" in buf.getvalue()
assert scopes_of(RS)[cids] == "my.weird_repo-2", \
    "the LATEST set_scope wins (append-only, latest decision in force) — and free-text labels are fine"

# (f) guards: blank refused; valid targets only (a decision on a retired concept would resurrect it).
for bad_call in (lambda: review.set_scope(cids, "  ", RS),
                 lambda: review.set_scope("c-nope", "claude-bus", RS)):
    try:
        bad_call()
        assert False, "set_scope must refuse a blank scope / unknown concept"
    except ValueError:
        pass
review.retire(cidr_s, RS, reason="retired to prove the guard")
try:
    review.set_scope(cidr_s, "claude-bus", RS)
    assert False, "set_scope on a retired concept must refuse — re-scoping is never an accidental un-retire"
except ValueError:
    pass
assert cidr_s not in scopes_of(RS), "…and the retired concept stayed retired"

# (g) LEGACY: a concept with no scope anywhere — no derivation, no accept-scope, no set_scope — reads global.
legacy_s = {"id": "c-legacy-s", "title": "old concept", "statement": "predates the scope axis",
            "evidence": [], "source_takeaway": "t-old-s"}
blobstore.ingest(blobstore.canonical_json(legacy_s), source_kind="concept", source_id="c-legacy-s",
                 origin_ref={"stage": "test"}, root=RS)
assert scopes_of(RS)["c-legacy-s"] == "global", \
    "legacy concepts default global — they apply everywhere until the reviewer says otherwise"
print("OK 9 — scope: derived on the card (one repo → its label; 2+/none → global), confirmed on the")
print("       accept decision (default = derivation, --scope overrides, blank refused), re-scoped via")
print("       set_scope (latest wins, valid targets only, open vocabulary), legacy → global.")


# === 10. THE REFRESH VERB: the human-triggered re-snapshot closes the why-pending statement gap =====
# Accept never withholds on synthesize (§6), so a why-pending accept mints statement "" — and when
# synthesize later fills the claim's why, the concept must NOT follow by itself (auto-refresh would
# land unreviewed prose behind the gate). refresh is the reviewer's re-read: prose moves; evidence,
# the kind/scope folds, concept validity, and the claim's own decisions all hold.

RF = use_store("r")
seed_events([("r-s1", JJ_SEED, M_HI, 0.85, "alpha", None),
             ("r-s2", JJ_PARA, M_MID, 0.85, "alpha", None)], RF)
resolve.run(ResolveFake(["same-as-1"]), model="fake", forget=False, root=RF)
claim_r = the_claim(RF)
assert claim_r["why"] is None, "the fixture claim is why-pending (synthesize hasn't run)"
cid_r = review.accept(claim_r["id"], RF, assessment="why-pending accept — the gap under test")
c_r0 = {c["id"]: c for c in concepts.load_concepts(RF)}[cid_r]
assert c_r0["statement"] == "", "the why-pending accept minted an EMPTY statement (the gap is real)"
ev_r0 = c_r0["evidence"]
facets_r0 = (kinds_of(RF)[cid_r], scopes_of(RF)[cid_r])

# (a) refresh BEFORE anything changed refuses — idempotence by refusal: nothing new to snapshot,
# and a decision recording no change would fake a review action.
try:
    review.refresh(cid_r, RF)
    assert False, "a refresh that would change nothing must refuse, not mint a hollow decision"
except ValueError:
    pass

# (b) synthesize fills the claim's why (§8's fake, same seat); the concept does NOT move by itself.
WHY_R = "version control here is jj; git bypasses the working-copy-is-a-commit model."
synthesize.run(SynthKind({"title": JJ_SEED, "why": WHY_R, "kind": "behavioral", "confidence": 0.9}),
               model="fake", claim=claim_r["id"], root=RF)
assert the_claim(RF)["why"] == WHY_R, "the claim's live fold carries the synthesized why"
assert {c["id"]: c for c in concepts.load_concepts(RF)}[cid_r]["statement"] == "", \
    "…and the concept did NOT auto-refresh — the human gate stays the trust source"

# (c) refresh: the statement fills; title, evidence, kind/scope, validity, claim decisions all hold.
res_r = review.refresh(cid_r, RF, note="synthesize filled the why; re-read on command")
assert res_r["concept"] == cid_r and res_r["before"]["statement"] == "" \
    and res_r["after"]["statement"] == WHY_R
c_r1 = {c["id"]: c for c in concepts.load_concepts(RF)}[cid_r]
assert c_r1["statement"] == WHY_R and c_r1["title"] == JJ_SEED, "statement filled, title unchanged"
assert c_r1["evidence"] == ev_r0, "evidence carried byte-identical — refresh re-reads PROSE only"
assert (kinds_of(RF)[cid_r], scopes_of(RF)[cid_r]) == facets_r0, \
    "kind/scope hold: they are the DECISION's facts (set_*/accept folds) and refresh carries neither"
assert cid_r in concepts.valid_concept_ids(RF), \
    "the refresh decision is now the concept's LATEST — validity holds because load_concepts checks " \
    "verb MEMBERSHIP (refresh ∉ CONCEPT_INVALID_VERBS), never mere decision presence"
assert blobstore.latest_decisions(RF)[claim_r["id"]]["verb"] == "accept", \
    "refresh targets the CONCEPT id — the claim's own LIFECYCLE decision is still the accept " \
    "(latest_decisions, the fold the queue reads; latest_decision would see synthesize's marker)"
assert review.pending(RF) == [], "…so the accepted claim does not re-enter the queue"
assert [v["id"] for v in resolve.high_confidence_view(RF)] == [claim_r["id"]], \
    "…and it stays in the trusted view"
d_rf = blobstore.latest_decision(cid_r, RF)
assert d_rf["verb"] == "refresh" and d_rf["before"]["statement"] == "" \
    and d_rf["after"]["statement"] == WHY_R, "the decision records before/after (the audit trail)"

# (d) a second identical refresh refuses — the no-op guard again, now with prose in place.
try:
    review.refresh(cid_r, RF)
    assert False, "an identical re-refresh must refuse"
except ValueError:
    pass

# (e) --refresh through the CLI, riding accept's --edit-title; before/after recorded.
buf = io.StringIO()
with redirect_stdout(buf):
    review.main(["--refresh", cid_r, "--edit-title", "jj, never git", "--json"])
out_rf = json.loads(buf.getvalue())
assert out_rf["concept"] == cid_r and out_rf["after"]["title"] == "jj, never git"
c_r2 = {c["id"]: c for c in concepts.load_concepts(RF)}[cid_r]
assert c_r2["title"] == "jj, never git" and c_r2["statement"] == WHY_R, \
    "--edit-title re-titles; the statement still reads from the claim"
d_rf2 = blobstore.latest_decision(cid_r, RF)
assert d_rf2["verb"] == "refresh" and d_rf2["before"]["title"] == JJ_SEED \
    and d_rf2["after"]["title"] == "jj, never git"

# (f) guards: a gone source, a retired concept, an unknown id — all refuse.
orphan = {"id": "c-orphan", "title": "t", "statement": "s", "evidence": [],
          "source_takeaway": "t-gone"}
blobstore.ingest(blobstore.canonical_json(orphan), source_kind="concept", source_id="c-orphan",
                 origin_ref={"stage": "test"}, root=RF)
review.retire(cid_r, RF, reason="retired to prove the guard")
for bad_call in (lambda: review.refresh("c-orphan", RF),                     # source gone
                 lambda: review.refresh(cid_r, RF, edited={"title": "x"}),   # retired target
                 lambda: review.refresh("c-nope", RF)):                      # unknown id
    try:
        bad_call()
        assert False, "refresh must refuse a gone source / a retired concept / an unknown id"
    except ValueError:
        pass
assert cid_r not in concepts.valid_concept_ids(RF), "…and the retired concept stayed retired"
print("OK 10 — refresh re-snapshots the concept's prose from the claim's live fold on the reviewer's")
print("        command: never automatic, no-op refused, evidence/kind/scope/claim-decisions untouched,")
print("        retired/unknown/orphaned targets refused.")

# === 11. THE INDEX + THE CURSOR: --brief lists the queue's shape, card() renders one in full =======
# A sitting is one-card-one-verdict, so its CONTEXT must be O(1) in backlog depth: `pending(brief=True)`
# is the light index (no evidence resolution), `card(id)` the full render for the claim under the lens.

RB = use_store("brief")
seed_events([("b-s1", JJ_SEED, M_HI, 0.85, "alpha", None),
             ("b-s2", JJ_PARA, M_MID, 0.85, "alpha", None)], RB)
resolve.run(ResolveFake(["same-as-1"]), model="fake", forget=False, root=RB)
claim_b11 = the_claim(RB)
seed_takeaway(RB, id="legacy-b", title="a legacy takeaway", why="v2 prose",
              evidence=[{k: e[k] for k in ("event_id", "cleaned_hash", "byte_start", "byte_end")}
                        for e in claim_b11["evidence"]],
              support={"events": 2, "sessions": 2})

full_q = review.pending(RB)
idx, idx_total = review.pending(RB, brief=True, with_total=True)
assert [r["takeaway_id"] for r in idx] == [c["takeaway_id"] for c in full_q] and idx_total == len(full_q), \
    "the index carries the SAME ids in the SAME importance order as the full queue"
for r in idx:
    assert "evidence" not in r and "audit" not in r and "merge_suggestions" not in r, \
        f"the index resolves nothing heavy: {sorted(r)}"
    assert {"takeaway_id", "kind", "title", "entrenchment", "bar", "rationale"} <= set(r), \
        "an index row still shows the standing the verdict loop orders by"
claim_row = [r for r in idx if r["kind"] == "claim"][0]
assert claim_row["why_pending"] is True and claim_row["evidence_count"] == 2, \
    "badges ride the index (why-pending, evidence depth) — enough to plan a sitting, not to judge one"

one = review.card(claim_b11["id"], RB)
full_card = [c for c in full_q if c["takeaway_id"] == claim_b11["id"]][0]
assert one["takeaway_id"] == claim_b11["id"] and [e["quote"] for e in one["evidence"]] == \
    [e["quote"] for e in full_card["evidence"]], "card() == the queue's own render for that claim"
assert "audit" in one and "merge_suggestions" in one, "the cursor pays the FULL render: audit + suggestions"
legacy_one = review.card("legacy-b", RB)
assert legacy_one["why"] == "v2 prose" and all(e["verified"] for e in legacy_one["evidence"]), \
    "card() serves the legacy arm through the same door"
assert review.card("t-no-such-id", RB) is None, "an unknown id is None, not a crash"
print("OK 11 — the index (--brief) lists the queue's shape without resolving evidence; card() renders")
print("        exactly one claim in full — a sitting's context is one card deep at any backlog size.")

print("\nall review-claims tests passed.")
