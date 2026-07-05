"""operability-knob tests (ADR-0022): the manual operator's levers for driving a months-long backlog by
hand (no `tick` orchestrator — everything explicit). Deterministic, offline (tap has no LLM; dream/glean
need none for the enumeration filters; review is the pure backend). Four knobs, each pinned here:

  TAP FETCH SELECTION — `--last N` keeps the N most-recently-MODIFIED to-ingest files (after the cursor
    skip — "the last N I haven't already pulled"); `--since <date>` keeps files modified at/after a cutoff.
    Owned by the FETCHER (selection is per-source), distinct from the driver's `--limit` (items EXAMINED).
  PROCESSING FOCUS — `dream --source X` / `glean --source X` keep only items whose SOURCE handle
    contains X (case-insensitive substring), reached by the `cleaned_hash` → raw `origin_ref.project` hop;
    `glean --exclude Y` (repeatable) is the include filter's complement — it DROPS handle-matching chunks
    (the junk quarantine for fixture projects tapped before tap grew its own --exclude, ADR-0025).
  REVIEW PRIORITIZED SUBSET — `pending` ORDERS by importance (net entrenchment × confidence) descending,
    `--limit N` takes the top-N, `--source X` filters to a source. Highest-leverage calls first.

Run: `python tests/test_operability.py` (throwaway dirs)."""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-oper-")

from ratchet import blobstore, block, chunk, config, dream, glean, review, tap  # noqa: E402
from ratchet.completer import Completion  # noqa: E402

JJ = "always commit with jj and never use git for version control"


def use_store(prefix):
    """An isolated data root per section (writes mutate derived views), set on env so the stages' default
    `config.data_root()` resolves here too; the section also passes `root=` explicitly."""
    os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix=f"ratchet-test-oper-{prefix}-")
    return config.ensure_layout()


# ============================================================================================
# 1. TAP FETCH SELECTION: --last N (N newest by mtime) and --since <date>, after the cursor skip
# ============================================================================================

R1 = use_store("tap")
ds = Path(tempfile.mkdtemp(prefix="ratchet-oper-ds-"))
proj = ds / "proj-x"
proj.mkdir()
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()   # a fixed epoch so mtimes are controlled
# five to-ingest files, ONE DAY apart: f0 oldest … f4 newest. Distinct content so each is its own blob.
for i in range(5):
    p = proj / f"f{i}.jsonl"
    p.write_text(json.dumps({"cwd": f"/p{i}", "n": i}) + "\n", encoding="utf-8")
    os.utime(p, (BASE + i * 86400, BASE + i * 86400))


def stems(blk):
    """The session ids `blk.items()` yields, IN ORDER (tap's item is (path, fingerprint))."""
    return [path.stem for path, _fp in blk.items(R1)]


# default (no selector): every file is a candidate, in discover (alphabetical) order — today's behavior.
assert stems(tap.TapBlock(datastore=ds)) == ["f0", "f1", "f2", "f3", "f4"], "no selector → all candidates"

# --last 2: the 2 most-recently-MODIFIED, newest-first (f4 then f3) — NOT the driver's --limit (examined).
assert stems(tap.TapBlock(datastore=ds, last=2)) == ["f4", "f3"], "--last keeps the N newest by mtime, desc"
assert stems(tap.TapBlock(datastore=ds, last=4)) == ["f4", "f3", "f2", "f1"], "--last 4 keeps the 4 newest"

# --since a cutoff BETWEEN f1 and f2 (1.5 days in): only f2, f3, f4 survive (mtime >= cutoff).
cutoff = datetime.fromtimestamp(BASE + 1.5 * 86400, timezone.utc).isoformat()
assert set(stems(tap.TapBlock(datastore=ds, since=cutoff))) == {"f2", "f3", "f4"}, "--since keeps mtime >= cutoff"
# --since keeps DISCOVER order among survivors (no mtime re-sort unless --last is also set).
assert stems(tap.TapBlock(datastore=ds, since=cutoff)) == ["f2", "f3", "f4"], "--since alone keeps discover order"

