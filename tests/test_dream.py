"""dream tests: the synthesis stage is exercised offline with FAKE Completers (no network, no API
key), so the suite is deterministic. Load-bearing checks: the deterministic clustering groups by
content and is reproducible; synthesis VERIFIES citations (a hallucinated cite is dropped, a cluster
with none yields no takeaway); the trust chain holds (a takeaway's evidence resolves to real bytes);
idempotency skips unchanged clusters; and EVOLUTION works — a re-run supersedes prior takeaways
(grow/split/merge) and the fold resolves "now". A live smoke is gated behind RATCHET_LIVE_TEST=1.

dream is now a `block.Block` (ADR-0009) — the ONE block that commits per RUN, not per item: `items()`
gathers + clusters, `process()` synthesizes a cluster but only RECORDS it (`commits_per_item=False`,
returns (0, cost)), and `finalize()` runs the coverage-conditioned supersession + every takeaway-blob
and `processed`-marker commit. §0 drives `block.run(DreamBlock(...))` directly to pin the new
mechanics — per-run commit, streaming progress per cluster, the uniform-marker shape, error isolation,
the budget stop's commit-so-far — and the rest assert through `dream.run` (the shim over the driver),
so both the substrate path and the preserved observable contract are tested. Run:
`python tests/test_dream.py`."""
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

ROOT = config.ensure_layout()


def rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r

def amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}


# Distinctive durable lines: JJ-theme lines share content (cluster together), the NIX line is disjoint
# (its own cluster). JJ2 is similar to JJ1 (joins the JJ cluster in the evolution test). Multibyte
# filler forces byte≠char offsets so glean's span math is genuinely exercised end to end.
JJ1 = "always commit with jj and never use git for version control"
JJ2 = "remember to commit with jj, never git, for version control here"
NIX = "run python only through nix develop because python3 is not on the path"


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
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid,
                                origin_ref={"session_id": sid})
    cs, _, chunks = chunk.materialize(raw_h, budget=600)
    return cs


class GleanFake:
    """Returns the same candidate quotes for every chunk; glean's trust anchor keeps only the one that
    is a real substring of each chunk — so each session yields exactly its own durable line as an event."""
    def __init__(self, lines):
        self.lines, self.calls = lines, 0

    def __call__(self, system, user):
        self.calls += 1
        cands = [{"quote": ln, "summary": f"machine summary of: {ln[:24]}",
                  "markers": {"insight": 0.7, "surprise": 0.2}, "confidence": 0.85} for ln in self.lines]
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class DreamFake:
    """A realistic synthesis fake: it CITES the observation ids it was actually given (parsed from the
    prompt), so verification is meaningfully tested. Knobs simulate the model's choices."""
    def __init__(self, *, cite="all", drop=False, relation=None, title="A durable theme",
                 why="The underlying principle.", confidence=0.8):
        self.cite, self.drop, self.relation = cite, drop, relation
        self.title, self.why, self.confidence = title, why, confidence
        self.calls, self.seen_ids = 0, []

    def __call__(self, system, user):
        self.calls += 1
        ids = re.findall(r"- id (\w+):", user)
        self.seen_ids.append(ids)
        if self.cite == "all":
            cites = ids
        elif self.cite == "first":
            cites = ids[:1]
        elif self.cite == "fake":
            cites = ["deadbeefdeadbeef"]            # an id never in any cluster → must be rejected
        else:
            cites = []
        obj = {"title": self.title, "why": self.why, "cites": cites,
               "confidence": self.confidence, "drop": self.drop}
        if self.relation is not None:
            obj["relation"] = self.relation
        return Completion(text=json.dumps(obj), model="fake", cost_usd=0.01)


def seed_takeaway(*, id, member_events, supersedes, cites, fetched_at):
    """Ingest a takeaway BLOB directly (source_id = its cluster_signature), bypassing synthesis, to
    drive the fold deterministically. ADR-0007: current_takeaways folds latest_by_kind('takeaway')
    by (fetched_at, content_hash), so a controlled `fetched_at` sets recency (replacing the old
    run_id-glob ordering). `id` == cluster_signature == source_id, so the fold keys on it."""
    rec = {"id": id, "cluster_signature": id, "member_events": member_events,
           "supersedes": supersedes, "cites": cites, "evidence": [], "support": {"events": 0, "sessions": 0}}
    h, _ = blobstore.ingest(blobstore.canonical_json(rec), source_kind="takeaway", source_id=id,
                            origin_ref={"stage": "dream", "model": "seed"}, fetched_at=fetched_at)
    return h


