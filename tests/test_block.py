"""block tests: the shared driver is exercised with FAKE blocks (no stage, no network), so the
substrate's cross-cutting guarantees are pinned independent of any stage. The load-bearing checks:

  PER-ITEM COMMIT — a mid-run exception leaves every EARLIER item committed WITH its marker; the
    failing item is retried next run; the run continues past it (crash/kill-safety, ADR-0009 fix #2).
  IDEMPOTENCY — a re-run skips items with a marker for (key, *params); bumping a param re-does them;
    the marker is a real `processed` decision blob (0007), folded back by done_index.
  BUDGET/LIMIT — --max-usd stops cleanly (committed-so-far persists); --limit caps items EXAMINED
    before the done-skip; --dry-run lists without processing.
  PROGRESS — the callback fires once per item as it LANDS, with live Report counters.
  DREAM'S finalize EXCEPTION — commits_per_item=False writes NO per-item marker; finalize sees every
    committed item and does the commit itself.

Run: `python tests/test_block.py`."""
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-block-")

from ratchet import block, blobstore, config  # noqa: E402

config.ensure_layout()
ROOT = config.data_root()


# --- a minimal fake block: items are plain strings, "process" ingests one raw blob per item ----------
# Each item commits a real blob (so we can assert it persists across a crash) and returns (1, cost).
# `key(item) == item` makes the done-set trivially inspectable; `params` lets us flip the done-key.

class FakeBlock:
    """A driver harness. `boom_on` names items whose process() RAISES (uncaught-seam isolation);
    `cost` is charged per processed item (drives the budget gate). Records every key it processed and
    every progress call, so the test reads the driver's behavior, not a stage's."""
    name = "fake"
    commits_per_item = True

    def __init__(self, items, *, version="v1", boom_on=(), cost=0.0):
        self._items = list(items)
        self.params = (("version", version),)
        self.boom_on = set(boom_on)
        self.cost = cost
        self.processed_keys: list[str] = []
        self.finalized: list[block.Done] | None = None

    def items(self, root, *, source_id=None):
        # source_id filters the enumeration (the --source-id contract); None → all.
        for it in self._items:
            if source_id is None or it == source_id:
                yield it

    def key(self, item):
        return item

    def process(self, item, *, root, run_id):
        if item in self.boom_on:
            raise RuntimeError(f"boom on {item}")     # the injected seam failing — driver isolates it
        # ingest a real raw blob keyed on the item, so "this item's work persisted" is checkable.
        blobstore.ingest(f"payload::{item}", source_kind="testblock", source_id=item,
                         origin_ref={"stage": "fake"}, root=root)
        self.processed_keys.append(item)
        return 1, self.cost

    finalize = block.no_finalize
    marker_extra = block.no_marker_extra


def fresh_root(prefix):
    """An isolated data root per scenario — the done-set is global to a root, so scenarios that assert
    on markers must not share one."""
    r = Path(tempfile.mkdtemp(prefix=f"ratchet-test-block-{prefix}-"))
    config.ensure_layout(r)
    return r


# === 1. per-item commit + error isolation: a mid-run boom leaves earlier items DONE, run continues ===
# The heart of fix #2: processing [a, BOOM, c] commits a (blob + marker), isolates BOOM (no marker,
# counted errored), and STILL processes c. Re-running re-does ONLY BOOM (now made to succeed).

r1 = fresh_root("crash")
calls: list[tuple] = []
def rec_progress(report, item, key, n_out, cost, *, dry_run=False, errored=False):
    calls.append((key, n_out, errored, report.processed, report.errored))

blk = FakeBlock(["a", "BOOM", "c"], boom_on=["BOOM"], cost=0.0)
rep = block.run(blk, root=r1, progress=rec_progress)

assert rep.stage == "fake"
assert rep.examined == 3, f"all three examined: {rep}"
assert rep.processed == 2 and rep.errored == 1, f"a,c done; BOOM isolated: {rep}"
assert rep.outputs == 2, f"two output blobs: {rep}"
assert blk.processed_keys == ["a", "c"], "BOOM never reached the commit; run continued past it"