# --last + --since compose: since narrows to {f2,f3,f4}, then last 2 takes the newest two of those.
assert stems(tap.TapBlock(datastore=ds, last=2, since=cutoff)) == ["f4", "f3"], "--last + --since compose"

# THE CURSOR INTERACTION — "the last N I haven't ALREADY pulled": a full tap records all five in the
# fingerprint cursor; new files arrive; --last selects among the UN-pulled survivors only.
block.run(tap.TapBlock(datastore=ds), root=R1)                 # pull all five (cursor now records f0..f4)
for i in range(5):
    assert blobstore.latest_version(f"f{i}", R1) is not None, "the first sweep ingested every file"
assert stems(tap.TapBlock(datastore=ds, last=2)) == [], "all five are cursor-skipped → no un-pulled candidates"
for j, mt in ((0, BASE + 10 * 86400), (1, BASE + 12 * 86400), (2, BASE + 11 * 86400)):  # g1 newest, g2 mid, g0 oldest
    g = proj / f"g{j}.jsonl"
    g.write_text(json.dumps({"cwd": f"/g{j}"}) + "\n", encoding="utf-8")
    os.utime(g, (mt, mt))
assert stems(tap.TapBlock(datastore=ds, last=2)) == ["g1", "g2"], \
    "--last picks the 2 newest of the UN-pulled new files (cursor-skipped originals never counted)"

# the CLI accepts + threads --last/--since (a --dry-run smoke: argparse wires them through, no raise).
tap.main(["--datastore", str(ds), "--last", "1", "--dry-run"])
tap.main(["--datastore", str(ds), "--since", cutoff, "--dry-run"])
# a malformed --since is rejected at the CLI (fail fast, not a silent empty selection).
try:
    tap.main(["--datastore", str(ds), "--since", "not-a-date"])
    assert False, "a bad --since must be refused"
except SystemExit:
    pass
print("OK 1 — tap: --last N = the N newest-by-mtime un-pulled (after the cursor skip), --since = mtime >= "
      "cutoff; they compose; the CLI wires through and refuses a bad --since.")


# ============================================================================================
# shared transcript fabrication (a real raw → cleaned → chunkset, so the project hop is genuine)
# ============================================================================================

def _rec(uuid, parent, type, **kw):
    r = {"type": type, "uuid": uuid, "parentUuid": parent, "isSidechain": False}
    r.update(kw)
    return r


