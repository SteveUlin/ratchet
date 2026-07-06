"""review tests (v2): the backend is pure and deterministic (Claude lives in the SKILL, not here), so the
suite runs offline with FAKE route + synth Completers. Load-bearing checks, in order of what dream v2
changed and what review must still guarantee:

  THE MATURITY GATE (the v2 headline) — `pending` shows ONLY takeaways corroborated across
    `temporal.MATURITY_SESSIONS` (=2) DISTINCT sessions; a one-session takeaway INCUBATES (live + routable,
    but out of the human gate) and surfaces via `incubating`. Strengthening it across the bar (a real
    second-session dream run) moves it INTO the queue — the loop that feeds review.
  THE TRUST CHAIN — the queue is a DERIVED query over references (no stored list); each cited span
    re-resolves AND re-validates against its immutable cleaned blob ("verified real"); a malformed/foreign
    span is dropped at the read boundary, never shown verified; accept bakes ONLY re-validated spans into
    the concept.
  THE LOOP CLOSE — accept mints a concept `concepts.load_concepts` then reads; a strengthens/refines accept
    versions the NAMED concept (same id); decisions are append-only and drive every transition
    (accept/reject/snooze/edit/retire), latest decision wins; a producer `processed` marker cannot
    resurrect a reviewed takeaway.

The seeded takeaways use the v2 shape (stable minted id == source_id; sessions_seen + last_seen; NO
cluster_signature/member_events/supersedes). `review.py` itself is unchanged downstream of the gate.
Run: `python tests/test_review.py`."""
import io
import json
import os
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-review-")

from ratchet import blobstore, chunk, concepts, config, dream, glean, review, temporal  # noqa: E402
from ratchet.completer import Completion  # noqa: E402

ROOT = config.ensure_layout()

# Two disjoint durable lines: JJ appears in two sessions (→ a MATURE takeaway across distinct sessions);
# NIX appears in one (→ INCUBATING). Their first-four-word titles share no content words, so the lexical
# router never crosses them. Multibyte filler forces byte≠char offsets so the span math is real.
JJ = "always commit with jj and never use git for version control"
NIX = "run python only through nix develop because python3 is not on the path"
JJ_TITLE = " ".join(JJ.split()[:4])      # the SynthFake titles from the quote's first words
NIX_TITLE = " ".join(NIX.split()[:4])
JJ_WHY = "sulin uses jj; git annoys him."
MARKERS = {"surprise": 0.3, "insight": 0.7}


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}


def make_session(sid, line):
    """A real transcript → cleaned blob → chunkset, so the trust chain (evidence resolving to real bytes
    of an immutable blob) is genuine, not mocked."""
    records = [rec("u0", None, "user", message={"role": "user", "content": f"session {sid} kickoff"})]
    parent = "u0"
    for i in range(4):
        body = f"step {i}: " + ("λ wörk ✓ " * 20)
        if i == 2:
            body = line     # the durable line stands ALONE on its rendered line, so a line-selection
                            # (ADR-0026) resolves to EXACTLY it; multibyte filler in the other turns
                            # keeps byte≠char offsets genuine
        records.append(rec(f"{sid}a{i}", parent, "assistant", message=amsg(f"{sid}M{i}", body)))
        parent = f"{sid}a{i}"
    blob = "\n".join(json.dumps(r) for r in records) + "\n"
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid, origin_ref={"session_id": sid})
    cs, _, _ = chunk.materialize(raw_h, budget=600)
    return cs