# Seed events: two sessions on the JJ theme (→ 2 distinct sessions), one on NIX.
for sid, line in [("dream-s1", JJ1), ("dream-s2", JJ1), ("dream-s3", NIX)]:
    cs = make_session(sid, line)
    glean.run([cs], GleanFake([JJ1, NIX]), model="fake")


# --- 0. THE BLOCK SUBSTRATE: dream's per-RUN-commit mechanics (ADR-0009) ---------------------
# Driven directly through block.run(DreamBlock(...)) — the new shape — in an ISOLATED data dir so it
# never perturbs the shared corpus §1+ depend on. Pins the four things the finalize/commits_per_item
# refactor MUST get right: (a) structural Block conformance + the commits_per_item=False flag;
# (b) per-RUN commit — process commits NOTHING durable (no takeaway blob, no marker until finalize),
# returns (0, cost); finalize lands every takeaway blob + a uniform block.write_processed marker LAST;
# (c) streaming progress fires once per cluster as it is synthesized; (d) error isolation + the budget
# stop's "commit what was synthesized before the stop" semantic.

def _run_block_substrate_checks():
    import re as _re
    prev_dir = os.environ["RATCHET_DATA_DIR"]
    iso = tempfile.mkdtemp(prefix="ratchet-test-dream-block-")
    os.environ["RATCHET_DATA_DIR"] = iso
    try:
        iroot = config.ensure_layout()
        # build two disjoint clusters (JJ ×2 sessions, NIX ×1) in the isolated store
        for sid, line in [("b-s1", JJ1), ("b-s2", JJ1), ("b-s3", NIX)]:
            glean.run([make_session(sid, line)], GleanFake([JJ1, NIX]), model="fake")

        class _DreamFake:                          # cites every id it is given (real, verifiable)
            def __init__(self): self.calls = 0
            def __call__(self, system, user):
                self.calls += 1
                ids = _re.findall(r"- id (\w+):", user)
                return Completion(text=json.dumps(
                    {"title": "t", "why": "w", "cites": ids, "confidence": 0.8, "drop": False}),
                    model="fake", cost_usd=0.01)

        # (a) structural conformance + the flag
        blk = dream.DreamBlock(_DreamFake(), model="fake")
        assert isinstance(blk, block.Block), "DreamBlock structurally satisfies the Block protocol"
        assert blk.name == "dream" and blk.commits_per_item is False, \
            "dream is the one block that commits per RUN (commits_per_item=False), not per item"
        assert blk.params == (("prompt_version", dream.PROMPT_VERSION), ("model", "fake")), \
            "params are the ordered (prompt_version, model) idempotency suffix"

        # (b) process records but commits NOTHING durable; finalize commits everything
        clusters = list(blk.items(iroot))
        assert len(clusters) == 2 and blk.n_clusters == 2, "items() gathers + clusters into 2 items"
        n_out, cost = blk.process(clusters[0], root=iroot, run_id="rid-probe")
        assert n_out == 0 and cost > 0, "process returns (0, cost): nothing durable yet, cost feeds the gate"
        assert blobstore.latest_by_kind("takeaway", iroot) == {}, \
            "no takeaway blob lands in process (commit waits for finalize)"
        assert not list(blobstore.decisions_for(None, iroot, verb="processed", stage="dream")), \
            "no processed marker is written in process (the all-or-nothing commits_per_item=False rule)"

        # the streaming printer is exercised once per cluster (capture the per-item lines). The probe is
        # a Progress-like object (start/tick/stop) — the decoupled driver speaks only that protocol — and
        # tracks its OWN examined/processed counters since the new tick takes key+outcome, not the Report.
        lines = []
        class _Probe:
            def __init__(self): self.examined = self.processed = 0
            def start(self, *, total, todo, already): pass
            def tick(self, key, outcome, *, outputs=0, cost=0.0):
                self.examined += 1
                if outcome == "done":
                    self.processed += 1
                lines.append((self.examined, self.processed, key))
            def stop(self): pass
        blk2 = dream.DreamBlock(_DreamFake(), model="fake-b")
        rep = block.run(blk2, progress=_Probe(), root=iroot)
        assert rep.stage == "dream" and rep.examined == 2 and rep.processed == 2, \
            "uniform block.Report: 2 clusters examined + processed"
        assert rep.outputs == 0, "Report.outputs is 0 for dream — outputs land in finalize, not per item"
        assert len(lines) == 2 and lines[0][0] == 1 and lines[1][0] == 2, \
            "(c) progress streams once per cluster as it is synthesized (examined ticks 1 then 2)"
        # finalize landed the takeaways + a uniform marker per cluster
        assert len(blk2.takeaways) == 2, "finalize committed both takeaway blobs"
        assert len(blobstore.latest_by_kind("takeaway", iroot)) == 2, "two takeaway sources now exist"
        markers = list(blobstore.decisions_for(None, iroot, verb="processed", stage="dream"))
        assert len(markers) == 2, "finalize wrote one processed marker per cluster, LAST"
        mk = markers[0]
        assert mk["params"] == [["prompt_version", dream.PROMPT_VERSION], ["model", "fake-b"]], \
            "the marker carries the authoritative ordered params list block.done_index keys on"
        assert mk["n_outputs"] == 1 and "event_ids" in mk and "dropped" in mk, \
            "the marker is a uniform block.write_processed body + dream's event_ids/dropped audit (extra)"
        # block.done_index and the dream.processed_index shim agree on the same keys
        assert block.done_index("dream", iroot) == dream.processed_index(iroot), \
            "dream.processed_index is now block.done_index — same (sig, prompt, model) keys"
        # a re-run for the same (clusters, prompt, model) does zero LLM work (done-skip in the driver)
        again = _DreamFake()
        rep2 = block.run(dream.DreamBlock(again, model="fake-b"), progress=None, root=iroot)
        assert again.calls == 0 and rep2.skipped == 2 and rep2.processed == 0, \
            "the done-skip lives in the driver: an unchanged re-run synthesizes nothing"

        # (d) error isolation + budget stop's commit-so-far. A completer that raises on the SECOND call:
        class _DieOnSecond:
            def __init__(self): self.calls = 0
            def __call__(self, system, user):
                self.calls += 1
                if self.calls == 2:
                    raise ValueError("boom on the 2nd cluster")
                ids = _re.findall(r"- id (\w+):", user)
                return Completion(text=json.dumps(
                    {"title": "t", "why": "w", "cites": ids, "confidence": 0.8, "drop": False}),
                    model="fake", cost_usd=0.01)
        die = _DieOnSecond()
        bb = dream.DreamBlock(die, model="fake-die")
        rb = block.run(bb, progress=None, root=iroot)
        assert rb.errored == 1 and rb.processed == 1, "one cluster errored (isolated), the other committed"
        assert len(bb.takeaways) == 1, "finalize committed only the survivor — the errored cluster is absent"
        die_done = {k for k in block.done_index("dream", iroot) if k[2] == "fake-die"}
        assert len(die_done) == 1, "only the survivor got a marker; the errored cluster is retried next run"

        # budget stop: max_usd=0.0 stops before any synthesis → finalize commits nothing (no orphans)
        bgt = dream.DreamBlock(_DreamFake(), model="fake-bgt")
        rg = block.run(bgt, max_usd=0.0, progress=None, root=iroot)
        assert rg.stopped_on_budget and rg.processed == 0 and bgt.takeaways == [], \
            "a budget stop before the first cluster commits nothing (commit-so-far == nothing)"
    finally:
        os.environ["RATCHET_DATA_DIR"] = prev_dir
        config.ensure_layout()