# earlier items' WORK persisted (the blob) AND their commit marker exists — that is per-item commit.
assert blobstore.has(blobstore.blob_hash("payload::a"), r1), "item a's blob committed"
assert blobstore.has(blobstore.blob_hash("payload::c"), r1), "item c's blob committed"
done = block.done_index("fake", r1)
assert ("a", "v1") in done and ("c", "v1") in done, f"markers for a,c: {done}"
assert ("BOOM", "v1") not in done, "the failing item got NO marker (so it retries next run)"
# the marker is a real 0007 processed decision blob, not a side ledger:
markers = list(blobstore.decisions_for("a", r1, verb="processed", stage="fake"))
assert len(markers) == 1 and markers[0]["n_outputs"] == 1, f"a's marker is a decision blob: {markers}"

# progress fired per item as it landed: a (ok), BOOM (errored), c (ok) — and saw LIVE counters.
assert [(k, err) for k, _, err, _, _ in calls] == [("a", False), ("BOOM", True), ("c", False)]
assert calls[0][3] == 1 and calls[2][3] == 2, "report.processed ticked up across the run (live)"
assert calls[1][4] == 1, "report.errored was already 1 when BOOM's progress fired"

# RECOVERY: re-run with BOOM no longer booming — only BOOM re-does (a,c skip via their markers).
blk2 = FakeBlock(["a", "BOOM", "c"], boom_on=[], cost=0.0)
rep2 = block.run(blk2, root=r1, progress=None)
assert rep2.skipped == 2 and rep2.processed == 1, f"a,c skip; BOOM finally done: {rep2}"
assert blk2.processed_keys == ["BOOM"], "recovery re-did exactly the previously-failed item"
assert ("BOOM", "v1") in block.done_index("fake", r1), "BOOM is now marked done"
print("OK §1 — per-item commit: mid-run boom isolates, earlier items persist+marked, failed item retried")


# === 2. idempotency: a clean re-run skips everything; a BUMPED param re-does everything ===============
r2 = fresh_root("idem")
b_v1 = FakeBlock(["x", "y", "z"], version="v1")
rep_a = block.run(b_v1, root=r2, progress=None)
assert rep_a.processed == 3 and rep_a.skipped == 0, f"first run does all: {rep_a}"

b_v1_again = FakeBlock(["x", "y", "z"], version="v1")
rep_b = block.run(b_v1_again, root=r2, progress=None)
assert rep_b.processed == 0 and rep_b.skipped == 3, f"re-run skips all (same param): {rep_b}"
assert b_v1_again.processed_keys == [], "no item re-processed on an unchanged re-run"

# bump the idempotency param → a new done-key → every item re-processes (the "re-do on logic change").
b_v2 = FakeBlock(["x", "y", "z"], version="v2")
rep_c = block.run(b_v2, root=r2, progress=None)
assert rep_c.processed == 3 and rep_c.skipped == 0, f"bumped param re-does all: {rep_c}"
# both param versions now have markers; done_index keys carry the version in the tuple.
done2 = block.done_index("fake", r2)
assert ("x", "v1") in done2 and ("x", "v2") in done2, f"both versions marked: {done2}"
print("OK §2 — idempotency: clean re-run skips; a bumped param flips the done-key and re-does")


# === 3. --max-usd stops cleanly; committed-so-far persists; the rest is untouched =====================
r3 = fresh_root("budget")
# cost 0.10/item, max 0.25: item1 (cost→0.10), item2 (cost→0.20), then item3's gate sees 0.20<0.25 so
# it processes (→0.30), item4's gate sees 0.30>=0.25 → STOP. So 3 processed, stopped before item4.
b_budget = FakeBlock(["i1", "i2", "i3", "i4", "i5"], cost=0.10)
rep_bud = block.run(b_budget, root=r3, max_usd=0.25, progress=None)
assert rep_bud.stopped_on_budget, f"budget stop flagged: {rep_bud}"
assert rep_bud.processed == 3, f"three landed before the gate tripped: {rep_bud}"
assert abs(rep_bud.cost_usd - 0.30) < 1e-9, f"cost accumulated: {rep_bud}"
assert b_budget.processed_keys == ["i1", "i2", "i3"], "committed-so-far is exactly the first three"
# the stop is CLEAN: the three that landed are durable (blob + marker); the rest never ran.
assert ("i3", "v1") in block.done_index("fake", r3), "i3 (the last before stop) is marked"
assert ("i4", "v1") not in block.done_index("fake", r3), "i4 never processed → no marker"
# resuming finishes the rest (the budget stop left a resumable state, fix #2).
b_resume = FakeBlock(["i1", "i2", "i3", "i4", "i5"], cost=0.10)
rep_res = block.run(b_resume, root=r3, progress=None)
assert rep_res.skipped == 3 and rep_res.processed == 2, f"resume does the remaining two: {rep_res}"
print("OK §3 — --max-usd: clean stop, committed-so-far persists, resumable")