class GleanFake:
    """Offers the given line(s) as candidates, POINTING at the numbered prompt line that carries each
    (ADR-0026: the model selects lines, the system copies their bytes). A line is found only by the chunk
    that contains it — so each session yields exactly its own durable line as a verified event."""
    def __init__(self, lines, *, markers=None, confidence=0.85):
        self.lines, self.markers, self.confidence = lines, markers or MARKERS, confidence

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
                              "summary": f"machine summary of: {ln[:24]}",
                              "markers": self.markers, "confidence": self.confidence})
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class SynthFake:
    """The SYNTH seat (Sonnet): titles the takeaway from the observation's own quote (its first words),
    so a lexical router can match a later same-theme event back to it — and so the evidence is genuine.
    `why` is fixed so the concept's statement is assertable."""
    def __init__(self, *, why=JJ_WHY, relation=None, confidence=0.8, cost=0.01):
        self.why, self.relation, self.confidence, self.cost, self.calls = why, relation, confidence, cost, 0

    def __call__(self, system, user):
        self.calls += 1
        m = re.search(r'quote: """(.*?)"""', user, re.S)
        quote = m.group(1) if m else ""
        title = " ".join(quote.split()[:4]) or "a takeaway"
        obj = {"title": title, "why": self.why, "confidence": self.confidence}
        if self.relation is not None:
            obj["relation"] = self.relation
        return Completion(text=json.dumps(obj), model="synth-fake", cost_usd=self.cost)


class RouteFake:
    """The ROUTER seat (Haiku): reads the WHOLE in-prompt catalog (no retrieval) and strengthens the
    first takeaway whose TITLE shares >= 2 content words with the observation quote, else new — the
    LLM-as-similarity-oracle behaviour without a network call."""
    def __init__(self, *, cost=0.0005):
        self.cost, self.calls = cost, 0

    def __call__(self, system, user):
        self.calls += 1
        m = re.search(r'quote: """(.*?)"""', user, re.S)
        qtokens = set(re.split(r"[^a-z0-9]+", (m.group(1) if m else "").lower())) - {""}
        for tid, title in re.findall(r"\[\d+\] id=(\S+?): (.+?) — ", user):
            ttokens = set(re.split(r"[^a-z0-9]+", title.lower())) - {""}
            if len(qtokens & ttokens) >= 2:
                return Completion(text=json.dumps({"decision": "strengthen", "takeaway_id": tid}),
                                  model="route-fake", cost_usd=self.cost)
        return Completion(text=json.dumps({"decision": "new", "takeaway_id": None}),
                          model="route-fake", cost_usd=self.cost)


def seed_events(specs):
    """session_id, line → a real cleaned blob + a glean event, so the working set + trust chain are real."""
    for sid, line in specs:
        cs = make_session(sid, line)
        glean.run([cs], GleanFake([line]), model="fake", root=ROOT)


def seed_takeaway(*, id, title, why, relation, evidence, support):
    """Ingest a v2-shape takeaway BLOB directly (source_id == the stable minted-style id), bypassing
    synthesis, to drive the lifecycle folds deterministically. `support['sessions']` decides whether the
    maturity gate lets it reach review (>= temporal.MATURITY_SESSIONS). v2 shape: NO cluster_signature/
    member_events/supersedes; ADD sessions_seen + last_seen; cites derive from the evidence event ids."""
    sessions_seen = [f"sess-{id}-{i}" for i in range(support.get("sessions", 0))]
    rec_ = {"id": id, "title": title, "why": why, "relation": relation,
            "cites": [e.get("event_id") for e in evidence], "evidence": evidence, "support": support,
            "sessions_seen": sessions_seen, "markers": {k: 0.0 for k in glean.MARKER_KINDS},
            "confidence": 0.8, "last_seen": "2024-01-01T00:00:00+00:00"}
    blobstore.ingest(blobstore.canonical_json(rec_), source_kind="takeaway", source_id=id,
                     origin_ref={"stage": "dream", "model": "seed"}, root=ROOT)
    return id


def pending_ids(root=ROOT):
    return {t["takeaway_id"] for t in review.pending(root)}