def _amsg(mid, text):
    return {"role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}


def make_session(sid, line, project, root):
    """A real transcript whose RAW blob carries `origin_ref.project` — so the `cleaned_hash` → raw →
    `origin_ref.project` hop `--source` walks is genuine, not mocked. Returns the chunkset hash."""
    records = [_rec("u0", None, "user", message={"role": "user", "content": f"session {sid} kickoff"})]
    parent = "u0"
    for i in range(4):
        body = f"step {i}: " + ("λ wörk ✓ " * 20)
        if i == 2:
            body = line     # the durable line stands ALONE on its rendered line, so a line-selection
                            # (ADR-0026) resolves to EXACTLY it
        records.append(_rec(f"{sid}a{i}", parent, "assistant", message=_amsg(f"{sid}M{i}", body)))
        parent = f"{sid}a{i}"
    blob = "\n".join(json.dumps(r) for r in records) + "\n"
    raw_h, _ = blobstore.ingest(blob, source_kind="transcript", source_id=sid,
                                origin_ref={"session_id": sid, "project": project}, root=root)
    cs, _, _ = chunk.materialize(raw_h, budget=600, root=root)
    return cs


class GleanFake:
    def __init__(self, line):
        self.line = line

    def __call__(self, system, user):
        # point at the numbered prompt line carrying our durable line (ADR-0026: select lines, copy bytes)
        hit = next((int(num) for row in user.splitlines()
                    for num, sep, body in [row.partition("| ")]
                    if sep and num.strip().isdigit() and self.line in body), None)
        cands = [] if hit is None else [
            {"lines": {"from": hit, "to": hit}, "summary": f"summary of {self.line[:20]}",
             "markers": {"surprise": 0.3, "insight": 0.6}, "confidence": 0.85}]
        return Completion(text=json.dumps({"events": cands}), model="fake", cost_usd=0.001)


# ============================================================================================
# 2. dream --source: consolidate only events from a SOURCE matching the substring
# ============================================================================================

R2 = use_store("dream-source")
RAT, NIX = "home-sulin-ratchet", "home-sulin-nixos"
cs_r = make_session("d-rat", JJ, RAT, R2)
cs_n = make_session("d-nix", JJ, NIX, R2)
glean.run([cs_r], GleanFake(JJ), model="fake", root=R2)
glean.run([cs_n], GleanFake(JJ), model="fake", root=R2)

ws = dream.working_set(R2)
assert len(ws) == 2, "two un-consolidated events, one per project"
# the project hop resolves for each event (cleaned_hash → raw origin_ref.project).
projs = {blobstore.project_of(rv.event["cleaned_hash"], R2) for rv in ws}
assert projs == {RAT, NIX}, f"each event resolves to its source project: {projs}"

# filter_by_source (the focus filter) and DreamBlock.items() both keep only the matching project.
only_rat = dream.filter_by_source(ws, "ratchet", R2)
assert {rv.event["cleaned_hash"] for rv in only_rat} == \
    {rv.event["cleaned_hash"] for rv in ws if blobstore.project_of(rv.event["cleaned_hash"], R2) == RAT}, \
    "filter_by_source keeps only ratchet-project events"
assert len(only_rat) == 1 and blobstore.project_of(only_rat[0].event["cleaned_hash"], R2) == RAT

blk_r = dream.DreamBlock(GleanFake(JJ), GleanFake(JJ), route_model="fake", synth_model="fake", source_filter="ratchet")
items_r = blk_r.items(R2)
assert all(blobstore.project_of(rv.event["cleaned_hash"], R2) == RAT for rv in items_r), \
    "DreamBlock(source_filter='ratchet').items() yields ONLY ratchet-project events"
assert len(items_r) == 1 and blk_r.n_events == 1, "exactly the one ratchet event (n_events reflects the focus)"

blk_n = dream.DreamBlock(GleanFake(JJ), GleanFake(JJ), route_model="fake", synth_model="fake", source_filter="nixos")
assert {rv.event["cleaned_hash"] for rv in blk_n.items(R2)} == {ws_rv.event["cleaned_hash"] for ws_rv in ws
                                                                 if blobstore.project_of(ws_rv.event["cleaned_hash"], R2) == NIX}, \
    "source_filter='nixos' yields ONLY the nixos event"
# default (no source filter) yields the whole working set.
blk_all = dream.DreamBlock(GleanFake(JJ), GleanFake(JJ), route_model="fake", synth_model="fake")
assert len(blk_all.items(R2)) == 2, "no --source → every event (default behavior preserved)"
# a substring matching NEITHER project yields nothing.
blk_none = dream.DreamBlock(GleanFake(JJ), GleanFake(JJ), route_model="fake", synth_model="fake", source_filter="zzz")
assert blk_none.items(R2) == [], "a source filter matching no project focuses on nothing"
print("OK 2 — dream --source: filter_by_source + DreamBlock.items() keep ONLY the matching project's events "
      "(cleaned_hash → raw origin_ref.project); default None → all events.")


# ============================================================================================
# 3. glean --source / --exclude: extract only chunks from a SOURCE matching the substring;
#    exclude drops handle-matching chunks (the junk quarantine, --source's complement)
# ============================================================================================

R3 = use_store("glean-source")
cs_r3 = make_session("g-rat", JJ, RAT, R3)
cs_n3 = make_session("g-nix", JJ, NIX, R3)
targets = [cs_r3, cs_n3]

all_chunks = list(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets).items(R3))
assert len(all_chunks) > 2, "several chunks across the two chunksets (no source filter → all)"
rat_chunks = list(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets, source_filter="ratchet").items(R3))
assert rat_chunks and all(blobstore.project_of(it.chunk.cleaned_hash, R3) == RAT for it in rat_chunks), \
    "glean --source ratchet enumerates ONLY ratchet-project chunks"