_run_block_substrate_checks()
print("OK — block substrate: dream commits per RUN (process records, finalize commits); streaming")
print("     progress per cluster; uniform marker (params list + event_ids/dropped); error + budget isolation.")


# --- 1. gather: events resolve to their TRUSTED quote; sessions resolve via lineage ----------

events = dream.gather_events(ROOT)
quotes = sorted(e.quote for e in events)
assert sum(e.quote == JJ1 for e in events) == 2, "two sessions carry the JJ line → two events"
assert sum(e.quote == NIX for e in events) == 1, "one session carries the NIX line"
assert all(e.session_id for e in events), "every event resolves its originating session via lineage"
assert {e.session_id for e in events} == {"dream-s1", "dream-s2", "dream-s3"}, "distinct sessions resolved"
print("OK — gather: events resolved to verbatim quotes; sessions recovered through content-addressed lineage.")


# --- 2. cluster: deterministic, content-grouped, reproducible -------------------------------

clusters = dream.cluster(events)
assert len(clusters) == 2, "JJ events cluster together; the disjoint NIX event stands alone"
by_quote = {tuple(sorted({e.quote for e in cl})) for cl in clusters}
assert (JJ1,) in by_quote and (NIX,) in by_quote, "clusters split exactly along content themes"
jj_cluster = [cl for cl in clusters if cl[0].quote == JJ1][0]
assert len(jj_cluster) == 2, "both JJ events land in one cluster"
# reproducible: same events → same partition + same signatures, regardless of input order
again = dream.cluster(list(reversed(events)))
assert {dream.cluster_signature(c) for c in clusters} == {dream.cluster_signature(c) for c in again}, \
    "clustering is order-stable (deterministic signatures)"