# Drive the GENUINE v2 pipeline: JJ over two sessions (strengthens into ONE 2-session MATURE takeaway),
# NIX over one (a 1-session INCUBATING takeaway). Forget off so stragglers aren't aged during the run.
seed_events([("rev-jj1", JJ), ("rev-jj2", JJ), ("rev-nix", NIX)])
rep = dream.run(RouteFake(), SynthFake(), route_model="fake", synth_model="fake", forget=False)
assert rep.takeaways, "dream produced takeaways to review"
cur = dream.catalog(ROOT)
jj_tk = [t for t in cur if t["support"]["events"] == 2][0]    # JJ: two events across two distinct sessions
nix_tk = [t for t in cur if t["support"]["events"] == 1][0]   # NIX: a single event/session
assert jj_tk["support"]["sessions"] == 2 and nix_tk["support"]["sessions"] == 1, "JJ matured; NIX incubating"


# --- 1. THE MATURITY GATE: only a corroborated-across-distinct-sessions takeaway reaches the queue ----

q_ids = pending_ids()
assert q_ids == {jj_tk["id"]}, f"the queue is ONLY the mature takeaway — NIX (1 session) is gated out: {q_ids}"
assert nix_tk["id"] not in q_ids, "a single-session takeaway does NOT fire the human gate"
# the gate is dream.current_takeaways; the full catalog still holds NIX (live + routable, just not reviewable).
assert {t["id"] for t in dream.current_takeaways(ROOT)} == {jj_tk["id"]}, "current_takeaways = the mature set"
assert {t["id"] for t in dream.catalog(ROOT)} == {jj_tk["id"], nix_tk["id"]}, "the catalog keeps the incubating one"
# incubating() surfaces what is accruing toward review, with the distinct-session shortfall.
inc = review.incubating(ROOT)
assert {t["takeaway_id"] for t in inc} == {nix_tk["id"]}, "the incubating view = catalog minus the mature set"
inc_nix = inc[0]
assert inc_nix["title"] == NIX_TITLE and inc_nix["support"]["sessions"] == 1, "incubating projects title + support"
assert inc_nix["needs"] == temporal.MATURITY_SESSIONS - 1 == 1, "`needs` = further distinct sessions to mature"
print("OK 1 — maturity gate: only the 2-session takeaway is reviewable; the 1-session one incubates "
      "(live, out of the queue) and shows in `--incubating` with its shortfall.")

# --- 1b. THE BAR IS THE REVIEWER'S KNOB, and TRANSPARENT (ADR-0027): not a hidden constant -----------
# Every surfaced takeaway carries its score-vs-bar rationale — pending AND incubating.
jj_view = [t for t in review.pending(ROOT) if t["takeaway_id"] == jj_tk["id"]][0]
assert jj_view["bar"] == temporal.MATURITY_WEIGHT and jj_view["entrenchment"] >= jj_view["bar"], \
    "a pending takeaway shows its entrenchment >= the bar"
assert jj_view["mature"] and "≥ bar" in jj_view["rationale"], "pending carries a plain why it cleared the bar"
assert "rationale" in inc_nix and inc_nix["entrenchment"] < inc_nix["bar"], \
    "an incubating takeaway shows its score < bar + the reason"
assert "RECURRING" in inc_nix["rationale"] or "RECENT" in inc_nix["rationale"], \
    "the incubating reason explains corroboration-as-durability, not a bare count"

# LOWERING the bar (the operator's call) graduates the once-incubating NIX into the queue — no re-mining,
# no hidden rule: the same score, a bar the reviewer moved.
low = {t["takeaway_id"] for t in review.pending(ROOT, maturity=0.5)}
assert nix_tk["id"] in low and jj_tk["id"] in low, "lowering --maturity surfaces the incubating takeaway too"
assert nix_tk["id"] not in {t["takeaway_id"] for t in review.incubating(ROOT, maturity=0.5)}, \
    "and it leaves the incubating view at the lowered bar (the two views stay complementary at any bar)"