assert len(rat_chunks) == sum(1 for it in all_chunks if blobstore.project_of(it.chunk.cleaned_hash, R3) == RAT), \
    "and exactly all of them (no over/under-selection)"
nix_chunks = list(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets, source_filter="nixos").items(R3))
assert nix_chunks and all(blobstore.project_of(it.chunk.cleaned_hash, R3) == NIX for it in nix_chunks), \
    "glean --source nixos enumerates ONLY nixos-project chunks"
assert len(rat_chunks) + len(nix_chunks) == len(all_chunks), "the two foci partition the full enumeration"
assert list(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets, source_filter="zzz").items(R3)) == [], \
    "a source filter matching no project enumerates nothing"

# --exclude: the include filter's complement — drops chunks whose handle contains ANY listed substring,
# case-insensitive, same handle vocabulary as --source (and tap --exclude). The junk-quarantine knob:
# the append-only store has no retire-source, so this is how fixture projects stay out of the LLM spend.
def chunk_keys(items):
    return {glean.chunk_key(it.chunk) for it in items}


ex_nix = list(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets, exclude=("nixos",)).items(R3))
assert chunk_keys(ex_nix) == chunk_keys(rat_chunks), \
    "--exclude nixos drops exactly the nixos-project chunks (the ratchet set survives intact)"
assert chunk_keys(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets,
                                   exclude=("NIXOS",)).items(R3)) == chunk_keys(rat_chunks), \
    "--exclude is case-insensitive, like --source"
# include + exclude COMPOSE, include first: 'sulin' matches BOTH handles, then exclude drops the nixos half.
both = glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets,
                        source_filter="sulin", exclude=("nixos",)).items(R3)
assert chunk_keys(both) == chunk_keys(rat_chunks), "--source + --exclude compose (include first, then exclude)"
# a no-match exclude is a NO-OP (drops only a positive match — it never silently narrows).
assert chunk_keys(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets,
                                   exclude=("zzz",)).items(R3)) == chunk_keys(all_chunks), \
    "an exclude matching no handle drops nothing"
# repeatable: every listed substring drops its matches.
assert list(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets,
                             exclude=("ratchet", "nixos")).items(R3)) == [], \
    "two excludes covering both projects quarantine everything"
# --dry-run counts reflect the exclusion honestly (nothing done yet in R3, so would_process == enumerated).
dry = block.run(glean.GleanBlock(GleanFake(JJ), model="fake", targets=targets, exclude=("nixos",)),
                dry_run=True, root=R3)
assert dry.would_process == len(rat_chunks), "--dry-run's would-process count sees the exclusion"
# the CLI accepts + threads --exclude (a --dry-run smoke over --all; env points at R3).
import contextlib  # noqa: E402
import io  # noqa: E402
out = io.StringIO()
with contextlib.redirect_stdout(out):
    glean.main(["--all", "--dry-run", "--exclude", "nixos"])
assert f"{len(rat_chunks)} chunk(s) would process" in out.getvalue(), \
    "the CLI threads --exclude through to the block (dry-run count matches the filtered enumeration)"
print("OK 3 — glean --source/--exclude: items() keeps ONLY the matching project's chunks; --exclude drops "
      "handle matches (case-insensitive, repeatable, composes include-first, no-match = no-op); --dry-run "
      "and the CLI see the same filtered set.")


# ============================================================================================
# 4. review: importance ORDERING + --limit (top-N) + --source (provenance filter)
# ============================================================================================

R4 = use_store("review")