# tokenization keeps technical tokens whole
assert "$ratchet_data_dir" in dream._tokens("set $RATCHET_DATA_DIR now") and \
    "--max-turns" in dream._tokens("pass --max-turns 1"), "technical tokens survive tokenization"
print("OK — cluster: deterministic TF-IDF leader clustering splits along content, order-stable.")


# --- 3. synthesize + verify: real cites kept, hallucinated dropped, no-evidence → no takeaway --

rep = dream.run(DreamFake(cite="all"), model="fake")
assert len(rep.takeaways) == 2 and rep.n_clusters == 2, "one takeaway per cluster"
jj_tk = [t for t in rep.takeaways if t["support"]["events"] == 2][0]
assert jj_tk["support"]["sessions"] == 2, "support weighted by DISTINCT sessions (Mem0-style)"
assert set(jj_tk["cites"]) == {e.id for e in jj_cluster}, "cites are the cluster's real event ids"
assert jj_tk["markers"]["insight"] == 0.7 and jj_tk["markers"]["surprise"] == 0.2, "markers aggregate (max) over cited events"
assert jj_tk["relation"]["kind"] == "new", "no concepts yet → every takeaway is new"
assert "status" not in jj_tk, "status is DROPPED (state is a decision now, never an in-record field)"
assert jj_tk["producer"]["stage"] == "dream"   # producer stays on the in-memory/report record (cost amortization)
assert jj_tk["producer"]["cost_usd"] > 0 and jj_tk["id"] == jj_tk["cluster_signature"]

# the trust chain extends: every cited piece of evidence resolves to real bytes in an immutable blob
for ev in jj_tk["evidence"]:
    data = blobstore.get(ev["cleaned_hash"]).encode("utf-8")
    assert data[ev["byte_start"]:ev["byte_end"]].decode() == JJ1, "takeaway evidence resolves to the verbatim quote"

# a hallucinated citation (id not in the cluster) is dropped → no surviving evidence → no takeaway
fakecite = dream.run(DreamFake(cite="fake"), model="fake-hallucinate")
assert fakecite.takeaways == [] and fakecite.dropped == fakecite.n_clusters, \
    "a takeaway citing only non-cluster ids is rejected (no verifiable evidence)"

# an explicit drop (model judged the cluster noise) yields no takeaway but IS marked done (not noise-retried)
drop = dream.run(DreamFake(drop=True), model="fake-drop")
assert drop.takeaways == [] and drop.dropped == drop.n_clusters
assert all((dream.cluster_signature(c), dream.PROMPT_VERSION, "fake-drop") in dream.processed_index()
           for c in dream.cluster(dream.gather_events(ROOT))), "dropped clusters are marked done"
print("OK — synthesize: citations verified (hallucination rejected, no-evidence dropped), trust chain")
print("     resolves to immutable bytes, support counts distinct sessions, drop marked done.")


# --- 4. the takeaway store (BLOBS) + idempotency (processed DECISIONS) -----------------------

# takeaways ARE blobs now (ADR-0007): source_id == cluster_signature, kind=raw/source_kind=takeaway.
latest_tk = blobstore.latest_by_kind("takeaway")
assert {t["cluster_signature"] for t in rep.takeaways} <= set(latest_tk), \
    "each takeaway is a blob version keyed on its cluster_signature (latest_by_kind contains its source)"
loaded = dream.load_takeaways()
assert {t["id"] for t in rep.takeaways} <= {t["id"] for t in loaded}, "takeaways committed and reloadable"
# meta of a committed takeaway blob: kind=raw, source_kind=takeaway, source_id=cluster_signature, prev linkage.
sig0 = rep.takeaways[0]["cluster_signature"]
m_tk = blobstore.get_meta(latest_tk[sig0])
assert m_tk["kind"] == "raw" and m_tk["source_kind"] == "takeaway" and m_tk["source_id"] == sig0, \
    "takeaway blob meta carries raw/takeaway/cluster_signature"
