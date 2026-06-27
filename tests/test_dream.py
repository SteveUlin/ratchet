"""dream tests: the synthesis stage is exercised offline with FAKE Completers (no network, no API
key), so the suite is deterministic. Load-bearing checks: the deterministic clustering groups by
content and is reproducible; synthesis VERIFIES citations (a hallucinated cite is dropped, a cluster
with none yields no takeaway); the trust chain holds (a takeaway's evidence resolves to real bytes);
idempotency skips unchanged clusters; and EVOLUTION works — a re-run supersedes prior takeaways
(grow/split/merge) and the fold resolves "now". A live smoke is gated behind RATCHET_LIVE_TEST=1.
Run: `python tests/test_dream.py`."""
import glob as _glob
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-dream-")

from ratchet import blobstore, chunk, completer, config, dream, glean  # noqa: E402
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


# Seed events: two sessions on the JJ theme (→ 2 distinct sessions), one on NIX.
for sid, line in [("dream-s1", JJ1), ("dream-s2", JJ1), ("dream-s3", NIX)]:
    cs = make_session(sid, line)
    glean.run([cs], GleanFake([JJ1, NIX]), model="fake")


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
assert jj_tk["status"] == "synthesized" and jj_tk["producer"]["stage"] == "dream"
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


# --- 4. the takeaway store + idempotency ----------------------------------------------------

shards = _glob.glob(os.path.join(ROOT, "events", "dream-*.jsonl"))
assert shards and not any(s.endswith(".partial") for s in shards), ".partial renamed to final on clean exit"
loaded = dream.load_takeaways()
assert {t["id"] for t in rep.takeaways} <= {t["id"] for t in loaded}, "takeaways committed and reloadable"

before = DreamFake(cite="all")
rerun = dream.run(before, model="fake")
assert before.calls == 0 and rerun.skipped == rerun.n_clusters and not rerun.takeaways, \
    "a re-run for the same (clusters, prompt, model) does zero LLM work"
# a different model is a different ledger key → re-synthesizes the same clusters
rerun2 = dream.run(DreamFake(cite="all"), model="fake-v2")
assert len(rerun2.takeaways) == 2, "bumping the model re-synthesizes over the same clusters"
print("OK — append-only takeaway store, idempotent re-run (skip done; re-synthesize on a new model key).")


# --- 5. EVOLUTION by supersession: grow/split/merge are one append-only mechanism -----------

# (a) GROW: a new session adds a JJ-similar line → it joins the JJ cluster → the cluster's membership
#     changes → a NEW takeaway supersedes the prior JJ takeaway; the unchanged NIX cluster is skipped.
prior_current = {t["id"]: t for t in dream.current_takeaways()}
prior_jj = [t for t in prior_current.values() if t["support"]["events"] == 2 and t["producer"]["model"] == "fake"]
# (use the original "fake"-model takeaways as the baseline current set for this check)
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

# (b) The fold resolves arbitrary supersede links (split = one→many, merge = many→one) — unit-level.
seed = Path(ROOT) / "events" / "dream-seedfold.jsonl"
A = {"id": "aaaa", "member_events": ["e1"], "supersedes": [], "cites": ["e1"]}
B = {"id": "bbbb", "member_events": ["e2"], "supersedes": [], "cites": ["e2"]}
C = {"id": "cccc", "member_events": ["e1", "e2"], "supersedes": ["aaaa", "bbbb"], "cites": ["e1", "e2"]}
seed.write_text("\n".join(json.dumps(r) for r in (A, B, C)) + "\n", encoding="utf-8")
folded = {t["id"] for t in dream.current_takeaways()}
assert "cccc" in folded and "aaaa" not in folded and "bbbb" not in folded, \
    "merge (many→one): the merged takeaway supersedes both, which fold out"
seed.unlink()

print("OK — evolution/FOLD: supersede links resolve split (one→many) and merge (many→one) by append only.")

# (c) supersession is COVERAGE-CONDITIONED (the orphan fix): a prior is folded out ONLY if ALL its
#     events are re-covered this run. Seed a prior owning a real event + a ghost event no cluster covers;
#     a re-run that re-synthesizes the real cluster must NOT supersede it (else the ghost's coverage is
#     orphaned). The old owner-overlap logic WOULD have wrongly folded it out (it shares the real event).
real_id = dream.gather_events(ROOT)[0].id
guard = Path(ROOT) / "events" / "dream-00000000T000000-000000-000000-0000.jsonl"   # oldest run_id → loses the fold
guard.write_text(json.dumps({
    "id": "guardtk", "member_events": sorted([real_id, "ghost-never-covered"]), "supersedes": [],
    "cites": [real_id], "evidence": [], "status": "synthesized",
    "producer": {"stage": "dream", "model": "seed", "run_id": "00000000T000000-000000-000000-0000"}}) + "\n",
    encoding="utf-8")
