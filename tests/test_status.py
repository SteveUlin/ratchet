"""status tests: the read-only census over (1) a seeded tiny store — a mature claim awaiting
synthesize, an accepted claim, an un-resolved event, live seed+llm edges — and (2) an empty store,
which must emit clean zeros, never a traceback. The --json object and the text render read the SAME
census dict, so the JSON shape is asserted against `status.census` directly.

Fixtures follow test_resolve.py's idiom: real transcript → cleaned blob → chunkset → GleanFake
events with controlled summaries → resolve with a scripted ResolveFake. No network, no API key.
Run: `python tests/test_status.py`."""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-status-")

from ratchet import blobstore, chunk, config, glean, resolve, status  # noqa: E402
from ratchet.completer import Completion  # noqa: E402

EMPTY_DATASTORE = Path(tempfile.mkdtemp(prefix="ratchet-test-status-datastore-"))  # 0 available

# The G2 paraphrase pair (same lesson, residue band) + one distinct lesson (test_resolve's fixtures).
JJ_SEED = "always commit with jj and never use git for version control"
JJ_PARA = "version control goes through jj, so avoid reaching for git commands"
ZIG = "zig struct types are anonymous by default; assign them to a const to name them"
M_HI = {"surprise": 0.9, "insight": 0.3}


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
    def __init__(self, lines):
        self.lines = lines

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
                              "markers": M_HI, "confidence": 0.85})
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


class ResolveFake:
    def __init__(self, verdicts=()):
        self.verdicts, self.calls = list(verdicts), 0

    def __call__(self, system, user):
        v = self.verdicts[self.calls] if self.calls < len(self.verdicts) else "none"
        self.calls += 1
        return Completion(text=json.dumps({"verdict": v}), model="resolve-fake", cost_usd=0.001)


def seed_event(sid, line, repo):
    cs = make_session(sid, line, repo=repo)
    glean.run([cs], GleanFake([line]), model="fake", root=ROOT)


def write_accept(claim_id, root):
    """A minimal binding accept decision (review's verb, written directly so this test does not
    couple to review.py's evolving surface — only the decision SHAPE, which is the contract)."""
    at = config.now()
    body = {"verb": "accept", "target": claim_id, "at": at, "run_id": config.run_id()}
    s = blobstore.canonical_json(body)
    blobstore.ingest(s, source_kind="decision", source_id=blobstore.blob_hash(s), prev=None,
                     origin_ref={"stage": "review", "verb": "accept", "target": claim_id},
                     fetched_at=at, root=root)


# === 1. the seeded store: every census counter lands where the fixtures put the work ===============

ROOT = config.ensure_layout()

# Two sessions, same lesson paraphrased → resolve merges via the residue call → ONE mature claim
# (2 fresh sessions ≥ bar 1.5), why=null → the matured-awaiting-synthesize population (§7.3).
seed_event("st-s1", JJ_SEED, "alpha")
seed_event("st-s2", JJ_PARA, "beta")
fake = ResolveFake(["same-as-1"])
resolve.run(fake, model="fake", forget=False, root=ROOT)
assert fake.calls == 1, "the paraphrase pair must reach exactly one residue call"

# A third, distinct event seeded AFTER resolve — the un-consolidated backlog the census must show.
seed_event("st-s3", ZIG, "alpha")

pool = resolve.claim_pool(ROOT)
assert len(pool) == 1 and pool[0]["title"] in (JJ_SEED, JJ_PARA)  # Greedy ties → either seeds first
claim_id = pool[0]["id"]
write_accept(claim_id, ROOT)

c = status.census(ROOT, datastore=EMPTY_DATASTORE)

assert c["sources"] == {"tapped": 3, "available": 0}, c["sources"]

p = c["prep"]
assert p["woven"] == 3 and p["chunksets"] == 3, p
assert p["chunks"] >= 3, f"each session contributes at least its lesson chunk: {p}"
assert p["chunks_gleaned"] == p["chunks"] and p["chunks_pending"] == 0, \
    f"glean.run marked every chunk under the current prompt_version: {p}"

assert c["events"] == {"total": 3, "awaiting_resolve": 1}, c["events"]

cl = c["claims"]
assert cl["total"] == 1 and cl["active"] == 1 and cl["dormant"] == 0, cl
assert cl["mature"] == 1, f"2 fresh distinct sessions cross the 1.5 bar: {cl}"
assert cl["awaiting_synthesize"] == 1, f"mature + why=null is the §7.3 synth queue: {cl}"
assert cl["accepted"] == 1 and cl["contested"] == 0, cl
assert cl["edges"] == 2 and cl["llm_edges"] == 1, \
    f"one seed edge + one llm-adjudicated corroboration: {cl}"

rv = c["review"]
assert set(rv) == {"pending", "incubating", "proposals"}
assert all(isinstance(v, int) and v >= 0 for v in rv.values()), rv  # counts only — review.py is
assert rv["proposals"] == 0, rv                                     # a moving sibling surface

assert c["concepts"] == {"valid": 0}, c["concepts"]
assert c["generate"] == {"region_nonempty": False, "rules": 0}, c["generate"]

print("OK census — 3 tapped, all chunks gleaned, 1 event awaiting resolve, 1 mature claim awaiting")
print("            synthesize (why=null), accepted+edge counts exact, generate honestly empty.")


# === 2. --json emits the census object; the text render carries every section ======================

buf = io.StringIO()
with redirect_stdout(buf):
    status.main(["--json", "--datastore", str(EMPTY_DATASTORE)])
j = json.loads(buf.getvalue())
assert j == c, "--json must emit exactly the census object"
assert list(j) == ["sources", "prep", "events", "claims", "review", "concepts", "generate"]

buf = io.StringIO()
with redirect_stdout(buf):
    status.main(["--datastore", str(EMPTY_DATASTORE)])
text = buf.getvalue()
for head in ("SOURCES", "PREP", "EVENTS", "CLAIMS", "REVIEW", "CONCEPTS", "GENERATE"):
    assert head in text, f"text render missing the {head} section:\n{text}"
assert "1 awaiting synthesize (why=null)" in text, text
print("OK json+text — --json == census(); every section renders one line.")


# === 3. the empty store: zeros everywhere, no traceback ============================================

os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-status-empty-")
empty_root = config.ensure_layout()
z = status.census(empty_root, datastore=EMPTY_DATASTORE)
assert z == status.ZEROS, f"an empty store is all zeros: {z}"
buf = io.StringIO()
with redirect_stdout(buf):
    status.main(["--datastore", str(EMPTY_DATASTORE)])
assert "0 tapped" in buf.getvalue() and "region would be empty" in buf.getvalue()
print("OK empty — a data-less store answers with clean zeros, text and JSON alike.")

print("\nall status tests passed")