assert m_tk["origin_ref"]["stage"] == "dream" and m_tk["origin_ref"]["model"] == "fake", \
    "producer/provenance is mirrored into origin_ref (answerable from meta alone)"
# the STORED content is the run-invariant projection: NO producer, NO status (those moved to origin_ref / decisions).
stored = json.loads(blobstore.get(latest_tk[sig0]))
assert "producer" not in stored and "status" not in stored, \
    "stored takeaway content drops run-varying producer + status (else a re-synthesis forks a spurious version)"
assert json.loads(blobstore.get(latest_tk[sig0])) == json.loads(
    blobstore.canonical_json(dream.takeaway_content(
        [t for t in rep.takeaways if t["cluster_signature"] == sig0][0]))), \
    "content == canonical_json(takeaway_content(record)) — the byte-stable projection"
# a `processed` DECISION exists per synthesized cluster (verb/target/key); its meta is a decision blob.
done_now = dream.processed_index()
for t in rep.takeaways:
    assert (t["cluster_signature"], dream.PROMPT_VERSION, "fake") in done_now, \
        "a processed decision marks the synthesized cluster done for (cluster_signature, prompt, model)"
# pick THIS run's marker (model="fake") — sig0 may have markers under several model keys by now
# (the drop/hallucinate runs above each marked their clusters done under their own model).
dec = next(d for d in blobstore.decisions_for(sig0, verb="processed", stage="dream")
           if d.get("model") == "fake")
m_dec = blobstore.get_meta(dec["content_hash"])
assert m_dec["source_kind"] == "decision" and m_dec["source_id"] == dec["content_hash"] and m_dec["prev"] is None, \
    "a decision blob: source_id == its own content_hash, never re-versioned (prev=None)"
# the marker is the UNIFORM block.write_processed body now (ADR-0009): the ordered params LIST that
# block.done_index keys on, plus dream's per-cluster audit (event_ids/dropped) carried as `extra`.
assert dec["params"] == [["prompt_version", dream.PROMPT_VERSION], ["model", "fake"]], \
    "the dream marker carries the authoritative ordered params list (the done-key, reorder-proof)"
assert dec["n_outputs"] == 1 and dec["dropped"] is False, \
    "a committed (non-dropped) cluster's marker records 1 output and dropped=False"
assert "event_ids" in dec and dec["event_ids"] == sorted(dec["event_ids"]), \
    "dream's event_ids/dropped per-cluster audit survives in the uniform marker's extra"

before = DreamFake(cite="all")
rerun = dream.run(before, model="fake")
assert before.calls == 0 and rerun.skipped == rerun.n_clusters and not rerun.takeaways, \
    "a re-run for the same (clusters, prompt, model) does zero LLM work (a processed decision exists → skipped)"
def _n_versions(sig):
    return sum(1 for m in blobstore.iter_meta()
               if m.get("source_kind") == "takeaway" and m.get("source_id") == sig)

# a model bump with BYTE-IDENTICAL output is a NO-OP version (the projection drops producer, so only
# the origin_ref.model differs — that lives in meta, not content): the content blob is shared, no churn.
n0 = _n_versions(sig0)
noop = dream.run(DreamFake(cite="all"), model="fake-v2-sametitle")   # same DreamFake defaults → same content
assert len(noop.takeaways) == 2, "the new model key re-synthesizes the clusters (LLM called)"
assert _n_versions(sig0) == n0, \
    "byte-identical re-synthesis no-ops as a version (only origin_ref.model differs → meta, not content)"
# a model bump with CHANGED output (different title) is a NEW prev-linked VERSION of the SAME source:
changed = dream.run(DreamFake(cite="all", title="a sharper title"), model="fake-v3-newtitle")
assert len(changed.takeaways) == 2, "the changed re-synthesis emits takeaways"
assert _n_versions(sig0) == n0 + 1, "changed content forks exactly one new version under the same source"
m_v3 = blobstore.get_meta(blobstore.latest_by_kind("takeaway")[sig0])
assert m_v3["origin_ref"]["model"] == "fake-v3-newtitle" and m_v3["prev"] is not None, \
    "the changed version is a new prev-linked snapshot of the same source (the TimeMap, latest wins)"
assert json.loads(blobstore.get(blobstore.latest_by_kind("takeaway")[sig0]))["title"] == "a sharper title", \
    "current content is the newest version's"
