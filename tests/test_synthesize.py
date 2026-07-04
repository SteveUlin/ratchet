"""synthesize (dream v3 S6) tests: the deferred prose stage, exercised OFFLINE with a FAKE Sonnet
Completer (no network, no API key). The §7.3 properties under test:

  COLD START — a fresh 1-session claim on a cold corpus sits below the bar → ZERO items, ZERO Sonnet
      calls. This is THE property that kills Sonnet-per-event (and the rejected "near-mature" trigger,
      which re-created it: at bar 1.5 every fresh seed is within one session of the bar).
  MATURED FILL — a matured why-null claim enumerates; ONE call fills why (title improved, relation
      coerced, kind PROPOSED — behavioral vs reference, unknown coerced behavioral, ADR-0029); edges
      are NEVER touched and support/cites stay byte-identical — prose is the one stored non-derived
      field.
  FINGERPRINT — the minted version stamps the live corroborates-edge-set fingerprint it consumed;
      retract an edge afterwards → the fold flags why_stale (fused prose must not latch silently).
  IDEMPOTENCY — the done-marker keys on (claim_id, prompt_version, model): a drop verdict marks done
      and a re-tick skips at $0; decay/re-cross never re-pays. Re-synthesis is --claim ONLY (bar AND
      marker bypassed via the per-run demand param) — and it re-stamps the fingerprint, clearing a
      stale flag.
  BUDGET — --max-usd stops the tick cleanly mid-queue: committed-so-far persists, the unpaid tail
      carries no marker and drains next tick.

Run: `python tests/test_synthesize.py`."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-synthesize-")

from ratchet import blobstore, block, chunk, config, dream, glean, resolve, sig, synthesize  # noqa: E402
from ratchet.completer import Completion  # noqa: E402
from ratchet.resolve import claim_pool  # noqa: E402

# --- fixtures: the test_resolve vocabulary (summaries verified into the measured bands) -------------
JJ_SEED = "always commit with jj and never use git for version control"
JJ_PARA = "version control goes through jj, so avoid reaching for git commands"
NEP = "numpy nep-50 promotion keeps python scalars weak so float32 arrays stay float32"

M_HI = {"surprise": 0.9, "insight": 0.3}
M_MID = {"surprise": 0.2, "insight": 0.7}


def sim_of(a, b):
    return sig.jaccard(sig.char_shingles(a), sig.char_shingles(b))


assert sig.J_MAYBE <= sim_of(JJ_SEED, JJ_PARA), "the jj pair must reach the residue (same lesson)"
for other in (JJ_SEED, JJ_PARA):
    assert sim_of(other, NEP) < sig.J_MAYBE, "jj and NEP-50 must be distinct lessons at $0"


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r


def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}


def make_session(sid, line, *, repo=None):
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
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid, origin_ref=origin)
    cs, _, _ = chunk.materialize(raw_h, budget=600)
    return cs


class GleanFake:
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
    """Scripted residue verdicts for the resolve stage (builds the claim stores under test)."""
    def __init__(self, verdicts=()):
        self.verdicts, self.calls = list(verdicts), 0

    def __call__(self, system, user):
        v = self.verdicts[self.calls] if self.calls < len(self.verdicts) else "none"
        self.calls += 1
        return Completion(text=json.dumps({"verdict": v}), model="resolve-fake", cost_usd=0.001)


GOOD = {"title": "  jj is the version-control interface  ",
        "why": "Version control here goes through jj; git commands bypass jj's working-copy model "
               "and desync its view of the repo.",
        "kind": "reference",           # the model's PROPOSED typology (ADR-0029) — stored on the version
        "relation": {"kind": "strengthens", "concept_id": "c-nonexistent", "note": "covers the jj rule"},
        "confidence": 0.9}
GOOD2 = {"title": "jj, never git",
         "why": "Every version-control operation must use jj: it is the only tool whose working-copy "
                "model matches this environment.",
         "kind": "mechanism",          # OUTSIDE the vocabulary → coerces behavioral (recall-first)
         "relation": {"kind": "new", "concept_id": None, "note": ""}, "confidence": 0.8}


class SynthFake:
    """Scripted Sonnet payloads (the prose seat), in call order; keeps the prompts it saw so a test
    can assert the matured-claim framing + quotes + digest reached the model."""
    def __init__(self, payloads=(), *, cost=0.01):
        self.payloads, self.cost, self.calls, self.prompts = list(payloads), cost, 0, []

    def __call__(self, system, user):
        self.prompts.append((system, user))
        p = self.payloads[self.calls] if self.calls < len(self.payloads) else GOOD
        self.calls += 1
        return Completion(text=json.dumps(p), model="synth-fake", cost_usd=self.cost)


class SynthNever:
    """The cold-start assertion in Completer form: below the bar, Sonnet must never be reached."""
    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        raise AssertionError("the synth completer must not be called")


class GarbageFake:
    """A response that parses to nothing — must decline (no version), never mint garbage prose."""
    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return Completion(text="definitely not json", model="garbage-fake", cost_usd=0.01)


def use_store(prefix):
    d = tempfile.mkdtemp(prefix=f"ratchet-test-synthesize-{prefix}-")
    os.environ["RATCHET_DATA_DIR"] = d
    return config.ensure_layout()


def seed_events(specs, root):
    for sid, line, summary, markers, conf, repo in specs:
        cs = make_session(sid, line, repo=repo)
        glean.run([cs], GleanFake([line], summaries={line: summary}, markers={line: markers},
                                  confidence=conf), model="fake", root=root)


def edge_snapshot(root):
    """Every claim_edge's latest version + the total edge-meta count — the edges-untouched witness."""
    n = sum(1 for m in blobstore.iter_meta(root) if m.get("source_kind") == resolve.EDGE_KIND)
    return blobstore.latest_by_kind(resolve.EDGE_KIND, root), n