def seed_takeaway(*, id, sessions, confidence, evidence=None, root):
    """A v2-shape MATURE takeaway blob with a controlled net entrenchment (`sessions` distinct, no
    contradictions → net == sessions) and `confidence`, so importance = sessions × confidence is exact."""
    rec_ = {"id": id, "title": id, "why": f"why {id}", "relation": {"kind": "new", "concept_id": None, "note": ""},
            "cites": [f"{id}-e{i}" for i in range(sessions)], "evidence": evidence or [],
            "support": {"events": sessions, "sessions": sessions},
            "sessions_seen": [f"{id}-s{i}" for i in range(sessions)],
            "markers": {k: 0.0 for k in glean.MARKER_KINDS}, "confidence": confidence,
            "last_seen": "2024-01-01T00:00:00+00:00"}
    blobstore.ingest(blobstore.canonical_json(rec_), source_kind="takeaway", source_id=id,
                     origin_ref={"stage": "dream", "model": "seed"}, root=root)
    return id


# four MATURE takeaways with DISTINCT importance (net sessions × confidence), seeded out of rank order:
#   A: 5×0.9 = 4.50   B: 4×0.5 = 2.00   C: 2×0.8 = 1.60   D: 3×0.4 = 1.20
seed_takeaway(id="C", sessions=2, confidence=0.8, root=R4)
seed_takeaway(id="A", sessions=5, confidence=0.9, root=R4)
seed_takeaway(id="D", sessions=3, confidence=0.4, root=R4)
seed_takeaway(id="B", sessions=4, confidence=0.5, root=R4)
# importance() is net_ENTRENCHMENT × confidence (ADR-0023): recency-weighted net distinct sessions. The
# session ids here are not real transcripts → undated → weight 1.0 (fresh), so net_entrenchment == the raw
# count and importance reduces to net_sessions × confidence (back-compat for fresh evidence).
assert review.importance({"sessions_seen": ["a", "b", "c", "d", "e"], "support": {"sessions": 5},
                          "confidence": 0.9}) == 4.5, "importance = net entrenchment × conf (fresh → count)"
assert review.importance({"sessions_seen": ["a", "b", "c"], "contradiction_evidence": [{"session_id": "x"}],
                          "confidence": 1.0}) == 2.0, "a contradiction NETS the entrenchment down (ADR-0012/0023)"

ordered = [t["takeaway_id"] for t in review.pending(R4)]
assert ordered == ["A", "B", "C", "D"], f"pending() is ORDERED by importance descending: {ordered}"
assert [t["takeaway_id"] for t in review.pending(R4, limit=2)] == ["A", "B"], "--limit N returns the top-N by importance"
assert [t["takeaway_id"] for t in review.pending(R4, limit=1)] == ["A"], "--limit 1 returns the single most-important"
assert len(review.pending(R4, limit=99)) == 4, "a limit above the queue size returns all (no padding)"
assert len(review.pending(R4, limit=0)) == 4, "limit 0 = EVERYTHING — the explicit escape hatch, never []"
cards, total = review.pending(R4, limit=2, with_total=True)
assert [t["takeaway_id"] for t in cards] == ["A", "B"] and total == 4, \
    "with_total carries the FULL backlog depth beside the slice (the honest 'top N of M' header)"

# determinism + stability: a re-query is byte-identical, and equal-importance ties keep derivation order.
assert [t["takeaway_id"] for t in review.pending(R4)] == ordered, "ordering is deterministic across calls"
R4b = use_store("review-ties")
seed_takeaway(id="tie-second", sessions=2, confidence=0.5, root=R4b)   # seeded first
seed_takeaway(id="tie-first", sessions=2, confidence=0.5, root=R4b)    # same importance, seeded second
tie_order = [t["takeaway_id"] for t in review.pending(R4b)]
cur_order = [t["id"] for t in dream.current_takeaways(R4b)]            # the derivation order ties fall back to
assert tie_order == cur_order, f"a stable sort keeps derivation order for equal-importance ties: {tie_order}"

# --source: a takeaway matches the queue filter if ANY cited span comes from that source. Seed two MATURE
# takeaways with REAL evidence from two projects, then filter.
R4c = use_store("review-source")
cs_rat = make_session("rv-rat", JJ, RAT, R4c)
cs_nix = make_session("rv-nix", JJ, NIX, R4c)