print("OK — takeaway BLOB store (source_id=cluster_signature) + processed-decision idempotency;")
print("     re-run zero-work; identical re-synthesis no-ops, changed = a new prev-linked version.")


# --- 5. EVOLUTION by supersession: grow/split/merge are one append-only mechanism -----------

# (a) GROW: a new session adds a JJ-similar line → it joins the JJ cluster → the cluster's membership
#     changes → a NEW takeaway supersedes the prior JJ takeaway; the unchanged NIX cluster is skipped.
prior_current = {t["id"]: t for t in dream.current_takeaways()}
prior_jj = [t for t in prior_current.values() if t["support"]["events"] == 2]
# (the 2-event JJ takeaways are the baseline current set this grow must supersede; stored content has
#  no `producer`, so the model is read from origin_ref if needed — recency is meta.fetched_at now)
assert prior_jj, "a prior 2-event JJ takeaway is current before the grow"
cs4 = make_session("dream-s4", JJ2)
glean.run([cs4], GleanFake([JJ1, JJ2, NIX]), model="fake")
events2 = dream.gather_events(ROOT)
assert any(e.quote == JJ2 for e in events2), "the new JJ-similar event is gathered"
jj_now = [cl for cl in dream.cluster(events2) if any(e.quote in (JJ1, JJ2) for e in cl)][0]
assert len(jj_now) == 3, "JJ2 joins the JJ cluster (grown to 3) — proves leader clustering absorbs near-dupes"

grow = dream.run(DreamFake(cite="all", title="jj over git"), model="fake")
grown_tk = [t for t in grow.takeaways if t["support"]["events"] == 3]
assert grown_tk, "the grown JJ cluster (new signature) re-synthesizes"
grown_tk = grown_tk[0]
assert grown_tk["supersedes"], "the grown takeaway supersedes the prior JJ takeaway(s) it shares events with"
superseded = set(grown_tk["supersedes"])
current_ids = {t["id"] for t in dream.current_takeaways()}
assert superseded.isdisjoint(current_ids), "superseded takeaways are folded OUT of the current view"
assert grown_tk["id"] in current_ids, "the new grown takeaway is current"
# the NIX cluster was unchanged → not re-synthesized this run
assert all(t["support"]["events"] == 3 for t in grow.takeaways), "only the changed cluster re-synthesized; NIX skipped"
print("OK — evolution/GROW: a changed cluster re-synthesizes and SUPERSEDES its prior; the fold drops")
print("     the superseded takeaway; the unchanged cluster is skipped (no churn).")

# (b) The fold resolves arbitrary supersede links (split = one→many, merge = many→one). The seams are
#     now takeaway BLOBS (distinct source_ids = distinct logical takeaways); current_takeaways folds
#     latest_by_kind('takeaway') + the supersede links — no log, no hand-written shard.
seed_takeaway(id="seedfoldaaaa", member_events=["e1"], supersedes=[], cites=["e1"],
              fetched_at="2000-01-01T00:00:00+00:00")
seed_takeaway(id="seedfoldbbbb", member_events=["e2"], supersedes=[], cites=["e2"],
              fetched_at="2000-01-01T00:00:01+00:00")
seed_takeaway(id="seedfoldcccc", member_events=["e1", "e2"], supersedes=["seedfoldaaaa", "seedfoldbbbb"],
              cites=["e1", "e2"], fetched_at="2000-01-01T00:00:02+00:00")
folded = {t["id"] for t in dream.current_takeaways()}
assert "seedfoldcccc" in folded and "seedfoldaaaa" not in folded and "seedfoldbbbb" not in folded, \
    "merge (many→one): the merged takeaway supersedes both, which fold OUT of the current view"

print("OK — evolution/FOLD: supersede links resolve split (one→many) and merge (many→one) over BLOBS.")

# (c) supersession is COVERAGE-CONDITIONED (the orphan fix): a prior is folded out ONLY if ALL its
#     events are re-covered this run. Seed a prior owning a real event + a ghost event no cluster covers;
#     a re-run that re-synthesizes the real cluster must NOT supersede it (else the ghost's coverage is
#     orphaned). The owner-overlap logic WOULD wrongly fold it out (it shares the real event). The prior
#     is now a takeaway BLOB seeded with an EARLIER fetched_at (the recency knob replacing run_id order);
#     its own source_id, so the fold drops it ONLY via a survivor's supersede link — which coverage gates.
real_id = dream.gather_events(ROOT)[0].id
seed_takeaway(id="guardtk", member_events=sorted([real_id, "ghost-never-covered"]), supersedes=[],
              cites=[real_id], fetched_at="2000-01-01T00:00:00+00:00")