# RAISING the bar withholds even the 2-session one — the reviewer can demand more corroboration.
assert review.pending(ROOT, maturity=99.0) == [], "raising the bar above all scores empties the queue"
print("OK 1b — the maturity bar is an explained, --maturity-adjustable knob; every takeaway shows its "
      "score vs the bar and why; lowering it graduates incubating ones with no re-mining.")


# --- 2. THE LOOP IN: strengthening an incubating takeaway ACROSS the bar makes it reviewable ---------

# a real second NIX session → a new un-consolidated event → dream routes it to STRENGTHEN the NIX takeaway
# (same stable id, new version) → it crosses the distinct-session bar and ENTERS the queue.
seed_events([("rev-nix2", NIX)])
route2, synth2 = RouteFake(), SynthFake()
rep2 = dream.run(route2, synth2, route_model="fake", synth_model="fake", forget=False)
assert rep2.n_strengthened == 1 and rep2.n_events == 1, "only the new NIX event is processed — a strengthen"
matured = [t for t in dream.current_takeaways(ROOT) if t["id"] == nix_tk["id"]]
assert matured and matured[0]["support"]["sessions"] == 2, "NIX now spans 2 distinct sessions (matured in place)"
assert nix_tk["id"] in pending_ids(), "the just-matured takeaway has ENTERED the review queue (the loop feeds review)"
assert nix_tk["id"] not in {t["takeaway_id"] for t in review.incubating(ROOT)}, "and left the incubating view"
print("OK 2 — strengthening a 1-session takeaway to the bar (a real 2nd-session run) moves it from "
      "incubating INTO the queue — the maturity transition that feeds the human gate.")


# --- 3. the queue is a derived query; evidence re-resolves + RE-VALIDATES ("verified real") ----------

jj_q = [t for t in review.pending(ROOT) if t["takeaway_id"] == jj_tk["id"]][0]
assert jj_q["title"] == JJ_TITLE and jj_q["support"]["events"] == 2, "the presented takeaway is the synthesis"
assert jj_q["evidence"] and all(e["verified"] for e in jj_q["evidence"]), "evidence is resolved + verified"
for e in jj_q["evidence"]:
    data = blobstore.get(e["cleaned_hash"]).encode("utf-8")
    assert data[e["byte_start"]:e["byte_end"]].decode() == e["quote"] == JJ, "the verified quote resolves to real bytes"
    assert "context" in e and JJ in e["context"], "a surrounding-context window is provided for the deep path"
print("OK 3 — queue: a derived query over references; each cited span re-resolved + re-validated against "
      "its immutable blob (verified real).")


# --- 4. accept mints a concept and CLOSES THE LOOP (concepts.load_concepts then sees it) ----------------

assert concepts.load_concepts(ROOT) == [], "no concepts before any accept"
cid = review.accept(jj_tk["id"], assessment="why follows the cited quotes", note="clear accept")
assert cid.startswith("c-")
cs = concepts.load_concepts(ROOT)
assert [c["id"] for c in cs] == [cid], "the accepted takeaway is now a concept dream reads (loop closed)"
c = cs[0]
assert c["title"] == JJ_TITLE and c["statement"] == JJ_WHY, "the concept carries the synthesis"
# the concept's stored evidence must itself RE-RESOLVE to real bytes — accept bakes only the verified spans.
ce = c["evidence"][0]
assert blobstore.get(ce["cleaned_hash"]).encode("utf-8")[ce["byte_start"]:ce["byte_end"]].decode() == JJ, \
    "the concept's stored evidence span resolves to the verbatim quote"
assert jj_tk["id"] not in pending_ids(), "an accepted takeaway leaves the queue"
d = blobstore.latest_decision(jj_tk["id"], ROOT)
assert d["verb"] == "accept" and d["concept"] == cid and d["assessment"].startswith("why follows")
print("OK 4 — accept: takeaway → concept blob; concepts.load_concepts sees it (loop closed); decision "
      "records provenance; only verified spans are baked in.")


# --- 5. accept of a strengthens/refines takeaway UPDATES the named concept (new version, same id) ----