def claim_content(cid, root):
    return json.loads(blobstore.get(blobstore.latest_version(cid, root), root))


# === 0. UNIT: the fingerprint is order-insensitive over the live corroborates set ====================

fp = resolve.corro_fingerprint("c-x", ["e2", "e1"])
assert fp == resolve.corro_fingerprint("c-x", ["e1", "e2", "e1"]), "sorted + deduped → order-free"
assert fp != resolve.corro_fingerprint("c-x", ["e1"]), "a different edge set → a different fingerprint"
assert fp != resolve.corro_fingerprint("c-y", ["e1", "e2"]), "the claim id is part of the edge identity"
print("OK §0 — corro_fingerprint: deterministic over the sorted live edge-id set.")


# === 1. COLD START: a fresh 1-session claim enumerates NOTHING (the anti-Sonnet-per-event property) ==

R1 = use_store("cold")
seed_events([("cs-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha")], R1)
resolve.run(SynthNever(), model="fake", forget=False, root=R1)      # $0 path: mints the claim
cold = claim_pool(R1)
assert len(cold) == 1 and cold[0]["why"] is None and cold[0]["support"]["sessions"] == 1
net1 = dream.net_entrenchment(cold[0], config.now(), root=R1)
assert net1 < dream.MATURITY_WEIGHT, f"a 1-session seed sits below the bar: {net1:.2f}"
blk1 = synthesize.SynthesizeBlock(SynthNever(), model="fake")
assert list(blk1.items(R1)) == [], \
    "THE cold-start property: a fresh 1-session why-null claim on a cold corpus → ZERO items"
fake1 = SynthNever()
rep1 = synthesize.run(fake1, model="fake", root=R1)
assert rep1.n_queue == 0 and rep1.processed == 0 and fake1.calls == 0, \
    "an all-new corpus pays Sonnet NOTHING — the queue is bounded by the graduation rate"
print("OK §1 — cold start: below-bar claims are not enumerated; zero items, zero Sonnet calls.")


# === 2. MATURED FILL: why filled, title improved, relation coerced; edges + support untouched ========

R2 = use_store("fill")
seed_events([("sy-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha"),
             ("sy-s2", JJ_PARA, JJ_PARA, M_MID, 0.85, "beta")], R2)
resolve.run(ResolveFake(["same-as-1"]), model="fake", forget=False, root=R2)
before = claim_pool(R2)[0]
cid = before["id"]
assert before["why"] is None and before["support"] == {"events": 2, "sessions": 2}
assert before["kind"] is None, "no kind before synthesize — nothing has proposed one yet"
edges_before = edge_snapshot(R2)

fake2 = SynthFake([GOOD])
rep2 = synthesize.run(fake2, model="fake", root=R2)
assert rep2.n_queue == 1 and rep2.n_filled == 1 and rep2.n_dropped == 0 and fake2.calls == 1, \
    "the matured why-null claim enumerates and pays exactly ONE call"