assert "guardtk" in {t["id"] for t in dream.current_takeaways()}, "the seeded prior is current before the run"
cov = dream.run(DreamFake(cite="all"), model="fake-cov")          # fresh key → re-synthesizes the real clusters
assert cov.takeaways and any(real_id in (t.get("member_events") or []) for t in cov.takeaways), \
    "the re-synthesis emits a takeaway that SHARES the guard's real event (overlap exists)"
assert "guardtk" in {t["id"] for t in dream.current_takeaways()}, \
    "a prior with an UNcovered event survives — a dropped/split child never orphans its parent's events"
print("OK — evolution/COVERAGE: supersession is coverage-conditioned (no orphan), recency keys on fetched_at.")


# --- 6. concept seam + belief-change relation -----------------------------------------------

assert dream.load_concepts(ROOT) == [], "no concepts yet → empty (the seam is wired, review fills it)"
blobstore.ingest(blobstore.canonical_json({"id": "c1", "title": "Use jj", "statement": "Use jj, never git."}),
                 source_kind="concept", source_id="c1", origin_ref={"stage": "review"})
assert [c["id"] for c in dream.load_concepts(ROOT)] == ["c1"], "an ingested concept BLOB is read once present (ADR-0007)"
# the relation is coerced against KNOWN ids: a real concept id sticks; an unknown one falls back to new
assert dream._clean_relation({"kind": "contradicts", "concept_id": "c1"}, {"c1"})["kind"] == "contradicts"
assert dream._clean_relation({"kind": "contradicts", "concept_id": "ghost"}, {"c1"})["kind"] == "new", \
    "a relation to a non-existent concept can't be trusted → new"
assert dream._clean_relation({"kind": "bogus"}, {"c1"})["kind"] == "new", "unknown relation kind → new"
# retire c1 (immutable — no file to unlink; a retire decision is how a concept leaves the valid set)
blobstore.ingest(blobstore.canonical_json({"verb": "retire", "target": "c1", "at": config.now()}),
                 source_kind="decision", source_id="dec-retire-c1", prev=None, origin_ref={"stage": "review"})
assert dream.load_concepts(ROOT) == [], "a retired concept leaves the valid set"
print("OK — concept seam reads VALID concept blobs (retire drops them); belief-change relation coerced.")


# --- 7. adversarial hardening: flaky completer isolated, budget stop, non-finite scrubbed ----

class Boom:
    def __init__(self): self.calls = 0
    def __call__(self, system, user):
        self.calls += 1
        raise ValueError("simulated synthesis-binding failure")

boom = Boom()
rep_b = dream.run(boom, model="fake-boom")
assert boom.calls > 0 and rep_b.errored == rep_b.n_clusters and not rep_b.takeaways, "every failing call isolated"
assert not any((dream.cluster_signature(c), dream.PROMPT_VERSION, "fake-boom") in dream.processed_index()
               for c in dream.cluster(dream.gather_events(ROOT))), "a fully-errored cluster is NOT marked done"
recovered = dream.run(DreamFake(cite="all"), model="fake-boom")
assert recovered.takeaways, "the retried clusters recover once the completer works"

stopped = dream.run(DreamFake(cite="all"), model="fake-budget", max_usd=0.0)
assert stopped.stopped_on_budget and not stopped.takeaways, "max_usd stops the run cleanly before spending"

below = dream.run(DreamFake(cite="all"), model="fake-min", min_events=10_000)
assert below.below_threshold and not below.takeaways, "min-events gates dreaming until signal accumulates"

assert dream._score(float("nan")) == 0.0 and dream._score(float("inf")) == 0.0 and dream._score("x", 0.5) == 0.5, \
    "non-finite / unparseable scores fall back, never reach the store"
print("OK — adversarial: flaky completer isolated + cluster retried, budget + min-events gate cleanly,")
print("     non-finite scores scrubbed.")


# --- 8. regression (review-battery fixes): span re-anchoring, concept id hygiene, model-bump fold --

