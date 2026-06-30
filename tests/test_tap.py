"""tap regression tests, now THROUGH the block.py driver (ADR-0009): tap is a uniform Block
whose item is a transcript file (surfaced via the fingerprint cursor) and whose process ingests
the raw blob. The original three guarantees are preserved verbatim, just asserted on the uniform
`Report` instead of the old printed tallies:

  §1 per-file error isolation — one file raising OSError counts report.errored, run continues,
     siblings still ingest (and the failed file is retried, never marked done);
  §2 idempotent re-tap — a clean re-run copies nothing (report.processed == 0), and skips at
     session granularity via the cursor + the per-session processed marker;
  §3 touched-once — a content-identical touch (mtime bump) is re-read AT MOST once, then the cheap
     (size, mtime) cursor tier in items() filters it before it is even examined.

Plus block-surface assertions the migration must hold: streaming progress lands per item, the
processed marker is a real decision blob, --dry-run lists without writing, --limit caps examined,
and --source-id scopes to one session.

Run: `python tests/test_tap.py` (throwaway dirs)."""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-data-")

from ratchet import blobstore, block, config, tap  # noqa: E402

config.ensure_layout()
ds = Path(tempfile.mkdtemp(prefix="ratchet-test-ds-"))
proj = ds / "proj-x"
proj.mkdir()
(proj / "good.jsonl").write_text('{"cwd":"/p","gitBranch":"main"}\n', encoding="utf-8")
(proj / "bad.jsonl").write_text("whatever\n", encoding="utf-8")
_orig_read = tap.read_origin


class Probe:
    """A Progress-like progress sink — it satisfies the driver's Progress PROTOCOL (start/tick/stop),
    so the driver drives it exactly as the real bar. Records one entry per item as it LANDS, so a test
    can assert the run is watchable per item (not batched). It tracks its own running tallies (the new
    decoupled tick takes only key+outcome, not the Report), so the tuple still carries live counters.
    A skipped item lands no per-item line (a skip is a bare counter)."""
    def __init__(self):
        self.lines = []
        self.examined = self.processed = self.skipped = self.errored = 0

    def start(self, *, total, todo, already, backlog=0):
        pass

    def tick(self, key, outcome, *, outputs=0, cost=0.0):
        self.examined += 1
        if outcome == "done":
            self.processed += 1
        elif outcome == "skipped":
            self.skipped += 1
            return                                    # a skip is a counter, not a landed per-item line
        elif outcome == "errored":
            self.errored += 1
        self.lines.append((key, outputs, cost, outcome == "dry_run", outcome == "errored",
                           self.examined, self.processed, self.skipped, self.errored))

    def stop(self):
        pass


def run(*, progress=None, **kw):
    """Drive a fresh TapBlock over the test datastore. A fresh instance each call mirrors a fresh
    process invocation — the cursor is reloaded from disk inside items(), so cross-run idempotency is
    exercised honestly (no in-memory carryover masks a missing cursor flush)."""
    return block.run(tap.TapBlock(datastore=ds), progress=progress, **kw)


def done_targets():
    """The set of session ids currently marked done — folding the fingerprint out of each done-key.
    tap's done-key is (f"{session}:{size}:{mtime}",), so a session is 'done' if ANY of its fingerprint
    keys is in the done-set. (The fingerprint suffix is what keeps a content change re-processable —
    see TapBlock.key — but a test usually only cares whether a session was tapped at all.)"""
    out = set()
    for (target,) in block.done_index("tap", config.data_root()):
        # the session id is the first ':'-delimited segment (path.stem, colon-free for UUIDs); the
        # rest is the size:mtime fingerprint (the mtime itself carries colons, so split once only).
        out.add(target.split(":", 1)[0])
    return out


# §1. error isolation: one file raising OSError must NOT abort the run; siblings still ingest, and
#     the failed file is left for retry (no processed marker written for it).
def boom(p):
    if p.name == "bad.jsonl":
        raise OSError("simulated unreadable")
    return _orig_read(p)

tap.read_origin = boom
probe = Probe()
rep = run(progress=probe)
tap.read_origin = _orig_read
assert rep.errored == 1, f"bad file isolated, not aborting: {rep}"
assert rep.processed >= 1, f"good ingested despite a broken sibling: {rep}"
assert blobstore.latest_version("good") is not None, "good copied despite a broken sibling"
# the broken file got NO processed marker (its key never entered the done-set) → it is retried.
done_after_boom = done_targets()
assert "bad" not in done_after_boom, "a failed file must not be marked done (it must retry)"
assert "good" in done_after_boom, "the good file is marked done (fingerprint-keyed, params=())"
# progress streamed per item: one errored line for bad, one landed line for good (as they landed).
assert any(errored for (key, n, c, dr, errored, ex, pr, sk, er) in probe.lines), \
    f"an errored progress line streamed for the failed file: {probe.lines}"