before_h = blobstore.latest_version(cid, ROOT)
seed_takeaway(id="grow-jj", title="jj over git (more)", why="more evidence sulin uses jj.",
              relation={"kind": "strengthens", "concept_id": cid}, evidence=jj_tk["evidence"],
              support={"events": 3, "sessions": 3})
cid2 = review.accept("grow-jj")
assert cid2 == cid, "a strengthens accept reuses the concept id from the relation (does not mint a new one)"
assert blobstore.latest_version(cid, ROOT) != before_h, "the concept gained a NEW version (refinement)"
assert concepts.load_concepts(ROOT)[0]["statement"] == "more evidence sulin uses jj.", "latest version wins"
assert len([c for c in concepts.load_concepts(ROOT) if c["id"] == cid]) == 1, "still one valid concept per id"
print("OK 5 — accept (strengthens): updates the named concept as a new version; latest wins; one per id.")


# --- 6. reject removes from the queue; the decision is a label (negative signal) ---------------------

review.reject(nix_tk["id"], reason="not durable", assessment="one-off after all")
assert nix_tk["id"] not in pending_ids(), "a rejected takeaway leaves the queue"
assert blobstore.latest_decision(nix_tk["id"], ROOT)["verb"] == "reject"
print("OK 6 — reject: leaves the queue; persisted as a label for future negative few-shot.")


# --- 7. snooze defers a MATURE takeaway until a concrete time; an unparseable trigger is refused -----

sn = seed_takeaway(id="snoozeme", title="maybe durable", why="seen twice so far.",
                   relation={"kind": "new", "concept_id": None}, evidence=jj_tk["evidence"],
                   support={"events": 2, "sessions": 2})         # mature → in the queue absent a snooze
assert sn in pending_ids(), "a mature takeaway is in the queue before any snooze"
review.snooze(sn, until="2999-01-01T00:00:00+00:00")
assert sn not in pending_ids(), "a future-snoozed takeaway is out of the queue"
review.snooze(sn, until="2000-01-01T00:00:00+00:00")            # a newer decision whose time has passed
assert sn in pending_ids(), "a due snooze re-surfaces (latest decision wins)"
for bad in (None, "", "next week", "2026-13-99"):              # an unparseable/empty trigger would be a graveyard
    try:
        review.snooze(sn, until=bad)
        assert False, f"snooze --until {bad!r} must be refused"
    except ValueError:
        pass
print("OK 7 — snooze: time trigger defers + re-surfaces (latest wins); an unparseable --until is refused.")


# --- 8. edit captures before/after; the concept uses the corrected content ---------------------------

ed = seed_takeaway(id="editme", title="loose claim", why="overgeneralized.",
                   relation={"kind": "new", "concept_id": None}, evidence=jj_tk["evidence"],
                   support={"events": 2, "sessions": 2})
ecid = review.accept(ed, edited={"title": "tight claim", "why": "narrowed to what the evidence supports."},
                     assessment="why overreached; narrowed it")
econcept = [c for c in concepts.load_concepts(ROOT) if c["id"] == ecid][0]
assert econcept["title"] == "tight claim" and econcept["statement"].startswith("narrowed"), "concept uses the edit"
edec = blobstore.latest_decision(ed, ROOT)
assert edec["verb"] == "edit" and edec["edited"]["before"]["title"] == "loose claim", "edit captures before/after"
print("OK 8 — edit: the corrected synthesis becomes the concept; before/after captured as the correction signal.")


# --- 9. retire drops a concept from the valid set (not a deletion) -----------------------------------

assert cid in {c["id"] for c in concepts.load_concepts(ROOT)}, "concept is valid before retire"
review.retire(cid, reason="superseded by a clearer concept")
assert cid not in {c["id"] for c in concepts.load_concepts(ROOT)}, "a retired concept leaves the valid set"
assert blobstore.has(blobstore.latest_version(cid, ROOT), ROOT), "but the concept blob + history remain (no deletion)"
print("OK 9 — retire: concept leaves the valid set via the latest decision; the immutable history is kept.")