after = claim_pool(R2)[0]
assert after["why"] == GOOD["why"].strip(), "why filled with the clipped prose"
assert after["title"] == "jj is the version-control interface", "title improved (stripped, capped)"
assert after["kind"] == "reference", "the proposed kind is stored on the version and folds through"
assert after["relation"] == {"kind": "new", "concept_id": None, "note": "covers the jj rule"}, \
    "an unknown concept_id coerces the relation to new (dream._clean_relation)"
assert after["support"] == before["support"] and after["cites"] == before["cites"] \
    and after["sessions_seen"] == before["sessions_seen"] and after["scope"] == before["scope"], \
    "synthesis mints a claim VERSION only — every edge-derived attribute is unchanged"
assert edge_snapshot(R2) == edges_before, "edges NEVER touched: no new edge, no new edge version"
system2, user2 = fake2.prompts[0]
assert "MATURED CLAIM" in system2 and '"drop"' in system2, "the matured-claim framing + JSON contract"
assert '"kind": "behavioral"|"reference"' in system2, "the typology rides the JSON contract (ADR-0029)"
assert JJ_SEED in user2 and JJ_PARA in user2, "every corroborating verbatim quote reaches the model"
assert "(no concepts yet — treat everything as new)" in user2, "the concept digest rides as the prior"
assert f"provisional title: {JJ_SEED!r}" in user2, "the machine summary is shown as a hint, not truth"
done2 = block.done_index("synthesize", R2)
assert (cid, synthesize.PROMPT_VERSION, "fake") in done2, \
    "the done-marker keys on (claim_id, prompt_version, model)"
print("OK §2 — matured fill: one call, why filled, title improved, relation coerced, kind proposed;")
print("        edges and support byte-identical; quotes + digest in the prompt; marker keyed as specified.")


# === 3. FINGERPRINT: the minted version stamps the live corroborates-edge-set it consumed ============

content2 = claim_content(cid, R2)
assert content2["kind"] == "reference", "the stored claim version carries the proposed kind, like title/why"
want_fp = resolve.corro_fingerprint(cid, after["cites"])
assert content2["why_fingerprint"] == want_fp, "the stored version carries the edge-set fingerprint"
assert after["why_fingerprint"] == want_fp and after["why_stale"] is False, \
    "the fold carries the stamp and reads fresh while the live set matches"
print("OK §3 — the edge-set fingerprint is stamped on the version and folds as why_stale=False.")


# === 4. RETRACTION AFTER SYNTHESIS: the fold flags why_stale (prose latches; the flag detects it) ====

para2 = [e for e in after["cites"] if e != after["seed_event"]][0]
resolve.retract_edge(para2, "corroborates", cid, root=R2, run_id="syn-retract")
stale = claim_pool(R2)[0]
assert stale["support"]["sessions"] == 1, "the retraction folds out of support as always"
assert stale["why"] == GOOD["why"].strip(), "the prose itself latches — why is stored, not derived"
assert stale["why_stale"] is True, \
    "…which is exactly why the fold must flag it: the live edge set diverged from the fingerprint"
print("OK §4 — retract an edge after synthesis → why_stale flips true (the latched-prose detector).")


# === 5. IDEMPOTENCY: a re-tick no-ops; a DROP verdict marks done and never re-pays ===================

fake5 = SynthNever()
rep5 = synthesize.run(fake5, model="fake", root=R2)
assert rep5.n_queue == 0 and fake5.calls == 0, "a why-filled claim never re-enumerates"

R3 = use_store("drop")
seed_events([("dr-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha"),
             ("dr-s2", JJ_PARA, JJ_PARA, M_MID, 0.85, "beta")], R3)
resolve.run(ResolveFake(["same-as-1"]), model="fake", forget=False, root=R3)
cid3 = claim_pool(R3)[0]["id"]
fake5b = SynthFake([{"drop": True}])
rep5b = synthesize.run(fake5b, model="fake", root=R3)
assert rep5b.n_dropped == 1 and rep5b.n_filled == 0 and fake5b.calls == 1
c3 = claim_content(cid3, R3)
assert c3.get("why") is None and "why_fingerprint" not in c3, "a drop mints NO version"
fake5c = SynthNever()
rep5c = synthesize.run(fake5c, model="fake", root=R3)
assert rep5c.n_queue == 1 and rep5c.skipped == 1 and rep5c.processed == 0 and fake5c.calls == 0, \
    "the marker holds: the still-matured why-null claim re-enumerates but re-ticks at $0 — decay or " \
    "re-crossing the bar can never re-pay the call"