assert any((not dr and not errored and n == 1) for (key, n, c, dr, errored, ex, pr, sk, er)
           in probe.lines), f"a landed progress line streamed for good: {probe.lines}"

# settle: bad.jsonl is actually readable; a normal run ingests it (and retries the formerly-failed
# file, proving the retry path).
rep_settle = run()
assert rep_settle.processed >= 1, f"the formerly-failed file is retried and ingested: {rep_settle}"
assert "bad" in done_targets(), "bad now marked done after retry"


# §2. idempotent re-tap copies nothing new — and reports it the uniform way (processed == 0,
#     skipped == every session via the cursor).
rep2 = run()
assert rep2.processed == 0, f"idempotent re-tap ingests nothing: {rep2}"
assert rep2.outputs == 0, f"idempotent re-tap produces no output blobs: {rep2}"
# the cheap cursor tier filters unchanged files INSIDE items(), so they are not even examined —
# a re-tap of a fully-cursored datastore examines nothing.
assert rep2.examined == 0, f"cursored files filtered in items(), never examined: {rep2}"
# the processed markers persist (one per session) — the done-set carries both sessions.
done = done_targets()
assert "good" in done and "bad" in done, f"a marker per session: {done}"


# §3. touch (mtime bump, content identical) is re-read at most ONCE, then cheap-skipped. The cheap
#     (size, mtime) tier lives in items(), so the second run never even reads the file.
reads = {"n": 0}

def counting(p):
    reads["n"] += 1
    return _orig_read(p)

os.utime(proj / "good.jsonl", None)  # bump mtime; content unchanged
tap.read_origin = counting
reads["n"] = 0
r_first = run()                       # cheap tier misses (mtime changed) -> reads good once, ingests
first = reads["n"]
reads["n"] = 0
r_second = run()                      # cursor now current -> items() filters good, no read
second = reads["n"]
tap.read_origin = _orig_read
assert first >= 1 and second == 0, f"touched file re-read once then skipped (got {first}, {second})"
# the touch changed no bytes, so the blobstore re-ingest no-ops: 0 new outputs despite the re-read.
assert r_first.outputs == 0, f"a content-identical touch ingests no new blob: {r_first}"
# second run: the unchanged file was filtered in items(), so it was not examined at all.
assert r_second.examined == 0, f"a cursored file is not examined on the next run: {r_second}"


# §4. the processed marker is a REAL decision blob — verify its body shape (uniform with every stage)
#     so a future reader trusts the done-set. params=() → the body carries an empty params list, and
#     the target is the fingerprint key (session id + size + mtime) per TapBlock.key.
all_tap = list(blobstore.decisions_for(None, config.data_root(), verb="processed", stage="tap"))
good_decs = [b for b in all_tap if b.get("target", "").startswith("good:")]
assert good_decs, f"a processed decision exists for the good session: {[b['target'] for b in all_tap]}"
body = good_decs[0]
assert body["verb"] == "processed" and body["stage"] == "tap"
assert body["target"].split(":", 1)[0] == "good", f"marker target carries the session id: {body}"
assert body["params"] == [], f"tap has no idempotency params (empty params list): {body}"


# §5. --dry-run lists what would be copied without writing — a new file appears in would_process but
#     no blob, no marker, and the cursor is NOT advanced (so a real run still picks it up).
(proj / "fresh.jsonl").write_text('{"cwd":"/q","gitBranch":"dev"}\n', encoding="utf-8")
probe_dry = Probe()
rep_dry = run(progress=probe_dry, dry_run=True)
assert rep_dry.would_process >= 1, f"--dry-run lists the new file: {rep_dry}"
assert rep_dry.processed == 0, f"--dry-run ingests nothing: {rep_dry}"
assert blobstore.latest_version("fresh") is None, "--dry-run wrote no blob for the new session"
assert "fresh" not in done_targets(), "--dry-run wrote no marker for the new session"
assert all(dr for (key, n, c, dr, errored, *_rest) in probe_dry.lines if key == "fresh"), \
    f"the dry-run progress line is flagged dry_run: {probe_dry.lines}"
# the real run still ingests it (the dry-run left the cursor untouched).
rep_real = run()
assert blobstore.latest_version("fresh") is not None, "a real run ingests the file dry-run only listed"
assert rep_real.processed >= 1, f"the real run ingests the new session: {rep_real}"


# §6. --source-id scopes items() to one session (path.stem); other sessions are not even enumerated.
(proj / "scoped.jsonl").write_text('{"cwd":"/r","gitBranch":"feat"}\n', encoding="utf-8")
rep_scoped = run(source_id="scoped")
assert rep_scoped.processed == 1, f"--source-id ingests just that session: {rep_scoped}"
# only the scoped session is touched; everything else stays as it was (examined nothing else).
assert rep_scoped.examined == 1, f"--source-id enumerates only the one session: {rep_scoped}"
assert blobstore.latest_version("scoped") is not None, "the scoped session was ingested"