# --- 10. evidence hardening: a malformed/foreign span is never shown 'verified' ---------------------

bad = seed_takeaway(id="badspan", title="x", why="y", relation={"kind": "new", "concept_id": None},
                    evidence=[{"event_id": "z", "cleaned_hash": jj_tk["evidence"][0]["cleaned_hash"],
                               "byte_start": None, "byte_end": 999999}], support={"events": 1, "sessions": 1})
present = review.context_for(bad, ROOT)
assert present["evidence"] == [], "a null/overshoot span resolves to nothing — never the whole blob as 'verified'"
print("OK 10 — evidence: malformed/foreign spans rejected at the review read boundary (trust chain intact).")


# --- 11. regression battery -------------------------------------------------------------------------

# (a) accept re-validates evidence into the CONCEPT — a malformed span is filtered out, not baked in.
real_ev = jj_tk["evidence"][0]
mix = seed_takeaway(id="mixev", title="mixed", why="one good, one bad span.",
                    relation={"kind": "new", "concept_id": None},
                    evidence=[real_ev, {"event_id": "bad", "cleaned_hash": real_ev["cleaned_hash"],
                                        "byte_start": None, "byte_end": 999999}],
                    support={"events": 1, "sessions": 1})
mcid = review.accept(mix, assessment="kept the verifiable span")
mconcept = [c for c in concepts.load_concepts(ROOT) if c["id"] == mcid][0]
assert len(mconcept["evidence"]) == 1, "the malformed span is filtered OUT of the concept (only verified spans)"
me = mconcept["evidence"][0]
assert blobstore.get(me["cleaned_hash"]).encode("utf-8")[me["byte_start"]:me["byte_end"]].decode() == JJ

# (b) a takeaway with NO resolvable evidence is refused (a belief with no anchor) unless overridden.
noev = seed_takeaway(id="noev", title="floats", why="no quotes back this.",
                     relation={"kind": "new", "concept_id": None}, evidence=[], support={"events": 1, "sessions": 1})
try:
    review.accept(noev)
    assert False, "accept of a no-evidence takeaway must be refused"
except ValueError:
    pass
assert review.accept(noev, allow_no_evidence=True), "the override mints it deliberately"

# (c) a strengthens/refines pointing at a NON-EXISTENT concept id mints fresh (no phantom concept).
ph = seed_takeaway(id="phantom", title="p", why="claims to strengthen a ghost.",
                   relation={"kind": "strengthens", "concept_id": "c-doesnotexist"},
                   evidence=jj_tk["evidence"], support={"events": 1, "sessions": 1})
pcid = review.accept(ph)
assert pcid != "c-doesnotexist", "a stale relation.concept_id is ignored — a fresh concept is minted"

# (d) THE RESURRECTION GUARD: a MATURE takeaway (so it would be in the queue) is accepted; a later dream
#     'processed' marker on the SAME id must NOT shadow the accept and bring it back into the queue.
acc = seed_takeaway(id="resurrect", title="r", why="accept me.", relation={"kind": "new", "concept_id": None},
                    evidence=jj_tk["evidence"], support={"events": 2, "sessions": 2})
assert acc in pending_ids(), "the mature takeaway is in the queue before accept (the guard is non-vacuous)"
review.accept(acc, assessment="ok")
assert acc not in pending_ids(), "accepted → out of the queue"
blobstore.ingest(blobstore.canonical_json({"verb": "processed", "target": acc, "stage": "dream",
                                           "producer": {"stage": "dream"}}),
                 source_kind="decision", source_id="proc-resurrect", prev=None,
                 origin_ref={"stage": "dream"}, fetched_at=config.now())
assert acc not in pending_ids(), \
    "a later 'processed' marker must NOT resurrect an accepted takeaway (the queue fold excludes producer markers)"