assert "guardtk" in {t["id"] for t in dream.current_takeaways()}, "the seeded prior is current before the run"
cov = dream.run(DreamFake(cite="all"), model="fake-cov")          # fresh key → re-synthesizes the real clusters
assert cov.takeaways and any(real_id in (t.get("member_events") or []) for t in cov.takeaways), \
    "the re-synthesis emits a takeaway that SHARES the guard's real event (overlap exists)"
assert "guardtk" in {t["id"] for t in dream.current_takeaways()}, \
    "a prior with an UNcovered event survives — a dropped/split child never orphans its parent's events"
guard.unlink()
print("OK — evolution/COVERAGE: supersession is coverage-conditioned (no orphan), fold keys recency on run_id.")


# --- 6. concept seam + belief-change relation -----------------------------------------------

assert dream.load_concepts(ROOT) == [], "no concepts yet → empty (the seam is wired, review fills it)"
(Path(ROOT) / "concepts" / "c1.json").write_text(
    json.dumps({"id": "c1", "title": "Use jj", "statement": "Use jj, never git."}), encoding="utf-8")
assert [c["id"] for c in dream.load_concepts(ROOT)] == ["c1"], "a concept file is read once present"
# the relation is coerced against KNOWN ids: a real concept id sticks; an unknown one falls back to new
assert dream._clean_relation({"kind": "contradicts", "concept_id": "c1"}, {"c1"})["kind"] == "contradicts"
assert dream._clean_relation({"kind": "contradicts", "concept_id": "ghost"}, {"c1"})["kind"] == "new", \
    "a relation to a non-existent concept can't be trusted → new"
assert dream._clean_relation({"kind": "bogus"}, {"c1"})["kind"] == "new", "unknown relation kind → new"
(Path(ROOT) / "concepts" / "c1.json").unlink()
print("OK — concept seam reads the curated layer; belief-change relation coerced against known concepts.")


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

# (a) TRUST CHAIN re-anchored at dream's READ boundary: a glean event with a malformed span (null /
#     overshoot / inverted) against a REAL blob is dropped — never resolved to the whole blob or to
#     silently-clamped bytes and labelled "verified". (A foreign log line is the reachability vector.)
real_ev = [e for e in glean.load_events() if e["evidence"][0].get("byte_start") is not None][0]
ch = real_ev["cleaned_hash"]
blen = len(blobstore.get(ch).encode("utf-8"))
bad = [{"id": "bad-null", "cleaned_hash": ch, "evidence": [{"byte_start": None, "byte_end": None}], "confidence": 0.9},
       {"id": "bad-over", "cleaned_hash": ch, "evidence": [{"byte_start": 0, "byte_end": blen + 999}], "confidence": 0.9},
       {"id": "bad-inv", "cleaned_hash": ch, "evidence": [{"byte_start": 50, "byte_end": 10}], "confidence": 0.9}]
mal = Path(ROOT) / "events" / "glean-malformed.jsonl"
mal.write_text("\n".join(json.dumps(r) for r in bad) + "\n", encoding="utf-8")
assert {e.id for e in dream.gather_events(ROOT)}.isdisjoint({"bad-null", "bad-over", "bad-inv"}), \
    "malformed/foreign spans are rejected at the read boundary (no whole-blob or clamped 'verified' bytes)"
mal.unlink()

# (b) a concept file with a non-string id is skipped (load_concepts' 'never fatal' contract) — a non-
#     string id is unhashable and would otherwise make the known-id set build error EVERY cluster.
(Path(ROOT) / "concepts" / "bad.json").write_text(json.dumps({"id": ["oops"], "title": "x"}), encoding="utf-8")
assert dream.load_concepts(ROOT) == [], "a non-string concept id is malformed, skipped — never fatal"
assert dream.run(DreamFake(cite="all"), model="fake-badconcept").errored == 0, "a bad concept file wedges nothing"
(Path(ROOT) / "concepts" / "bad.json").unlink()

# (c) a model bump re-synthesizes the same grouping and REPLACES it in the current fold (one current
#     takeaway per grouping; the older model's record is not also current) — the intended semantics.
m1 = dream.run(DreamFake(cite="all", title="ONE"), model="bumpA")
if m1.takeaways:
    tid = m1.takeaways[0]["id"]
    assert {t["id"]: t for t in dream.current_takeaways()}[tid]["producer"]["model"] == "bumpA"
    dream.run(DreamFake(cite="all", title="TWO"), model="bumpB")
    assert {t["id"]: t for t in dream.current_takeaways()}[tid]["producer"]["model"] == "bumpB", \
        "a model bump replaces the grouping's current takeaway (fold keys recency on run_id, not glob order)"
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