# §7. --limit caps items EXAMINED (before the done-skip). Add two genuinely new files; --limit 1
#     examines exactly one of them this run.
(proj / "lim-a.jsonl").write_text('{"cwd":"/a"}\n', encoding="utf-8")
(proj / "lim-b.jsonl").write_text('{"cwd":"/b"}\n', encoding="utf-8")
rep_lim = run(limit=1)
assert rep_lim.examined == 1, f"--limit caps items examined: {rep_lim}"
assert rep_lim.processed == 1, f"the one examined new file is ingested: {rep_lim}"
# a follow-up run with no limit picks up the remaining new file (and skips the already-done one).
rep_rest = run()
assert blobstore.latest_version("lim-a") is not None and blobstore.latest_version("lim-b") is not None, \
    "both limited files are eventually ingested"


# §8. cost is always 0 (no LLM), so --max-usd is inert: any positive budget never fires (cost stays 0
#     and never reaches the budget), and tap ingests every file regardless of the budget. (A literal $0
#     budget is the one value the driver's `cost >= budget` gate trips on the first item — that is the
#     driver's general semantics, not a tap quirk; tap's point is that a REAL guard never gates it.)
(proj / "costless.jsonl").write_text('{"cwd":"/c"}\n', encoding="utf-8")
rep_cost = run(max_usd=0.01)
assert rep_cost.cost_usd == 0.0, f"tap cost is always 0: {rep_cost}"
assert rep_cost.stopped_on_budget is False, f"--max-usd is inert for tap (never stops): {rep_cost}"
assert rep_cost.processed >= 1, f"a positive budget did not block ingestion: {rep_cost}"
assert blobstore.latest_version("costless") is not None, "a positive budget did not block ingestion"


# §9. discover() scoping: --exclude drops matching project dirs, and skip_self drops the dir ratchet's
#     OWN completer runs land in (cwd=data_root) so tap never eats its own generated `claude -p` runs
#     (ADR-0025). Tested directly on discover() (a pure enumerator) over a purpose-built datastore.
ds2 = Path(tempfile.mkdtemp(prefix="ratchet-test-ds2-"))
fake_root = Path("/home/sulin/.local/share/ratchet")          # a data_root to encode + skip
self_proj = ds2 / tap.encode_project(fake_root)               # -home-sulin--local-share-ratchet
self_child = ds2 / (tap.encode_project(fake_root) + "-runs")  # a nested-cwd child of data_root
real_proj = ds2 / "-home-sulin-ratchet"                       # a REAL interactive project (keep!)
tmp_proj = ds2 / "-tmp-ratchet-itest-data"                    # a test-fixture project (--exclude)
for d in (self_proj, self_child, real_proj, tmp_proj):
    d.mkdir()
    (d / "s.jsonl").write_text('{"cwd":"/x"}\n', encoding="utf-8")

# encode_project reproduces Claude Code's cwd→dir-name convention exactly.
assert tap.encode_project(fake_root) == "-home-sulin--local-share-ratchet", \
    f"encode_project must match the CC convention: {tap.encode_project(fake_root)!r}"

names = lambda paths: {p.parent.name for p in paths}

# no skip → everything is discoverable.
assert names(tap.discover(ds2)) == {self_proj.name, self_child.name, real_proj.name, tmp_proj.name}, \
    "bare discover sees all project dirs"

# skip_self drops the data-dir project AND its nested-cwd children, but never a real interactive project.
got = names(tap.discover(ds2, skip_self=fake_root))
assert self_proj.name not in got, "skip_self drops ratchet's own data-dir project"
assert self_child.name not in got, "skip_self drops nested-cwd children of data_root"
assert real_proj.name in got, "skip_self must NOT drop a real interactive project that merely shares a prefix word"
assert tmp_proj.name in got, "skip_self only targets the data-dir project, not test fixtures"

# --exclude is an independent substring filter (here: the -tmp- test fixtures).
got_excl = names(tap.discover(ds2, exclude=("-tmp-",), skip_self=fake_root))
assert tmp_proj.name not in got_excl and real_proj.name in got_excl, \
    "--exclude drops only the matching (fixture) dirs"

# --include-self (skip_self=None) lifts the auto-skip — an escape hatch, not a hard wall.
assert self_proj.name in names(tap.discover(ds2, skip_self=None)), \
    "skip_self=None (--include-self) re-includes the data-dir project"


print("OK — tap on block.py: error isolation, idempotent re-tap, touched-once, marker shape, "
      "dry-run, source-id scoping, limit, inert budget, self+exclude discover scoping")