print("OK §5 — re-tick no-ops: filled claims leave the queue; a dropped claim's marker blocks re-pay.")


# === 6. --claim: explicit review demand bypasses the bar AND the marker (the ONLY re-synthesis path) ==

try:
    synthesize.run(SynthNever(), model="fake", claim="c-nope", root=R3)
    raise SystemExit("an unknown --claim id must raise")
except ValueError as e:
    assert "c-nope" in str(e)

# (a) bar bypass: the below-bar cold-start claim from §1 synthesizes on demand.
cid1 = cold[0]["id"]
fake6a = SynthFake([GOOD2])
rep6a = synthesize.run(fake6a, model="fake", claim=cid1, root=R1)
assert rep6a.n_filled == 1 and fake6a.calls == 1
assert claim_pool(R1)[0]["why"] == GOOD2["why"].strip(), "--claim works regardless of the bar"
assert claim_pool(R1)[0]["kind"] == "behavioral", \
    "an out-of-vocabulary kind ('mechanism') coerces behavioral — recall-first: wrongly-behavioral is " \
    "caught at review; wrongly-reference would silently vanish from generation"

# (b) marker bypass: R3's claim carries a done-marker (the drop) — the demand param re-pays anyway.
fake6b = SynthFake([GOOD])
rep6b = synthesize.run(fake6b, model="fake", claim=cid3, root=R3)
assert rep6b.n_filled == 1 and rep6b.skipped == 0 and fake6b.calls == 1, \
    "--claim's per-run demand param versions the done-key past the existing marker"
assert claim_pool(R3)[0]["why"] == GOOD["why"].strip()

# (c) re-synthesis re-stamps the fingerprint: R2's stale claim reads fresh again after the demand.
fake6c = SynthFake([GOOD2])
synthesize.run(fake6c, model="fake", claim=cid, root=R2)
fresh = claim_pool(R2)[0]
assert fresh["why"] == GOOD2["why"].strip() and fresh["why_stale"] is False, \
    "review-demand re-synthesis consumes the CURRENT live edge set → the stale flag clears"
print("OK §6 — --claim: unknown id raises; the bar is bypassed; the marker is bypassed (demand param);")
print("        re-synthesis re-stamps the fingerprint and clears why_stale.")


# === 7. BUDGET: --max-usd stops cleanly mid-queue; the unpaid tail is unmarked and drains next tick ==

R4 = use_store("budget")
seed_events([("bg-s1", JJ_SEED, JJ_SEED, M_HI, 0.85, "alpha"),
             ("bg-s2", JJ_PARA, JJ_PARA, M_MID, 0.85, "beta"),
             ("bg-s3", NEP, NEP, M_HI, 0.85, "gamma"),
             ("bg-s4", NEP, NEP, M_MID, 0.85, "delta")], R4)
resolve.run(ResolveFake(["same-as-1", "same-as-1"]), model="fake", forget=False, root=R4)
queue4 = [c for c in claim_pool(R4) if c["why"] is None]
assert len(queue4) == 2, "two matured why-null claims queued"

fake7z = SynthNever()
rep7z = synthesize.run(fake7z, model="fake", max_usd=0.0, root=R4)
assert rep7z.stopped_on_budget and rep7z.processed == 0 and fake7z.calls == 0, \
    "a zero cap stops before the first paid call — cleanly, nothing half-done"

fake7 = SynthFake([GOOD, GOOD2])
rep7 = synthesize.run(fake7, model="fake", max_usd=0.01, root=R4)
assert rep7.stopped_on_budget and rep7.n_filled == 1 and fake7.calls == 1, \
    "the cap lands after one paid call; the tick stops cleanly with one claim committed"
still = [c for c in claim_pool(R4) if c["why"] is None]
assert len(still) == 1, "committed-so-far persists; the tail is untouched"
done7 = block.done_index("synthesize", R4)
assert not any(k[0] == still[0]["id"] for k in done7), "the unpaid tail carries NO marker"
fake7b = SynthFake([GOOD2])
rep7b = synthesize.run(fake7b, model="fake", root=R4)
assert rep7b.n_queue == 1 and rep7b.n_filled == 1, "the next funded tick drains the tail"
assert all(c["why"] is not None for c in claim_pool(R4))
print("OK §7 — budget: a zero cap stops at $0; a mid-queue cap stops cleanly, the unmarked tail")
print("        retries and drains on the next funded tick.")


print("\nall synthesize tests passed.")