print("OK 11 — regression: accept re-validates evidence into the concept; no-evidence refused; stale")
print("        concept_id mints fresh; a producer 'processed' marker can't resurrect a reviewed takeaway.")

# --- 12. THE SITTING DEFAULT (CLI): --pending is a bounded top-10 slice; --limit 0 = everything ------
# A growing backlog must never load whole into a sitting: the CLI defaults to the top SITTING_LIMIT by
# importance, the header states "top N of M" (the backlog depth without loading it), and --limit 0 is
# the explicit escape hatch. The LIBRARY default stays unlimited (status counts the full queue with it).

def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        review.main(argv)
    return buf.getvalue()


for i in range(12):                                # a backlog wider than one sitting
    seed_takeaway(id=f"bulk-{i:02d}", title=f"bulk takeaway {i}", why="a sitting-slice fixture.",
                  relation={"kind": "new", "concept_id": None}, evidence=jj_tk["evidence"],
                  support={"events": 2, "sessions": 2})
full = review.pending(ROOT)                        # the library default: EVERYTHING (no hidden cap)
total = len(full)
assert total > review.SITTING_LIMIT == 10, f"the backlog exceeds one sitting ({total})"

q10 = json.loads(run_cli(["--pending", "--json"]))
assert len(q10) == review.SITTING_LIMIT, f"the CLI sitting default is a top-{review.SITTING_LIMIT} slice"
assert [t["takeaway_id"] for t in q10] == [t["takeaway_id"] for t in full[:review.SITTING_LIMIT]], \
    "the slice is the TOP of the importance order — never an arbitrary ten"
assert len(json.loads(run_cli(["--pending", "--json", "--limit", "3"]))) == 3, "--limit N still narrows"
assert len(json.loads(run_cli(["--pending", "--json", "--limit", "0"]))) == total, \
    "--limit 0 = everything (the explicit escape hatch)"

head = run_cli(["--pending"]).splitlines()[0]
assert head == f"showing top {review.SITTING_LIMIT} of {total} pending (by importance) — --limit to widen", \
    f"the header states the slice honestly, with the backlog depth: {head!r}"
head_all = run_cli(["--pending", "--limit", "0"]).splitlines()[0]
assert head_all == f"showing all {total} pending (by importance)", \
    f"an unsliced render still states the total: {head_all!r}"

cards, t2 = review.pending(ROOT, limit=2, with_total=True)
assert len(cards) == 2 and t2 == total, "with_total returns the backlog depth beside the slice"
assert len(review.pending(ROOT, limit=0)) == total, "limit 0 is unlimited at the function level too"

# --incubating shares the sitting default and the honest header (nearest the bar first).
for i in range(11):
    seed_takeaway(id=f"inc-{i:02d}", title=f"incubating {i}", why="below the bar.",
                  relation={"kind": "new", "concept_id": None}, evidence=jj_tk["evidence"],
                  support={"events": 1, "sessions": 1})
inc_total = len(review.incubating(ROOT))
assert inc_total > review.SITTING_LIMIT, f"the incubating backlog exceeds one sitting ({inc_total})"
assert len(json.loads(run_cli(["--incubating", "--json"]))) == review.SITTING_LIMIT, \
    "--incubating defaults to the same sitting slice"
inc_head = run_cli(["--incubating"]).splitlines()[0]
assert inc_head.startswith(f"showing {review.SITTING_LIMIT} of {inc_total} takeaway(s) below the maturity bar"), \
    f"the incubating header states its slice too: {inc_head!r}"
assert len(json.loads(run_cli(["--incubating", "--json", "--limit", "0"]))) == inc_total
print("OK 12 — the sitting default: --pending/--incubating load a bounded top-10 slice (importance-"
      "ordered), the header says 'top N of M' so the backlog depth is always visible, and --limit 0")
print("        is the explicit everything escape hatch (the library functions stay unlimited by default).")

print("\nall review tests passed.")
