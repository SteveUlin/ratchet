"""dream v2 tests: incremental working-set consolidation, exercised OFFLINE with FAKE Completers (no
network, no API key), so the suite is deterministic. dream v2 routes each un-consolidated glean event
to NEW / STRENGTHEN / NOOP against an IN-PROMPT catalog (the LLM as the similarity oracle — no
embeddings) and maintains evidence-cited takeaways with STABLE minted ids. The load-bearing checks:

  WORKING SET / CATALOG — the two derived views: working_set folds `consolidated`/`stale` decisions
    (and IGNORES producer `processed` markers); catalog excludes merge/retire/reject; current_takeaways
    is the maturity gate (distinct-session bar). salience orders the priority queue.
  ROUTE — `_clean_route` coercions (strengthen of an unknown id → new; unknown verb → noop; new clears
    id); cost accounted; the list-number is mapped back to an id.
  SYNTHESIZE / STRENGTHEN — a new takeaway gets a deterministic minted id + robust-anchored evidence
    (span + verbatim quote + context, re-validated on write) + support {1,1}; a strengthen bumps DISTINCT
    sessions (a re-strengthen of an already-cited event is a byte-identical no-op that does NOT inflate);
    the drift gate re-synthesizes only on a distant event.
  THE BLOCK — drive `block.run(DreamBlock(...))`: per-EVENT commit (takeaway + `consolidated` land in
    process), the ON-INSTANCE catalog updates WITHIN a run (event N's new takeaway is what event N+1
    strengthens), priority orders the queue, a consolidated event does zero LLM work on re-run, a noop is
    parked by its processed marker, error isolation + budget stop + resume-from-crash.
  TRUST CHAIN — every takeaway's evidence re-resolves to the verbatim bytes of an immutable blob; a
    malformed event span is dropped at the read boundary.

A live smoke is gated behind RATCHET_LIVE_TEST=1. Run: `python tests/test_dream.py`."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-dream-")

from ratchet import blobstore, block, chunk, completer, config, dream, glean  # noqa: E402
from ratchet.completer import Completion  # noqa: E402
from ratchet.dream import (  # noqa: E402
    apply, catalog, current_takeaways, mint_takeaway_id, render_catalog, route, salience,
    synthesize_new, takeaway_content, update_support, working_set, _clean_route)


# Distinctive durable lines: the two JJ lines share content (so a lexical router strengthens one with
# the other); the NIX line is disjoint. Multibyte filler forces byte≠char offsets so the span math is
# genuinely exercised end to end.
JJ1 = "always commit with jj and never use git for version control"
JJ2 = "remember to commit with jj, never git, for version control here"
NIX = "run python only through nix develop because python3 is not on the path"

# salience-shaping markers: surprise is weighted highest, so the NIX event (high surprise) outranks the
# JJ events — letting us assert the priority queue processes by salience, not arrival.
M_HI = {"surprise": 0.9, "insight": 0.3}     # high salience
M_MID = {"surprise": 0.2, "insight": 0.7}    # medium
M_LO = {"surprise": 0.1, "insight": 0.5}     # lower


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
            body = line     # the durable line stands ALONE on its rendered line, so a line-selection
                            # (ADR-0026) resolves to EXACTLY it; the multibyte filler in the OTHER turns
                            # still pushes its byte offset past its char offset (byte≠char under test)
        records.append(rec(f"{sid}a{i}", parent, "assistant", message=amsg(f"{sid}M{i}", body)))
        parent = f"{sid}a{i}"
    blob = "\n".join(json.dumps(r) for r in records) + "\n"
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid, origin_ref={"session_id": sid})
    cs, _, _ = chunk.materialize(raw_h, budget=600)
    return cs


class GleanFake:
    """Returns a candidate per given line, POINTING at the numbered prompt line that carries it (ADR-0026:
    the model selects lines, the system copies their bytes). A line is 'found' only by the chunk that
    actually contains it — so each session yields exactly its own durable line as an event, with the
    salience signal we asked for. (Each durable line stands alone in its turn, so the selected line's
    bytes ARE the line — see make_session.)"""
    def __init__(self, lines, *, markers=None, confidence=0.85, relevance=None):
        self.lines, self.markers, self.confidence, self.calls = lines, markers or {}, confidence, 0
        self.relevance = relevance   # the per-event novelty verdict (4b); None → field omitted (→ novel)

    def __call__(self, system, user):
        self.calls += 1
        line_of = {}
        for row in user.splitlines():
            num, sep, body = row.partition("| ")
            if sep and num.strip().isdigit():
                line_of[int(num)] = body
        cands = []
        for ln in self.lines:
            hit = next((n for n, body in line_of.items() if ln in body), None)
            if hit is None:
                continue                     # this line is not in this chunk → nothing to point at
            c = {"lines": {"from": hit, "to": hit}, "summary": f"machine summary of: {ln[:24]}",
                 "markers": self.markers.get(ln, M_MID), "confidence": self.confidence}
            if self.relevance is not None:
                c["relevance"] = self.relevance
            cands.append(c)
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class SynthFake:
    """A realistic SYNTH fake (Sonnet's seat): it TITLES the takeaway from the observation's own quote
    (the first words), so a downstream lexical router can match a later same-theme event to it — and so
    the trust chain is real. Knobs simulate the model's choices (drop/empty-why/relation)."""
    def __init__(self, *, why="the durable underlying principle worth keeping", relation=None,
                 confidence=0.8, drop=False, cost=0.01):
        self.why, self.relation, self.confidence = why, relation, confidence
        self.drop, self.cost, self.calls = drop, cost, 0

    def __call__(self, system, user):
        self.calls += 1
        m = re.search(r'quote: """(.*?)"""', user, re.S)
        quote = m.group(1) if m else ""
        title = " ".join(quote.split()[:4]) or "a takeaway"
        obj = {"title": title, "why": self.why, "confidence": self.confidence, "drop": self.drop}
        if self.relation is not None:
            obj["relation"] = self.relation
        return Completion(text=json.dumps(obj), model="synth-fake", cost_usd=self.cost)


class RouteFake:
    """A realistic ROUTER fake (Haiku's seat): it reads the WHOLE in-prompt catalog (no retrieval) and
    strengthens the first takeaway whose TITLE shares >= 2 content words with the observation quote, else
    new — the LLM-as-similarity-oracle behaviour, without a network call. Counts calls so a re-run that
    does zero LLM work is checkable."""
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


class EchoRoute:
    """Echoes a FIXED route verdict (or raw text) — for the route/apply unit tests where we drive an
    explicit decision rather than the lexical heuristic."""
    def __init__(self, decision=None, takeaway_id=None, *, raw=None, cost=0.0007):
        self.decision, self.takeaway_id, self.raw, self.cost, self.calls = decision, takeaway_id, raw, cost, 0

    def __call__(self, system, user):
        self.calls += 1
        text = self.raw if self.raw is not None else json.dumps(
            {"decision": self.decision, "takeaway_id": self.takeaway_id})
        return Completion(text=text, model="route-fake", cost_usd=self.cost)


def use_store(prefix):
    """An ISOLATED data root per section — `consolidated`/`stale`/takeaway writes mutate the derived
    views, so sections must not share a store. Sets RATCHET_DATA_DIR so `config.data_root()` (which the
    LLM-seam helpers default to) resolves to this dir."""
    d = tempfile.mkdtemp(prefix=f"ratchet-test-dream-{prefix}-")
    os.environ["RATCHET_DATA_DIR"] = d
    return config.ensure_layout()


def seed_events(specs, root):
    """Build real sessions → real cleaned blobs → glean events (with controlled markers/confidence), so
    the working set + trust chain are genuine. `specs` = [(session_id, line, markers, confidence)]."""
    for sid, line, markers, conf in specs:
        cs = make_session(sid, line)
        glean.run([cs], GleanFake([line], markers={line: markers}, confidence=conf), model="fake", root=root)


def by_quote(root):
    return {rv.quote: rv for rv in working_set(root)}


def seed_takeaway_v2(*, id, title, why, cites, evidence, sessions_seen, markers=None,
                     confidence=0.8, last_seen="2024-01-01T00:00:00+00:00", root=None):
    """Ingest a v2-shape takeaway BLOB directly (source_id = the stable minted-style id), bypassing
    synthesis, to drive the catalog/merge folds deterministically."""
    rec_ = {"id": id, "title": title, "why": why,
            "relation": {"kind": "new", "concept_id": None, "note": ""},
            "cites": cites, "evidence": evidence,
            "support": {"events": len(set(cites)), "sessions": len(set(sessions_seen))},
            "sessions_seen": sessions_seen, "markers": markers or {k: 0.0 for k in glean.MARKER_KINDS},
            "confidence": confidence, "last_seen": last_seen}
    blobstore.ingest(blobstore.canonical_json(rec_), source_kind="takeaway", source_id=id,
                     origin_ref={"stage": "dream", "model": "seed"}, root=root)
    return id


# === 1. WORKING SET + CATALOG + the maturity gate + salience + _clean_route + render_catalog =========

R1 = use_store("derivations")
seed_events([("d-jj1", JJ1, M_MID, 0.85), ("d-jj2", JJ2, M_LO, 0.85), ("d-nix", NIX, M_HI, 0.85)], R1)

# working_set re-anchors every un-consolidated event to its TRUSTED quote + resolves the session.
ws = working_set(R1)
assert {rv.quote for rv in ws} == {JJ1, JJ2, NIX}, "every event re-anchored to its verbatim quote"
assert {rv.session_id for rv in ws} == {"d-jj1", "d-jj2", "d-nix"}, "distinct sessions resolved via lineage"
ev_id = {rv.quote: rv.id for rv in ws}

# folding: a `consolidated` decision and a `stale` decision each remove an event; a producer `processed`
# marker does NOT (it is bookkeeping, not membership state — the resurrection-bug guard review relies on).
dream._write_consolidated(ev_id[JJ1], "t-deadbeef0001", "new", run_id="r-fold", root=R1)
dream._write_stale(ev_id[NIX], "noise", run_id="r-fold", root=R1)
block.write_processed("dream", ev_id[JJ2], (("prompt_version", dream.PROMPT_VERSION), ("model", "x")),
                      n_outputs=0, cost_usd=0.0, run_id="r-fold", extra={}, root=R1)
ws2 = working_set(R1)
assert {rv.quote for rv in ws2} == {JJ2}, \
    "consolidated + stale fold OUT of the working set; a producer 'processed' marker does NOT"

# salience orders the priority queue: surprise weighted highest → NIX (high surprise) outranks the JJ
# events; and DreamBlock.priority delegates to salience.
sal = {rv.quote: salience(rv.event) for rv in ws}
assert sal[NIX] > sal[JJ1] > sal[JJ2], f"salience ranks high-surprise first: {sal}"
blk_for_prio = dream.DreamBlock(RouteFake(), SynthFake(), route_model="fake", synth_model="fake")
nix_rv = [rv for rv in ws if rv.quote == NIX][0]
assert blk_for_prio.priority(nix_rv) == salience(nix_rv.event), "DreamBlock.priority delegates to salience"

# RELEVANCE (4b/ADR-0019) scales salience RECALL-FIRST: for otherwise-equal events, contradicts > novel >
# known, so dream drains novel/contradicting events first and SINKS (never drops) already-known ones. A
# missing relevance field coerces to `novel` (×1.0), so a pre-4b event keeps its EXACT prior salience.
_relbase = {"confidence": 0.8, "markers": {"surprise": 0.5, "insight": 0.2, "research": 0.0}}
_s_contra = salience({**_relbase, "relevance": "contradicts"})
_s_novel = salience({**_relbase, "relevance": "novel"})
_s_known = salience({**_relbase, "relevance": "known"})
assert _s_contra > _s_novel > _s_known, f"contradicts > novel > known for equal events: {(_s_contra, _s_novel, _s_known)}"
assert salience(_relbase) == _s_novel, "a missing relevance coerces to novel (×1.0) — pre-4b salience preserved"
assert salience({**_relbase, "relevance": "bogus"}) == _s_novel, "an unknown verdict coerces to novel, never sinks"
assert _s_known > 0, "known only SINKS in the queue — never zero, never a hard drop (recall-first)"

# _clean_route: the defensive coercion of the untrusted router JSON.
assert _clean_route({"decision": "new", "takeaway_id": "t-x"}, {"t-x"}) == {"decision": "new", "takeaway_id": None}, \
    "new clears the takeaway_id (a new mints fresh)"
assert _clean_route({"decision": "strengthen", "takeaway_id": "t-x"}, {"t-x"}) == \
    {"decision": "strengthen", "takeaway_id": "t-x"}, "a strengthen of a KNOWN id is kept"
assert _clean_route({"decision": "strengthen", "takeaway_id": "t-ghost"}, {"t-x"})["decision"] == "new", \
    "a strengthen of an id NOT in the catalog is coerced to new (the spec's explicit rule)"
assert _clean_route({"decision": "strengthen", "takeaway_id": None}, {"t-x"})["decision"] == "new", \
    "a strengthen with a null id is coerced to new"
assert _clean_route({"decision": "bogus"}, {"t-x"}) == {"decision": "noop", "takeaway_id": None}, \
    "an unknown decision verb → noop (no write, re-routable on a prompt bump)"
assert _clean_route({}, set()) == {"decision": "noop", "takeaway_id": None}, "empty/garbage → noop"

# render_catalog: the in-prompt numbered list, with a sentinel for the empty catalog.
assert render_catalog([]) == "(no takeaways yet — every observation is new)", "empty catalog renders a sentinel"
rendered = render_catalog([{"id": "t-abc", "title": "jj over git", "why": "sulin uses jj"}])
assert rendered.startswith("[1] id=t-abc: jj over git — "), f"numbered '[N] id=..: title — why' shape: {rendered}"

# catalog excludes merge/retire/reject; current_takeaways is the DISTINCT-SESSION maturity gate.
R1b = use_store("catalog")
seed_takeaway_v2(id="t-keep", title="keep me", why="durable", cites=["e1", "e2"], sessions_seen=["s1", "s2"],
                 evidence=[], root=R1b)                                       # mature (2 sessions)
seed_takeaway_v2(id="t-incub", title="incubating", why="thin", cites=["e3"], sessions_seen=["s3"],
                 evidence=[], root=R1b)                                       # incubating (1 session)
seed_takeaway_v2(id="t-rej", title="rejected", why="x", cites=["e4"], sessions_seen=["s4"], evidence=[], root=R1b)
seed_takeaway_v2(id="t-merged", title="merged away", why="y", cites=["e5"], sessions_seen=["s5"], evidence=[], root=R1b)
dream._write_decision({"verb": "reject", "target": "t-rej", "at": config.now()}, run_id="r", root=R1b)
dream._write_merge("t-merged", "t-keep", run_id="r", root=R1b)
cat_ids = {t["id"] for t in catalog(R1b)}
assert cat_ids == {"t-keep", "t-incub"}, f"catalog drops reject + merge losers, keeps incubating: {cat_ids}"
cur_ids = {t["id"] for t in current_takeaways(R1b)}
assert cur_ids == {"t-keep"}, f"maturity gate: only the >= {dream.MATURITY_SESSIONS}-session takeaway reaches review: {cur_ids}"
print("OK §1 — working_set folds consolidated/stale (ignores 'processed'); catalog drops merge/retire/reject;")
print("        maturity gate counts distinct sessions; salience orders the queue; _clean_route + render_catalog.")


# === 2. SYNTHESIZE NEW: deterministic minted id, robust-anchored evidence, support {1,1}, drop → None =

R2 = use_store("synth")
seed_events([("syn-s1", JJ1, M_MID, 0.85)], R2)
rv = working_set(R2)[0]
tk, cost = synthesize_new(rv, SynthFake(), [], known_concept_ids=set(), model="fake", run_id="r")
assert tk["id"] == mint_takeaway_id(rv.id) == "t-" + __import__("hashlib").sha256(rv.id.encode()).hexdigest()[:12], \
    "the takeaway id is the STABLE, DETERMINISTIC hash of the seeding event id"
assert tk["support"] == {"events": 1, "sessions": 1} and tk["cites"] == [rv.id], "support {1,1}, cites the one event"
assert tk["sessions_seen"] == [rv.session_id], "the distinct-session set is seeded"
ev = tk["evidence"][0]
data = blobstore.get(ev["cleaned_hash"]).encode("utf-8")
assert data[ev["byte_start"]:ev["byte_end"]].decode() == JJ1 == ev["quote"], \
    "the evidence is a ROBUST ANCHOR: the byte span AND the verbatim quote, resolving to real bytes"
assert "context" in ev and JJ1 in ev["context"], "a surrounding-context window rides alongside (W3C robust anchoring)"
assert cost > 0 and tk["producer"]["cost_usd"] > 0, "synthesis cost is accounted"
# drop / no-usable-why → None (a successful adjudication of noise, not an error)
assert synthesize_new(rv, SynthFake(drop=True), [], known_concept_ids=set(), model="fake", run_id="r")[0] is None, \
    "drop yields no takeaway"
assert synthesize_new(rv, SynthFake(why=""), [], known_concept_ids=set(), model="fake", run_id="r")[0] is None, \
    "an empty why yields no takeaway"
# the STORED projection drops the run-varying producer (so a no-op re-version re-hashes identically).
assert "producer" not in takeaway_content(tk), "takeaway_content drops the in-memory producer"
print("OK §2 — synthesize_new: deterministic minted id, evidence resolves to verbatim bytes + context,")
print("        support {1,1}, drop/empty-why → None, stored projection drops producer.")


# === 3. UPDATE SUPPORT: distinct-session bump + maturity, idempotent no-op, the drift gate ============

R3 = use_store("update")
seed_events([("up-s1", JJ1, M_MID, 0.85), ("up-s2", JJ2, M_MID, 0.85)], R3)
wm = by_quote(R3)
rv1, rv2 = wm[JJ1], wm[JJ2]
assert rv1.session_id != rv2.session_id, "the two strengthening events come from DISTINCT sessions"
synth = SynthFake()
tk1, _ = synthesize_new(rv1, synth, [], known_concept_ids=set(), model="fake", run_id="r")
assert tk1["support"]["sessions"] == 1 < dream.MATURITY_SESSIONS, "one session → incubating"

# a 2nd-session event bumps sessions 1→2 (maturing it); drift_threshold=1.0 forces the CHEAP path (drift
# in [0,1] can never exceed 1.0), so NO LLM call, cost 0.
synth.calls = 0
nv, c = update_support(tk1, rv2, synth, [], known_concept_ids=set(), drift_threshold=1.0, model="fake", run_id="r")
assert nv["support"] == {"events": 2, "sessions": 2}, "a distinct-session event bumps both events and sessions"
assert nv["support"]["sessions"] >= dream.MATURITY_SESSIONS, "now mature (corroborated across distinct sessions)"
assert {e["event_id"] for e in nv["evidence"]} == {rv1.id, rv2.id}, "the new event's evidence is appended"
assert synth.calls == 0 and c == 0.0, "the cheap bump path: no LLM call, no cost"

# a SAME-event re-strengthen is a byte-identical no-op: sessions never inflate (the closed BIRCH op).
nv2, c2 = update_support(nv, rv2, synth, [], known_concept_ids=set(), drift_threshold=1.0, model="fake", run_id="r")
assert nv2["support"] == {"events": 2, "sessions": 2}, "re-strengthening an already-cited event does NOT inflate"
assert c2 == 0.0 and blobstore.canonical_json(takeaway_content(nv2)) == blobstore.canonical_json(takeaway_content(nv)), \
    "the no-op re-version is byte-identical (a TimeMap no-op, no churn)"

# the DRIFT GATE: a low threshold forces a re-synthesis (one Sonnet call, cost > 0) of the why+relation.
synth.calls = 0
nv3, c3 = update_support(tk1, rv2, synth, [], known_concept_ids=set(), drift_threshold=-1.0, model="fake", run_id="r")
assert synth.calls == 1 and c3 > 0, "drift above the threshold re-synthesizes the why (one call, cost > 0)"
assert 0.0 <= dream._drift(rv2, tk1) <= 1.0, "drift is a bounded lexical distance"
print("OK §3 — update_support: distinct-session bump matures a takeaway; a re-strengthen is a byte-identical")
print("        no-op (no inflation); the drift gate is cheap by default and re-synthesizes only when far.")


# === 4. ROUTE: cost accounted, the list-number mapped to an id, coercion applied ======================

R4 = use_store("route")
seed_events([("rt-s1", JJ1, M_MID, 0.85)], R4)
rv = working_set(R4)[0]
cat_one = [{"id": "t-x", "title": "always commit with jj", "why": "sulin uses jj"}]
rr, rcost = route(rv, [], EchoRoute("noop", cost=0.0009))
assert rr == {"decision": "noop", "takeaway_id": None} and rcost == 0.0009, "route returns the verdict + its cost"
assert route(rv, cat_one, EchoRoute("strengthen", "t-x"))[0] == {"decision": "strengthen", "takeaway_id": "t-x"}, \
    "a strengthen of a real catalog id passes through"
assert route(rv, cat_one, EchoRoute("strengthen", "t-ghost"))[0]["decision"] == "new", \
    "a strengthen of an unknown id is coerced to new"
assert route(rv, cat_one, EchoRoute("strengthen", 1))[0] == {"decision": "strengthen", "takeaway_id": "t-x"}, \
    "the router may answer with the list NUMBER — it is mapped back to the id"
assert route(rv, cat_one, EchoRoute(raw="not json at all"))[0] == {"decision": "noop", "takeaway_id": None}, \
    "malformed router JSON → noop (no write)"
print("OK §4 — route: cost accounted; list-number → id; unknown-id strengthen → new; malformed → noop.")


# === 5. APPLY: new/strengthen/noop, takeaway-first/consolidated-last, synth-drop marks consolidated ===

R5 = use_store("apply")
seed_events([("ap-noop", NIX, M_MID, 0.85), ("ap-new", JJ1, M_MID, 0.85),
             ("ap-str", JJ2, M_MID, 0.85), ("ap-drop", "use ripgrep not grep for speed in big repos", M_MID, 0.85)], R5)
am = by_quote(R5)
synth = SynthFake()
KW = dict(known_concept_ids=set(), drift_threshold=0.5, model="fake", run_id="r", root=R5)

# noop → no write, event stays un-consolidated.
rv_noop = am[NIX]
assert apply(rv_noop, {"decision": "noop", "takeaway_id": None}, {}, synth, [], **KW) == (0, 0.0, None)
assert not list(blobstore.decisions_for(rv_noop.id, R5, verb="consolidated")), "noop writes no consolidated decision"
assert rv_noop.id in {e.id for e in working_set(R5)}, "a noop event stays in the working set (forget-eligible)"

# new → takeaway blob FIRST, then the consolidated decision; the event leaves the working set.
rv_new = am[JJ1]
n, c, tkA = apply(rv_new, {"decision": "new", "takeaway_id": None}, {}, synth, [], **KW)
assert n == 1 and tkA is not None, "new returns one output + the takeaway"
assert tkA["id"] in blobstore.latest_by_kind("takeaway", R5), "the takeaway blob is committed"
consA = blobstore.latest_decision(rv_new.id, R5)
assert consA["verb"] == "consolidated" and consA["takeaway"] == tkA["id"] and consA["decision"] == "new", \
    "a consolidated decision (decision='new') targets the event and names the takeaway"
assert rv_new.id not in {e.id for e in working_set(R5)}, "the incorporated event leaves the working set"

# strengthen → a new version of the SAME id; support across distinct sessions; consolidated(strengthen).
rv_str = am[JJ2]
n2, c2, tkB = apply(rv_str, {"decision": "strengthen", "takeaway_id": tkA["id"]},
                    {tkA["id"]: takeaway_content(tkA)}, synth, [], **KW)
assert n2 == 1 and tkB["id"] == tkA["id"], "strengthen versions the same takeaway id (no membership churn)"
assert tkB["support"]["events"] == 2 and tkB["support"]["sessions"] == 2, "support counts the distinct second session"
assert blobstore.latest_decision(rv_str.id, R5)["decision"] == "strengthen", "the event is consolidated (decision='strengthen')"

# new whose synth DROPS it → no takeaway, but a consolidated decision (takeaway=None) so it is not re-synthesized.
rv_drop = am["use ripgrep not grep for speed in big repos"]
nd, cd, tkD = apply(rv_drop, {"decision": "new", "takeaway_id": None}, {}, SynthFake(drop=True), [], **KW)
assert (nd, tkD) == (0, None) and cd > 0, "a synth-drop returns no takeaway"
consD = blobstore.latest_decision(rv_drop.id, R5)
assert consD["verb"] == "consolidated" and consD["takeaway"] is None, "a synth-drop still consolidates (not re-synthesized)"
assert rv_drop.id not in {e.id for e in working_set(R5)}, "the dropped event leaves the working set"
print("OK §5 — apply: noop is inert; new/strengthen commit takeaway-first then consolidated-last; a")
print("        strengthen versions the same id across distinct sessions; a synth-drop consolidates as noise.")


# === 6. THE BLOCK: per-event commit, ON-INSTANCE catalog updates within a run, priority, resume =======

R6 = use_store("block")
seed_events([("b-jj1", JJ1, M_MID, 0.85), ("b-jj2", JJ2, M_LO, 0.85), ("b-nix", NIX, M_HI, 0.85)], R6)
b_evid = {rv.quote: rv.id for rv in working_set(R6)}

# (a) structural conformance + the v2 flags.
blk = dream.DreamBlock(RouteFake(), SynthFake(), route_model="fake", synth_model="fake", forget=False)
assert isinstance(blk, block.Block), "DreamBlock structurally satisfies the Block protocol"
assert blk.name == "dream" and blk.commits_per_item is True, "dream v2 commits PER ITEM (the inversion of v1)"
assert blk.params == (("prompt_version", dream.PROMPT_VERSION), ("model", "fake")), "params = (prompt_version, synth_model)"

# (b) per-EVENT commit + the ON-INSTANCE catalog: drive process() manually in priority order. The catalog
#     starts EMPTY (nothing in the store), so the only way the 2nd JJ event can STRENGTHEN the takeaway the
#     1st JJ event minted is the in-memory fold — proving the catalog updates WITHIN the run.
items = blk.items(R6)
items = sorted(items, key=blk.priority, reverse=True)          # the driver's sort
assert items[0].quote == NIX, "the priority queue serves the highest-salience (NIX) event first"
assert blk._cat == [], "the on-instance catalog is empty at run start (reconciled from the store)"
n0, _ = blk.process(items[0], root=R6, run_id="probe")         # NIX → new
assert n0 == 1 and len(blobstore.latest_by_kind("takeaway", R6)) == 1, "process commits the takeaway blob immediately"
assert blobstore.latest_decision(items[0].id, R6)["verb"] == "consolidated", "and the consolidated decision (per-event)"
assert not list(blobstore.decisions_for(None, R6, verb="processed", stage="dream")), \
    "process writes NO processed marker — the driver does that LAST"
for it in items[1:]:                                           # JJ1 → new, JJ2 → strengthen JJ1's takeaway
    blk.process(it, root=R6, run_id="probe")
latest_tk = {h: json.loads(blobstore.get(h, R6)) for h in blobstore.latest_by_kind("takeaway", R6).values()}
jj_tk = [t for t in latest_tk.values() if t["support"]["events"] == 2]
assert len(blobstore.latest_by_kind("takeaway", R6)) == 2 and jj_tk, \
    "exactly 2 takeaways: the 2nd JJ event STRENGTHENED the 1st's (the on-instance catalog updated within the run)"
assert jj_tk[0]["support"]["sessions"] == 2, "support counts the two distinct JJ sessions"

# (c) maturity gate over the produced takeaways: the 2-session JJ takeaway is review-ready; NIX (1 session)
#     stays incubating but live (routable).
assert {t["id"] for t in current_takeaways(R6)} == {jj_tk[0]["id"]}, "only the mature JJ takeaway reaches review"
assert len(catalog(R6)) == 2, "both takeaways stay LIVE in the routing catalog (incubating included)"

# (d) idempotent re-run: every event is consolidated → working_set empty → zero LLM work via the driver.
R6b = use_store("block-rerun")
seed_events([("br-jj1", JJ1, M_MID, 0.85), ("br-jj2", JJ2, M_LO, 0.85), ("br-nix", NIX, M_HI, 0.85)], R6b)
route1, synth1 = RouteFake(), SynthFake()
rep = dream.run(route1, synth1, route_model="fake", synth_model="fake", forget=False, root=R6b)
assert rep.n_events == 3 and (rep.n_new + rep.n_strengthened) == 3 and rep.processed == 3, \
    f"a clean first run consolidates all three: {rep.n_new} new, {rep.n_strengthened} strengthened"
assert working_set(R6b) == [], "every event is consolidated → the working set is empty"
route2, synth2 = RouteFake(), SynthFake()
rep2 = dream.run(route2, synth2, route_model="fake", synth_model="fake", forget=False, root=R6b)
assert route2.calls == 0 and synth2.calls == 0 and rep2.n_events == 0, \
    "a re-run does ZERO LLM work — consolidated events never re-enumerate"

# (e) a NOOP event is parked by its processed marker (stays in the working set, skipped on a same-param re-run).
R6c = use_store("block-noop")
seed_events([("no-s1", NIX, M_MID, 0.85)], R6c)
rep_noop = dream.run(EchoRoute("noop"), SynthFake(), route_model="fake", synth_model="fake", forget=False, root=R6c)
assert rep_noop.n_noop == 1 and rep_noop.n_new == 0, "the event routed noop — no takeaway"
assert len(working_set(R6c)) == 1, "a noop event STAYS in the working set (un-consolidated)"
park = EchoRoute("noop")
rep_noop2 = dream.run(park, SynthFake(), route_model="fake", synth_model="fake", forget=False, root=R6c)
assert park.calls == 0 and rep_noop2.skipped == 1, "but its 'processed' marker parks it: a same-param re-run skips it"

# (f) error isolation + RESUME from a simulated crash: the router raises on its 2nd call. The 1st + 3rd
#     events commit; the 2nd is isolated (errored, NO consolidated decision); a fresh re-run finishes it.
R6d = use_store("block-crash")
seed_events([("cr-jj1", JJ1, M_MID, 0.85), ("cr-jj2", JJ2, M_LO, 0.85), ("cr-nix", NIX, M_HI, 0.85)], R6d)

class BoomAfter:
    def __init__(self, inner, boom_call):
        self.inner, self.boom_call, self.calls = inner, boom_call, 0
    def __call__(self, system, user):
        self.calls += 1
        if self.calls == self.boom_call:
            raise ValueError("simulated crash in the router")
        return self.inner(system, user)

crash_route = BoomAfter(RouteFake(), boom_call=2)
rep_crash = dream.run(crash_route, SynthFake(), route_model="fake", synth_model="fake", forget=False, root=R6d)
assert rep_crash.errored == 1 and rep_crash.processed == 2, "the crashing event is isolated; the run continues"
assert len(working_set(R6d)) == 1, "the un-committed event is still in the working set (no consolidated decision)"
rep_resume = dream.run(RouteFake(), SynthFake(), route_model="fake", synth_model="fake", forget=False, root=R6d)
assert rep_resume.n_events == 1, "resume re-derives the working set from the store and processes the remainder"
assert working_set(R6d) == [], "after resume every event is consolidated — none lost"

# (g) budget stop: max_usd=0.0 stops before the first event → nothing committed (no orphans).
R6e = use_store("block-budget")
seed_events([("bg-s1", JJ1, M_MID, 0.85)], R6e)
bgt_route, bgt_synth = RouteFake(), SynthFake()
rep_bgt = dream.run(bgt_route, bgt_synth, route_model="fake", synth_model="fake", max_usd=0.0, forget=False, root=R6e)
assert rep_bgt.stopped_on_budget and rep_bgt.processed == 0 and bgt_synth.calls == 0, "a budget stop commits nothing"
assert blobstore.latest_by_kind("takeaway", R6e) == {}, "no takeaway orphaned by the budget stop"
print("OK §6 — the Block: per-event commit (takeaway + consolidated in process), the ON-INSTANCE catalog")
print("        updates within a run (strengthen targets the just-minted), priority-first, maturity gate,")
print("        zero-work re-run, noop parked, error-isolate + resume, budget stop commits nothing.")


# === 7. TRUST CHAIN: takeaway evidence re-resolves to verbatim bytes; malformed event spans dropped ===

R7 = use_store("trust")
seed_events([("tr-s1", JJ1, M_MID, 0.85), ("tr-s2", JJ2, M_MID, 0.85)], R7)
rep7 = dream.run(RouteFake(), SynthFake(), route_model="fake", synth_model="fake", forget=False, root=R7)
for t in current_takeaways(R7) or catalog(R7):
    for ev in t["evidence"]:
        data = blobstore.get(ev["cleaned_hash"], R7).encode("utf-8")
        span = blobstore.validate_span(data, ev["byte_start"], ev["byte_end"])
        assert span is not None, "every stored evidence span RE-VALIDATES at the read boundary"
        assert data[span[0]:span[1]].decode() == ev["quote"], "and resolves to the stored verbatim quote"

# a malformed event span (null / overshoot / inverted) is dropped at dream's READ boundary — never
# resolved to the whole blob or to silently-clamped bytes.
real = working_set(R7)
R7b = use_store("trust-malformed")
seed_events([("tm-s1", JJ1, M_MID, 0.85)], R7b)
good = working_set(R7b)[0]
ch = good.event["cleaned_hash"]
blen = len(blobstore.get(ch, R7b).encode("utf-8"))
for eid, sp in [("bad-null", {"byte_start": None, "byte_end": None}),
                ("bad-over", {"byte_start": 0, "byte_end": blen + 999}),
                ("bad-inv", {"byte_start": 50, "byte_end": 10})]:
    rec_ = {"id": eid, "cleaned_hash": ch, "evidence": [sp], "confidence": 0.9, "markers": M_MID}
    blobstore.ingest(blobstore.canonical_json(rec_), source_kind="event", source_id=eid,
                     origin_ref={"stage": "glean", "model": "malformed"}, root=R7b)
assert {"bad-null", "bad-over", "bad-inv"} <= set(blobstore.latest_by_kind("event", R7b)), "malformed events committed"
assert {rv.id for rv in working_set(R7b)}.isdisjoint({"bad-null", "bad-over", "bad-inv"}), \
    "malformed spans are rejected at the read boundary (no whole-blob or clamped 'verified' bytes)"
print("OK §7 — trust chain: takeaway evidence re-validates + resolves to verbatim bytes; malformed event")
print("        spans dropped at the read boundary (never the whole blob, never clamped).")


# === 8. FORGET: conservative straggler eviction — stale only on (tau cycles AND low salience) =========

R8 = use_store("forget")
# a LOW-salience event (no markers, low confidence → salience ≈ conf*EPS) and a HIGH-salience one.
seed_events([("fg-low", "an incidental throwaway line nobody will need again later", {}, 0.1),
             ("fg-high", JJ1, M_HI, 0.9)], R8)
low = [rv for rv in working_set(R8) if rv.quote.startswith("an incidental")][0]
high = [rv for rv in working_set(R8) if rv.quote == JJ1][0]
assert salience(low.event) < dream.FORGET_SALIENCE_FLOOR < salience(high.event), \
    f"the throwaway is below the floor, JJ1 is well above: {salience(low.event):.4f} / {salience(high.event):.4f}"
# simulate >= tau dream cycles AFTER the events were born: distinct dream-stage decisions with later 'at'.
for rid in ("fc-1", "fc-2", "fc-3"):
    block.write_processed("dream", "dummy-evt", (("prompt_version", dream.PROMPT_VERSION), ("model", "fake")),
                          n_outputs=0, cost_usd=0.0, run_id=rid, extra={}, root=R8)
staled = dream.forget(R8, tau=2, salience_floor=dream.FORGET_SALIENCE_FLOOR, run_id="forget-run")
assert staled == [low.id], f"only the aged AND low-salience event is staled (the conjunction): {staled}"
assert {rv.id for rv in working_set(R8)} == {high.id}, "the staled straggler leaves the working set; the salient one survives"
assert blobstore.has(blobstore.latest_version(low.id, R8), R8), "stale is a DECISION, not a deletion — the blob remains (reversible)"
# the conjunction protects against age-alone: even at tau cycles, the high-salience event is never staled.
assert dream.forget(R8, tau=2, salience_floor=dream.FORGET_SALIENCE_FLOOR, run_id="forget-run-2") == [], \
    "a salient event is never aged out (never age alone — ADR-0010 §6)"
print("OK §8 — forget: stales ONLY on (tau cycles AND low salience); reversible (a decision, not a deletion);")
print("        a salient event is never aged out.")


# === 8b. FORGET-DECOUPLING: relevance scales salience-ORDER, never the forget gate (4b/ADR-0019) ======
# The bug the `_intrinsic_salience` split fixes: `salience` now folds in × W_REL, so a `known` (×0.4) verdict
# could flip an aged, modest-marker event from surviving to forget-eligible — a NEW drop path violating
# recall-first. The fix gates `forget` on `_intrinsic_salience` (the relevance-FREE conf×marker-mass), so
# relevance ORDERS the queue but NEVER evicts. This pins that invariant.
R8b = use_store("forget-relevance")
# A `known` event tuned so its INTRINSIC salience clears the floor while ×0.4 sinks `salience` BELOW it —
# the exact band where the old coupled gate would have wrongly evicted it (intrinsic ≈ 0.85 × 0.11 ≈ 0.094;
# × 0.4 ≈ 0.037 < the 0.05 floor).
cs_known = make_session("fr-known", JJ1)
glean.run([cs_known], GleanFake([JJ1], markers={JJ1: {"surprise": 0.1}}, confidence=0.85, relevance="known"),
          model="fake", root=R8b)
# A genuinely low-intrinsic event (no markers, low confidence) — forget SHOULD still evict this one.
cs_low = make_session("fr-low", NIX)
glean.run([cs_low], GleanFake([NIX], markers={NIX: {}}, confidence=0.1), model="fake", root=R8b)

known_rv = [rv for rv in working_set(R8b) if rv.quote == JJ1][0]
low_rv = [rv for rv in working_set(R8b) if rv.quote == NIX][0]
assert known_rv.event["relevance"] == "known", "the known event stored its verdict (the ×0.4 tier)"
# the decoupling invariant in numbers: INTRINSIC clears the floor, but `salience` (×0.4) sinks BELOW it —
# so the OLD `salience`-gated forget would have evicted this event; the intrinsic-gated forget must NOT.
assert dream._intrinsic_salience(known_rv.event) > dream.FORGET_SALIENCE_FLOOR > salience(known_rv.event), \
    (f"known event: intrinsic clears the floor, ×0.4 salience sinks below it — "
     f"{dream._intrinsic_salience(known_rv.event):.4f} / {salience(known_rv.event):.4f}")
assert dream._intrinsic_salience(low_rv.event) < dream.FORGET_SALIENCE_FLOOR, "the low event is genuinely sub-floor"
# age BOTH events past tau cycles (distinct dream-stage decisions with a later 'at').
for rid in ("fr-1", "fr-2", "fr-3"):
    block.write_processed("dream", "dummy-evt", (("prompt_version", dream.PROMPT_VERSION), ("model", "fake")),
                          n_outputs=0, cost_usd=0.0, run_id=rid, extra={}, root=R8b)
staled = dream.forget(R8b, tau=2, salience_floor=dream.FORGET_SALIENCE_FLOOR, run_id="forget-rel-run")
assert staled == [low_rv.id], \
    f"forget gates on INTRINSIC salience: the ×0.4 `known` event survives, only the genuinely-low one stales: {staled}"
assert known_rv.id in {rv.id for rv in working_set(R8b)}, "relevance ORDERS but never EVICTS — the known event survives"
print("OK §8b — forget-decoupling: relevance scales salience-ORDER only; forget gates on _intrinsic_salience,")
print("        so a ×0.4 `known` verdict re-orders an event but never evicts it (relevance orders, never drops).")


# === 9. MERGE: maintenance de-dup — winner unions the loser's evidence/sessions; loser leaves catalog =

R9 = use_store("merge")
seed_takeaway_v2(id="t-win", title="use jj for version control not git",
                 why="sulin's vcs is jj; reach for jj over git for commits", cites=["e1", "e2"],
                 sessions_seen=["s1", "s2"], evidence=[
                     {"event_id": "e1", "cleaned_hash": "h1", "byte_start": 0, "byte_end": 3, "quote": "jj1", "context": "jj1"},
                     {"event_id": "e2", "cleaned_hash": "h2", "byte_start": 0, "byte_end": 3, "quote": "jj2", "context": "jj2"}],
                 markers={"surprise": 0.2, "insight": 0.6, "research": 0.0}, root=R9)
seed_takeaway_v2(id="t-lose", title="always commit with jj never git",
                 why="commit using jj rather than git for version control", cites=["e3"],
                 sessions_seen=["s3"], evidence=[
                     {"event_id": "e3", "cleaned_hash": "h3", "byte_start": 0, "byte_end": 3, "quote": "jj3", "context": "jj3"}],
                 markers={"surprise": 0.5, "insight": 0.1, "research": 0.0}, root=R9)
seed_takeaway_v2(id="t-other", title="run python through nix develop",
                 why="python3 is not on the path; use nix develop", cites=["e9"], sessions_seen=["s9"],
                 evidence=[], root=R9)
pairs = dream.merge_candidates(catalog(R9), threshold=0.2)
assert any({a, b} == {"t-win", "t-lose"} for a, b in pairs), f"the two near-dup jj takeaways surface as a pair: {pairs}"
assert all("t-other" not in (a, b) for a, b in pairs), "the disjoint nix takeaway is not a merge candidate"
loser, winner = next((a, b) for a, b in pairs if {a, b} == {"t-win", "t-lose"})
assert winner == "t-win", "the higher-support takeaway is the winner"
merged = dream.merge(loser, winner, None, model="fake", run_id="r-merge", root=R9, resynth=False)
assert {e["event_id"] for e in merged["evidence"]} == {"e1", "e2", "e3"}, "the winner UNIONS the loser's evidence (dedup by event_id)"
assert merged["support"] == {"events": 3, "sessions": 3}, "support recomputed over the union of distinct cites + sessions"
assert merged["markers"]["surprise"] == 0.5, "markers max-merge over both"
cat_after = {t["id"] for t in catalog(R9)}
assert loser not in cat_after and winner in cat_after, "the loser leaves the catalog (a merge decision); the winner stays"
assert loser not in {t["id"] for t in current_takeaways(R9)}, "and leaves current_takeaways"
assert blobstore.has(blobstore.latest_version(loser, R9), R9), "the loser's blob + history are RETAINED (no deletion)"

# resynth=True: the OPTIONAL one-Sonnet-call path re-writes the merged why/title and folds last_seen (max).
R9b = use_store("merge-resynth")
seed_takeaway_v2(id="t-win2", title="prefer jj over git", why="sulin's vcs is jj, not git",
                 cites=["e1"], sessions_seen=["s1"], evidence=[], last_seen="2024-01-01T00:00:00+00:00", root=R9b)
seed_takeaway_v2(id="t-lose2", title="commit with jj never git", why="use jj for commits not git",
                 cites=["e2"], sessions_seen=["s2"], evidence=[], last_seen="2025-12-31T00:00:00+00:00", root=R9b)
msynth = SynthFake(why="reach for jj over git — sulin's version control is jj", cost=0.02)
merged_r = dream.merge("t-lose2", "t-win2", msynth, model="fake", run_id="r-merge2", root=R9b, resynth=True)
assert msynth.calls == 1, "resynth=True spends ONE synth call to re-write the merged why"
assert merged_r["why"] == msynth.why, "the re-synthesized why replaces the winner's"
assert merged_r["last_seen"] == "2025-12-31T00:00:00+00:00", "last_seen folds to the MAX over both takeaways"
assert merged_r["producer"]["cost_usd"] > 0, "the resynth cost is accounted on the merged version"
assert "t-lose2" not in {t["id"] for t in catalog(R9b)}, "the loser still leaves the catalog on a resynth merge"

print("OK §9 — merge: near-dups surface as a candidate pair; merge unions evidence/sessions/markers onto")
print("        the winner and drops the loser from catalog/current_takeaways while its blob remains;")
print("        resynth=True spends one synth call to re-write the merged why and folds last_seen (max).")


# === 10. CONCEPT SEAM + belief-change relation coercion ==============================================

R10 = use_store("concepts")
assert dream.load_concepts(R10) == [], "no concepts yet → empty (the seam is wired; review fills it)"
blobstore.ingest(blobstore.canonical_json({"id": "c1", "title": "Use jj", "statement": "Use jj, never git."}),
                 source_kind="concept", source_id="c1", origin_ref={"stage": "review"}, root=R10)
assert [c["id"] for c in dream.load_concepts(R10)] == ["c1"], "an ingested concept blob is read once present"
assert dream._clean_relation({"kind": "contradicts", "concept_id": "c1"}, {"c1"})["kind"] == "contradicts", \
    "a relation to a KNOWN concept sticks"
assert dream._clean_relation({"kind": "contradicts", "concept_id": "ghost"}, {"c1"})["kind"] == "new", \
    "a relation to a non-existent concept can't be trusted → new"
assert dream._clean_relation({"kind": "bogus"}, {"c1"})["kind"] == "new", "an unknown relation kind → new"
blobstore.ingest(blobstore.canonical_json({"verb": "retire", "target": "c1", "at": config.now()}),
                 source_kind="decision", source_id="dec-retire-c1", prev=None, origin_ref={"stage": "review"}, root=R10)
assert dream.load_concepts(R10) == [], "a retired concept leaves the valid set"
# a non-string concept id is skipped (never fatal) — it would otherwise break the known-id set build.
blobstore.ingest(blobstore.canonical_json({"id": ["oops"], "title": "x"}),
                 source_kind="concept", source_id="badconcept", origin_ref={"stage": "review"}, root=R10)
assert all(c["id"] != ["oops"] for c in dream.load_concepts(R10)), "a non-string concept id is skipped — never fatal"
print("OK §10 — concept seam reads VALID concept blobs (retire drops them); belief-change relation coerced.")


# === 11. live smoke (opt-in): real claude CLI route + synth over real events =========================

if os.environ.get("RATCHET_LIVE_TEST") == "1":
    RL = use_store("live")
    seed_events([("live-s1", JJ1, M_MID, 0.85), ("live-s2", JJ2, M_MID, 0.85)], RL)
    rep = dream.run(completer.make_cli_completer("haiku"), completer.make_cli_completer("sonnet"),
                    route_model="haiku", synth_model="sonnet", max_usd=0.50, root=RL)
    for t in catalog(RL):
        for ev in t["evidence"]:
            data = blobstore.get(ev["cleaned_hash"], RL).encode("utf-8")
            assert data[ev["byte_start"]:ev["byte_end"]], "every live takeaway's evidence resolves"
    print(f"OK — live: {rep.n_new} new, {rep.n_strengthened} strengthened, ${rep.cost_usd:.4f}")
else:
    print("SKIP live smoke — set RATCHET_LIVE_TEST=1 to run the real claude CLI")

print("\nall dream tests passed.")