# === 4. --limit caps items EXAMINED (before the done-skip), --dry-run lists without processing ========
r4 = fresh_root("limit")
b_lim = FakeBlock(["p", "q", "r", "s", "t"], cost=0.0)
rep_lim = block.run(b_lim, root=r4, limit=2, progress=None)
assert rep_lim.examined == 2 and rep_lim.processed == 2, f"--limit caps examination: {rep_lim}"
assert b_lim.processed_keys == ["p", "q"], "exactly the first two were processed"

# --limit caps EXAMINED, not processed: re-running a done corpus with --limit still examines `limit`
# items and stops (it does not scan past them hunting for undone work). p,q are done → both skip.
b_lim2 = FakeBlock(["p", "q", "r", "s", "t"], cost=0.0)
rep_lim2 = block.run(b_lim2, root=r4, limit=2, progress=None)
assert rep_lim2.examined == 2 and rep_lim2.skipped == 2 and rep_lim2.processed == 0, \
    f"--limit examines the first two (both done) and stops: {rep_lim2}"

# --dry-run: list-only, no process, no marker, no blob.
r4b = fresh_root("dryrun")
dry_calls: list[str] = []
def dry_progress(report, item, key, n_out, cost, *, dry_run=False, errored=False):
    dry_calls.append(("dry" if dry_run else "real", key))
b_dry = FakeBlock(["m", "n"], cost=0.5)
rep_dry = block.run(b_dry, root=r4b, dry_run=True, progress=dry_progress)
assert rep_dry.would_process == 2 and rep_dry.processed == 0, f"dry-run lists only: {rep_dry}"
assert rep_dry.cost_usd == 0.0, "dry-run spends nothing"
assert b_dry.processed_keys == [], "dry-run never called process"
assert not blobstore.has(blobstore.blob_hash("payload::m"), r4b), "dry-run ingested no blob"
assert block.done_index("fake", r4b) == set(), "dry-run wrote no marker"
assert dry_calls == [("dry", "m"), ("dry", "n")], "progress flagged each as dry"
print("OK §4 — --limit caps examination; --dry-run lists without processing/committing")


# === 5. --source-id scopes enumeration; --quiet (progress=None) suppresses output =====================
r5 = fresh_root("scope")
b_one = FakeBlock(["alpha", "beta", "gamma"], cost=0.0)
rep_one = block.run(b_one, root=r5, source_id="beta", progress=None)
assert rep_one.examined == 1 and rep_one.processed == 1, f"--source-id processes just that one: {rep_one}"
assert b_one.processed_keys == ["beta"], "only the named source was enumerated"
assert block.done_index("fake", r5) == {("beta", "v1")}, "only beta marked"
print("OK §5 — --source-id scopes items(); --quiet passes progress=None (no output)")


# === 6. dream's finalize exception: commits_per_item=False → NO per-item marker; finalize commits ======
# Mirrors dream: process() records (no durable write, returns (0, cost)); the driver writes no marker
# and skips none of the budget accounting; finalize sees EVERY committed item and does the real work.

class FinalizeBlock:
    """A commits_per_item=False harness (dream's shape). process records the item + cost (no blob, no
    marker — returns 0 outputs); finalize gets the whole committed list and is where commits would
    happen. Proves the driver skips the per-item marker yet still gates budget on process()'s cost."""
    name = "dreamlike"
    commits_per_item = False

    def __init__(self, items, *, cost=0.0):
        self._items = list(items)
        self.params = (("prompt_version", "d1"), ("model", "fake"))
        self.cost = cost
        self.recorded: list[str] = []
        self.finalized_keys: list[str] | None = None

    def items(self, root, *, source_id=None):
        yield from self._items

    def key(self, item):
        return item

    def process(self, item, *, root, run_id):
        self.recorded.append(item)
        return 0, self.cost                      # nothing durable yet; cost feeds the budget gate

    def finalize(self, processed, *, root, run_id):
        # the driver handed us every committed Done; THIS is where dream ingests + writes markers.
        self.finalized_keys = [d.key for d in processed]
        for d in processed:
            block.write_processed(self.name, d.key, self.params, n_outputs=1, cost_usd=d.cost_usd,
                                  run_id=run_id, extra={"event_ids": [d.key]}, root=root)

    marker_extra = block.no_marker_extra