# (a) TRUST CHAIN re-anchored at dream's READ boundary: a glean event whose stored span is malformed
#     (null / overshoot / inverted) against a REAL blob is dropped — never resolved to the whole blob
#     or to silently-clamped bytes and labelled "verified". Events are blobs now (ADR-0007), so the
#     reachability vector is a malformed EVENT BLOB version (a buggy producer or out-of-band write),
#     injected via ingest exactly as a real glean event would be — gather_events re-validates it.
real_ev = [e for e in glean.load_events() if e["evidence"][0].get("byte_start") is not None][0]
ch = real_ev["cleaned_hash"]
blen = len(blobstore.get(ch).encode("utf-8"))
bad = [{"id": "bad-null", "cleaned_hash": ch, "evidence": [{"byte_start": None, "byte_end": None}], "confidence": 0.9},
       {"id": "bad-over", "cleaned_hash": ch, "evidence": [{"byte_start": 0, "byte_end": blen + 999}], "confidence": 0.9},
       {"id": "bad-inv", "cleaned_hash": ch, "evidence": [{"byte_start": 50, "byte_end": 10}], "confidence": 0.9}]
for r in bad:
    blobstore.ingest(blobstore.canonical_json(r), source_kind="event", source_id=r["id"],
                     origin_ref={"stage": "glean", "model": "malformed"})
assert {r["id"] for r in bad} <= set(blobstore.latest_by_kind("event")), "the malformed event blobs are committed (reachable)"
assert {e.id for e in dream.gather_events(ROOT)}.isdisjoint({"bad-null", "bad-over", "bad-inv"}), \
    "malformed spans are rejected at the read boundary (no whole-blob or clamped 'verified' bytes)"

# (b) a concept blob with a non-string id is skipped (load_concepts' 'never fatal' contract) — a non-
#     string id is unhashable and would otherwise make the known-id set build error EVERY cluster.
blobstore.ingest(blobstore.canonical_json({"id": ["oops"], "title": "x"}),
                 source_kind="concept", source_id="badconcept", origin_ref={"stage": "review"})
assert all(c["id"] != ["oops"] for c in dream.load_concepts(ROOT)), "a non-string concept id is skipped — never fatal"
assert dream.run(DreamFake(cite="all"), model="fake-badconcept").errored == 0, "a bad concept blob wedges nothing"

# (c) a model bump re-synthesizes the same grouping and REPLACES it in the current fold (one current
#     takeaway per grouping; the older model's version is not also current). Recency is now the
#     TimeMap's meta.fetched_at (the prev-chain), NOT glob/run_id order. The stored content has no
#     `producer` (it moved to origin_ref), so the visible change is the title (ONE→TWO) plus the latest
#     version's origin_ref.model — both must flip to bumpB.
m1 = dream.run(DreamFake(cite="all", title="ONE"), model="bumpA")
if m1.takeaways:
    tid = m1.takeaways[0]["id"]                                   # == cluster_signature == source_id
    cur = {t["id"]: t for t in dream.current_takeaways()}[tid]
    assert cur["title"] == "ONE", "bumpA's version is current after its run"
    m_a = blobstore.get_meta(blobstore.latest_by_kind("takeaway")[tid])
    assert m_a["origin_ref"]["model"] == "bumpA", "latest version's provenance is bumpA"
    dream.run(DreamFake(cite="all", title="TWO"), model="bumpB")
    cur2 = {t["id"]: t for t in dream.current_takeaways()}[tid]
    assert cur2["title"] == "TWO", \
        "a model bump REPLACES the grouping's current takeaway (the TimeMap latest wins, fetched_at recency)"
    m_b = blobstore.get_meta(blobstore.latest_by_kind("takeaway")[tid])
    assert m_b["origin_ref"]["model"] == "bumpB" and m_b["prev"] is not None, \
        "the bumpB version is a new prev-linked snapshot of the SAME source_id (one current per grouping)"
print("OK — regression: span re-anchored, malformed concept id non-fatal, model bump replaces in the fold.")


# --- 9. live smoke (opt-in): real claude CLI synthesis over real clustered events ------------

if os.environ.get("RATCHET_LIVE_TEST") == "1" and dream.gather_events(ROOT):
    rep = dream.run(completer.make_cli_completer("sonnet"), model="sonnet", max_usd=0.50)
    for t in rep.takeaways:
        for ev in t["evidence"]:
            data = blobstore.get(ev["cleaned_hash"]).encode("utf-8")
            assert data[ev["byte_start"]:ev["byte_end"]], "every live takeaway's evidence resolves"
    print(f"OK — live: {len(rep.takeaways)} takeaways over {rep.n_clusters} clusters, ${rep.cost_usd:.4f}")
else:
    print("SKIP live smoke — set RATCHET_LIVE_TEST=1 to run the real claude CLI")

print("\nall dream tests passed.")
