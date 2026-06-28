"""garden tests (3b, ADR-0014): the gardener Block's MANAGED-TAGS phase, exercised OFFLINE with a FAKE
tagger (no network, no API key) so the suite is deterministic. garden runs ONE cheap tagger call per
valid concept over the FROZEN controlled vocabulary, commits an append-only per-concept assignment, and
auto-adds any proposed new tag — all on the blob model (vocab + assignments are DERIVED FOLDS, the
concept blob stays IMMUTABLE). The load-bearing checks:

  STORE FOLDS — `vocabulary` folds the append-only `tag` blobs; a proposed new tag GROWS it; `concept_tags`
    is the latest-wins fold of a concept's assignments; concepts are never re-versioned by tagging.
  THE BLOCK — drive `garden.run`: a concept gets its scripted tags committed; idempotency (a concept
    tagged against the CURRENT vocab is done-skipped on re-run — zero tagger calls); a vocab CHANGE flips
    the fingerprint and re-tags everything; coercion drops a hallucinated tag.
  GRAPH SHARPENING — a single shared tag fires a `shares-tag` EDGE (the thematic relation stays VISIBLE)
    but does NOT cluster two concepts alone (W_TAG < CLUSTER_THRESHOLD); CORROBORATION — a SECOND shared
    tag (or a tag + a shared file/repo) — clears the bar and clusters them. (With no tags the 3a graph is
    unchanged — the golden in test_concepts proves that.)
  VOCAB_MAX — the controlled vocabulary is CAPPED: once it fills, new-tag proposals are DROPPED (reuse-only),
    enforcing the load-bearing "small, in-prompt vocabulary" invariant.

Run: `python tests/test_garden.py` (throwaway dir)."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-garden-")

from ratchet import blobstore, concepts, config, garden, weave  # noqa: E402
from ratchet.completer import Completion  # noqa: E402

R = config.ensure_layout()


# --- synthetic transcripts → cleaned blobs → concepts (mirrors test_concepts' harness) ------------

def rec(uuid, parent, typ, **kw):
    r = {"type": typ, "uuid": uuid, "parentUuid": parent}
    r.update(kw)
    return r

def amsg(mid, *blocks):
    return {"role": "assistant", "id": mid, "content": list(blocks)}

def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def jsonl(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def add_session(records, *, project, sid, mtime):
    raw_h, _ = blobstore.ingest(jsonl(records), source_kind="transcript", source_id=sid,
                                origin_ref={"project": project, "session_id": sid, "mtime": mtime,
                                            "path": f"/store/{project}/{sid}.jsonl"},
                                fetched_at=mtime, root=R)
    cleaned_h, _, _ = weave.materialize(raw_h, root=R)
    return cleaned_h


def mint_concept(cid, title, cleaned_hashes):
    evidence = [{"event_id": f"e-{cid}-{i}", "cleaned_hash": ch, "byte_start": 0, "byte_end": 1,
                 "quote": "q", "context": "q"} for i, ch in enumerate(cleaned_hashes)]
    concept = {"id": cid, "title": title, "statement": f"the {title} lesson",
               "evidence": evidence, "source_takeaway": f"t-{cid}"}
    blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=cid,
                     origin_ref={"stage": "test"}, root=R)


# Four sessions, DELIBERATELY DISJOINT on every provenance facet (distinct repo / file / EDIT tool, and a
# month+ apart so no temporal-proximity edge) — so the ONLY thing that can relate two of them is a tag.
SESS_P = [rec("p-u", None, "user", message={"role": "user", "content": "edit x"}),
          rec("p-1", "p-u", "assistant", message=amsg("P1", tool_use("pt1", "Edit", file_path="/p/x.py",
                                                                      old_string="a", new_string="b")))]
SESS_Q = [rec("q-u", None, "user", message={"role": "user", "content": "write y"}),
          rec("q-1", "q-u", "assistant", message=amsg("Q1", tool_use("qt1", "Write", file_path="/q/y.py",
                                                                      content="hi")))]
SESS_R = [rec("r-u", None, "user", message={"role": "user", "content": "multiedit z"}),
          rec("r-1", "r-u", "assistant", message=amsg("R1", tool_use("rt1", "MultiEdit", file_path="/r/z.py",
                                                                      edits=[{"old_string": "a", "new_string": "b"}])))]
SESS_S = [rec("s-u", None, "user", message={"role": "user", "content": "notebook w"}),
          rec("s-1", "s-u", "assistant", message=amsg("S1", tool_use("st1", "NotebookEdit",
                                                                      notebook_path="/s/w.ipynb", new_source="x")))]

ch_p = add_session(SESS_P, project="proj-p", sid="sess-p", mtime="2026-01-01T10:00:00+00:00")
ch_q = add_session(SESS_Q, project="proj-q", sid="sess-q", mtime="2026-03-01T10:00:00+00:00")
ch_r = add_session(SESS_R, project="proj-r", sid="sess-r", mtime="2026-05-01T10:00:00+00:00")
ch_s = add_session(SESS_S, project="proj-s", sid="sess-s", mtime="2026-07-01T10:00:00+00:00")

mint_concept("c-aaa", "jj over git", [ch_p])         # → tag "version-control"
mint_concept("c-bbb", "commit early often", [ch_q])  # → tag "version-control" (shares ONE tag with c-aaa)
mint_concept("c-ccc", "nix develop shell", [ch_r])   # → tags "nixos","shell"
mint_concept("c-ddd", "flake inputs pin", [ch_s])    # → tags "nixos","shell" (shares TWO tags with c-ccc)


# --- the FAKE tagger: scripted per-concept tags, proposing a tag as NEW only when the FROZEN vocab ----
# --- (rendered into the prompt) does not already carry it — a realistic reuse-first tagger -----------

class TaggerFake:
    def __init__(self, by_title, *, cost=0.001):
        self.by_title = by_title           # {concept title: [wanted slugs]}
        self.cost, self.calls = cost, 0

    def __call__(self, system, user):
        self.calls += 1
        m = re.search(r"title: '(.*?)'", user)
        want = list(self.by_title.get(m.group(1) if m else "", []))
        vocab_slugs = set(re.findall(r"^- (\S+?)(?::|$)", user.split("VOCABULARY", 1)[-1], re.M))
        new = [{"slug": s, "gloss": f"the {s} theme"} for s in want if s not in vocab_slugs]
        return Completion(text=json.dumps({"tags": want, "new_tags": new}),
                          model="tagger-fake", cost_usd=self.cost)


WANT = {"jj over git": ["version-control"], "commit early often": ["version-control"],
        "nix develop shell": ["nixos", "shell"], "flake inputs pin": ["nixos", "shell"]}


# === 0. pure store folds + coercion (no LLM) ========================================================

assert garden.slugify("Version Control!") == "version-control", "slugify → short kebab-case"
assert garden.slugify("  --FOO__bar--  ") == "foo-bar", garden.slugify("  --FOO__bar--  ")
assert garden.vocabulary(R) == {}, "vocabulary starts EMPTY (no tag blobs yet)"
assert garden.concept_tags("c-aaa", R) == [], "an untagged concept folds to no tags"

# _clean_assigned drops a hallucinated slug (one not in vocab ∪ proposed-new); keeps the allowed one.
assert garden._clean_assigned(["version-control", "ghost"], {"version-control"}) == ["version-control"], \
    "a tag the model neither knows nor proposes is dropped (the _clean_route rule)"
# _clean_new_tags slugifies, drops a slug already in the frozen vocab, dedups.
assert garden._clean_new_tags([{"slug": "Nix OS", "gloss": "g"}, {"slug": "version-control"}],
                              {"version-control": ""}) == [("nix-os", "g")], "novel + slugified only"


# === 1. first run (empty vocab): scripted tags committed + a proposed new tag grows the vocabulary ===

concept_hashes_before = dict(blobstore.latest_by_kind("concept", R))
tagger = TaggerFake(WANT)
run1 = garden.run(tagger, root=R)

assert run1.examined == 4 and run1.processed == 4 and run1.skipped == 0, \
    f"first run tags all four concepts: {run1.examined}/{run1.processed}/{run1.skipped}"
assert garden.concept_tags("c-aaa", R) == ["version-control"], "scripted tag committed + folded back"
assert garden.concept_tags("c-bbb", R) == ["version-control"], garden.concept_tags("c-bbb", R)
assert garden.concept_tags("c-ccc", R) == ["nixos", "shell"], garden.concept_tags("c-ccc", R)
assert garden.concept_tags("c-ddd", R) == ["nixos", "shell"], garden.concept_tags("c-ddd", R)
# the proposed new tags GREW the vocabulary fold (it started empty).
assert set(garden.vocabulary(R)) == {"version-control", "nixos", "shell"}, \
    f"proposed new tags grew the vocab: {set(garden.vocabulary(R))}"
assert garden.vocabulary(R)["nixos"] == "the nixos theme", "the proposed gloss is stored"
# the concept BLOBS were never re-versioned by tagging (concepts stay IMMUTABLE).
assert blobstore.latest_by_kind("concept", R) == concept_hashes_before, \
    "tagging must NOT re-version any concept blob"
print("OK §1 — scripted tags committed; a proposed new tag grows the vocab; concepts stay immutable.")


# === 2. tags sharpen the graph: ONE tag fires the EDGE; only CORROBORATION clusters ==================

graph = concepts.concept_graph(R)

def has_edge(src, dst, kind):
    return any({e["source"], e["target"]} == {src, dst} and e["kind"] == kind for e in graph["edges"])

def tag_shared(src, dst):
    return [e["shared"] for e in graph["edges"]
            if e["kind"] == "shares-tag" and {e["source"], e["target"]} == {src, dst}]

# baseline truth: every pair shares NO provenance facet (distinct repo/file/tool, a month+ apart) — the
# ONLY edges in this graph are tag ones, so the tag signal is doing all the work.
assert not has_edge("c-aaa", "c-bbb", "shares-file") and not has_edge("c-aaa", "c-bbb", "shares-tool") \
    and not has_edge("c-aaa", "c-bbb", "shares-repo"), "the two concepts share no provenance facet"

# ONE shared tag → a shares-tag EDGE (the thematic relation stays VISIBLE) carrying the shared slug...
assert has_edge("c-aaa", "c-bbb", "shares-tag"), "one shared tag still fires the shares-tag edge"
assert tag_shared("c-aaa", "c-bbb") == [["version-control"]], "the edge carries the one shared slug"

# ...but one tag ALONE is below the cluster bar (W_TAG=2.0 < CLUSTER_THRESHOLD=3.0), so c-aaa and c-bbb
# are NOT clustered together. CORROBORATION clusters: c-ccc and c-ddd share TWO tags (2·W_TAG = 4.0 ≥ bar).
clusters = {cl["leader"]: cl["members"] for cl in graph["clusters"]}
assert clusters == {"c-aaa": ["c-aaa"], "c-bbb": ["c-bbb"], "c-ccc": ["c-ccc", "c-ddd"]}, \
    f"one tag does NOT cluster (c-aaa|c-bbb apart); two shared tags DO (c-ccc+c-ddd): {clusters}"
assert has_edge("c-ccc", "c-ddd", "shares-tag") and tag_shared("c-ccc", "c-ddd") == [["nixos", "shell"]], \
    "the corroborating pair shares both slugs on its shares-tag edge"
print("OK §2 — one shared tag fires the edge but stays below the cluster bar; corroboration clusters.")


# === 3. idempotency: re-run against the SAME vocab does ZERO tagger work =============================

# run2 re-tags (run1 grew the vocab from empty → the fingerprint flipped, so run1's markers don't match);
# run2 proposes nothing new (both tags now IN the frozen vocab), so the vocab is STABLE after it.
calls_before_run2 = tagger.calls
run2 = garden.run(tagger, root=R)
assert run2.fingerprint != run1.fingerprint, "run1 grew the vocab → run2's vocab fingerprint differs"
assert run2.processed == 4, "the vocab change re-tagged every concept (fingerprint flipped the done-key)"
assert tagger.calls == calls_before_run2 + 4, "run2 made one tagger call per concept"
assert set(garden.vocabulary(R)) == {"version-control", "nixos", "shell"}, \
    "run2 proposed no new tags — vocab stable"

# run3: the vocab is now stable (same fingerprint as run2) → every concept is done-skipped, ZERO calls.
calls_before_run3 = tagger.calls
run3 = garden.run(tagger, root=R)
assert run3.fingerprint == run2.fingerprint, "no vocab change → same fingerprint"
assert run3.examined == 4 and run3.skipped == 4 and run3.processed == 0, \
    f"a concept tagged against the CURRENT vocab is done-skipped: {run3.examined}/{run3.skipped}/{run3.processed}"
assert tagger.calls == calls_before_run3, "an idempotent re-run makes ZERO tagger calls"
print("OK §3 — re-tag on vocab change, then a stable re-run skips every concept with zero LLM calls.")


# === 4. a vocab change re-tags: grow the vocab out-of-band, the fingerprint flips, every concept re-tags

garden.add_tag("testing", "the testing theme", R, run_id="manual", model="seed")
calls_before_run4 = tagger.calls
run4 = garden.run(tagger, root=R)
assert run4.fingerprint != run3.fingerprint, "adding a tag flips the vocab fingerprint"
assert run4.processed == 4 and run4.skipped == 0, "the fingerprint flip re-tags every concept"
assert tagger.calls == calls_before_run4 + 4, "the re-tag is one tagger call per concept"
print("OK §4 — a vocab change (a new tag) flips the fingerprint and re-tags every concept.")


# === 5. VOCAB_MAX cap: a FULL frozen vocab admits NO new tag — the small-in-prompt-vocab INVARIANT ====

# The no-embeddings design renders the WHOLE vocabulary into every prompt, so its size is load-bearing;
# VOCAB_MAX (256 in prod, never hit on the happy path) is the hard backstop. Shrink it for the test, then
# FILL the vocab to the cap out-of-band — which also flips the fingerprint, so the next run RE-tags rather
# than done-skips — and assert a tagger proposing fresh novel tags grows the fold NO further.
garden.VOCAB_MAX = len(garden.vocabulary(R)) + 2        # a small cap for the test (prod is 256)
while len(garden.vocabulary(R)) < garden.VOCAB_MAX:     # fill to EXACTLY the cap with throwaway tags
    garden.add_tag(f"filler-{len(garden.vocabulary(R))}", "filler", R, run_id="seed", model="seed")
cap = len(garden.vocabulary(R))
assert cap == garden.VOCAB_MAX, "seeded the vocab to exactly the cap"

flood = TaggerFake({"jj over git": ["novel-a", "novel-b"], "commit early often": ["novel-c"],
                    "nix develop shell": ["novel-d"], "flake inputs pin": ["novel-e"]})
run5 = garden.run(flood, root=R)
assert run5.processed == 4, "the filled vocab flipped the fingerprint → every concept re-tags"
assert len(garden.vocabulary(R)) == cap, \
    f"at VOCAB_MAX the tagger's novel proposals are DROPPED — the vocab stops growing: " \
    f"{len(garden.vocabulary(R))} != {cap}"
print("OK §5 — at VOCAB_MAX new-tag proposals are dropped; the vocabulary stops growing (reuse-only).")

print("test_garden: all assertions passed")