r6 = fresh_root("finalize")
fb = FinalizeBlock(["c1", "c2", "c3"], cost=0.0)
fin_calls: list[str] = []
def fin_progress(report, item, key, n_out, cost, *, dry_run=False, errored=False):
    fin_calls.append(key)
rep_fin = block.run(fb, root=r6, progress=fin_progress)

assert fb.recorded == ["c1", "c2", "c3"], "process ran per item (synthesis happened)"
assert rep_fin.processed == 3 and rep_fin.outputs == 0, \
    f"process committed nothing durable per item (0 outputs): {rep_fin}"
# CRUCIAL: the driver wrote NO per-item marker mid-loop (commits_per_item=False) ...
# ... yet finalize, running after the loop, sees every committed item and writes the markers itself.
assert fb.finalized_keys == ["c1", "c2", "c3"], "finalize received the full committed list"
done6 = block.done_index("dreamlike", r6)
assert done6 == {("c1", "d1", "fake"), ("c2", "d1", "fake"), ("c3", "d1", "fake")}, \
    f"finalize wrote the markers (with the ordered two-param key): {done6}"
assert fin_calls == ["c1", "c2", "c3"], "progress still streamed per item even though commit is deferred"

# a finalize block also honors --dry-run: process AND finalize are both skipped (no commit at all).
r6b = fresh_root("finalize-dry")
fb_dry = FinalizeBlock(["c1", "c2"])
rep_fin_dry = block.run(fb_dry, root=r6b, dry_run=True, progress=None)
assert rep_fin_dry.would_process == 2 and fb_dry.recorded == [], "dry-run skips process()"
assert fb_dry.finalized_keys is None, "dry-run skips finalize too (no commit on a dry run)"
assert block.done_index("dreamlike", r6b) == set(), "dry-run on a finalize block wrote nothing"
print("OK §6 — commits_per_item=False: driver writes no per-item marker; finalize commits the run")


# === 7. error isolation when EVERY item booms: run completes, nothing marked, all retryable ===========
r7 = fresh_root("allboom")
b_allboom = FakeBlock(["e1", "e2"], boom_on=["e1", "e2"])
rep_ab = block.run(b_allboom, root=r7, progress=None)
assert rep_ab.errored == 2 and rep_ab.processed == 0, f"every item isolated, run still completed: {rep_ab}"
assert block.done_index("fake", r7) == set(), "no markers when nothing succeeded → all retry next run"
print("OK §7 — total failure: every item isolated, run completes, nothing marked done (all retried)")


# === 8. the Block protocol is structural: a plain object with the members IS a Block ==================
assert isinstance(FakeBlock(["a"]), block.Block), "a structural Block (no subclassing) satisfies the Protocol"
assert isinstance(FinalizeBlock(["a"]), block.Block), "the finalize variant satisfies it too"
print("OK §8 — Block is a structural Protocol (stages are plain objects, no inheritance)")


# === 9. the default progress printer is stage-agnostic: it reads only Report + the per-item tuple =====
# (smoke: it must not raise on any of the three line shapes — normal, dry, errored.)
import io
import contextlib
buf = io.StringIO()
demo = block.Report(stage="glean", run_id="r", examined=812, processed=47, skipped=0, outputs=3, cost_usd=0.21)
with contextlib.redirect_stdout(buf):
    block._default_progress(demo, "item", "a1b2c3d4e5f6", 3, 0.21)
    block._default_progress(demo, "item", "deadbeefcafe", 0, 0.0, dry_run=True)
    block._default_progress(demo, "item", "0badf00d1234", 0, 0.0, errored=True)
lines = buf.getvalue().splitlines()
assert lines[0].startswith("glean  812 examined · 47 done · 0 skip · 3 out · $0.21"), lines[0]
assert lines[1] == "would glean deadbeefcafe", lines[1]
assert lines[2] == "  ! glean 0badf00d1234 errored", lines[2]
print("OK §9 — default progress printer renders all three line shapes from Report alone")


print("\nOK — block driver: per-item commit/crash-safety, idempotency, budget/limit/dry-run, "
      "progress streaming, dream's finalize exception, protocol structure")