def real_evidence(cs, line, root):
    ch = chunk.load(cs, root)[0].cleaned_hash
    data = blobstore.get(ch, root).encode("utf-8")
    off = data.find(line.encode("utf-8"))
    assert off >= 0, "the durable line is a real substring of the cleaned blob"
    return [{"event_id": f"e-{ch[:6]}", "cleaned_hash": ch, "byte_start": off,
             "byte_end": off + len(line.encode("utf-8")), "quote": line}]


seed_takeaway(id="T-rat", sessions=3, confidence=0.9, evidence=real_evidence(cs_rat, JJ, R4c), root=R4c)
seed_takeaway(id="T-nix", sessions=3, confidence=0.9, evidence=real_evidence(cs_nix, JJ, R4c), root=R4c)
assert {t["takeaway_id"] for t in review.pending(R4c)} == {"T-rat", "T-nix"}, "both mature takeaways are in the queue"
assert [t["takeaway_id"] for t in review.pending(R4c, source_filter="ratchet")] == ["T-rat"], \
    "--source ratchet filters the queue to the ratchet-project takeaway"
assert [t["takeaway_id"] for t in review.pending(R4c, source_filter="nixos")] == ["T-nix"], "--source nixos → only the nixos one"
assert review.pending(R4c, source_filter="zzz") == [], "a source filter matching no project empties the queue"

# --source scopes --incubating the same way — a focused sitting's "N below the bar" counts the SAME slice
# it points at, not the global backlog. Seed one below-bar takeaway per project beside the mature pair.
seed_takeaway(id="I-rat", sessions=1, confidence=0.9, evidence=real_evidence(cs_rat, JJ, R4c), root=R4c)
seed_takeaway(id="I-nix", sessions=1, confidence=0.9, evidence=real_evidence(cs_nix, JJ, R4c), root=R4c)
assert {t["takeaway_id"] for t in review.incubating(R4c)} == {"I-rat", "I-nix"}, "both below-bar takeaways incubate"
assert [t["takeaway_id"] for t in review.incubating(R4c, source_filter="ratchet")] == ["I-rat"], \
    "--source scopes --incubating to the same slice the filtered queue points at"
assert review.incubating(R4c, source_filter="zzz") == [], "an unmatched source filter empties incubating too"
print("OK 4 — review: pending() ORDERS by importance (net entrenchment × confidence) descending; --limit "
      "takes the top-N; ties keep derivation order (stable); --source filters pending AND incubating.")


# ============================================================================================
# 5. review --proposals: ordered by STAKES, with --limit and --source
# ============================================================================================

R5 = use_store("review-prop")
from ratchet import garden  # noqa: E402


def seed_proposal(*, pid, op, stakes, concept_ids, root):
    content = {"proposal_id": pid, "op": op, "params": {}, "concept_ids": concept_ids,
               "rationale": f"rationale {pid}", "stakes": stakes, "cluster_leader": concept_ids[0] if concept_ids else "",
               "prompt_version": "test"}
    blobstore.ingest(blobstore.canonical_json(content), source_kind=garden.PROPOSAL_KIND, source_id=pid,
                     origin_ref={"stage": "garden"}, root=root)
    return pid


seed_proposal(pid="gp-lo", op="reparent", stakes=0.10, concept_ids=[], root=R5)
seed_proposal(pid="gp-hi", op="merge", stakes=0.90, concept_ids=[], root=R5)
seed_proposal(pid="gp-mid", op="abstract", stakes=0.50, concept_ids=[], root=R5)
order = [p["proposal_id"] for p in review.pending_proposals(R5)]
assert order == ["gp-hi", "gp-mid", "gp-lo"], f"proposals ORDERED by stakes descending: {order}"
assert [p["proposal_id"] for p in review.pending_proposals(R5, limit=1)] == ["gp-hi"], "--limit takes the top-N by stakes"
print("OK 5 — review --proposals: ordered by STAKES descending (the on-proposal leverage signal); --limit "
      "takes the highest-stakes top-N.")

print("\nall operability tests passed.")
