"""block tests: the shared driver is exercised with FAKE blocks (no stage, no network), so the
substrate's cross-cutting guarantees are pinned independent of any stage. The load-bearing checks:

  PER-ITEM COMMIT — a mid-run exception leaves every EARLIER item committed WITH its marker; the
    failing item is retried next run; the run continues past it (crash/kill-safety, ADR-0009 fix #2).
  IDEMPOTENCY — a re-run skips items with a marker for (key, *params); bumping a param re-does them;
    the marker is a real `processed` decision blob (0007), folded back by done_index.
  BUDGET/LIMIT — --max-usd stops cleanly (committed-so-far persists); --limit caps items EXAMINED
    before the done-skip; --dry-run lists without processing.
  PROGRESS DECOUPLING — the driver speaks only the Progress PROTOCOL (start(total,todo,already) /
    tick(key, outcome, …) / stop), never constructs one, never reads stage knobs off the block. A
    probe satisfying that protocol records the stream; the driver computes total/todo/already itself.
  DREAM'S finalize EXCEPTION — commits_per_item=False writes NO per-item marker; finalize sees the
    block's own pending state (no driver-passed list) and does the commit itself.

Run: `python tests/test_block.py`."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-block-")

from ratchet import block, blobstore, config  # noqa: E402

config.ensure_layout()
ROOT = config.data_root()


# --- a Progress-like probe: the driver speaks the PROTOCOL (start/tick/stop), so a plain object that ---
# implements it captures the exact stream the driver emits. tick's SINGLE `outcome` (one of
# done/skipped/errored/dry_run) is recorded verbatim — proving the driver passes the one discriminator
# per call site, not a soup of bool flags. start() records the driver-computed (total, todo, already).

class ProbeProgress:
    def __init__(self):
        self.started = None                         # (total, todo, already)
        self.backlog = None                         # the full pre-limit un-done count start() received
        self.ticks: list[dict] = []                 # one per landed/skipped/dry/errored item, in order
        self.stopped = False

    def start(self, *, total, todo, already, backlog=0):
        self.started = (total, todo, already)
        self.backlog = backlog

    def tick(self, key, outcome, *, outputs=0, cost=0.0):
        self.ticks.append({"key": key, "outcome": outcome, "outputs": outputs, "cost": cost})

    def stop(self):
        self.stopped = True


# --- a minimal fake block: items are plain strings, "process" ingests one raw blob per item ----------
# Each item commits a real blob (so we can assert it persists across a crash) and returns (1, cost).
# `key(item) == item` makes the done-set trivially inspectable; `params` lets us flip the done-key.

class FakeBlock:
    """A driver harness. `boom_on` names items whose process() RAISES (uncaught-seam isolation);
    `cost` is charged per processed item (drives the budget gate). Records every key it processed,
    so the test reads the driver's behavior, not a stage's."""
    name = "fake"
    commits_per_item = True

    def __init__(self, items, *, version="v1", boom_on=(), cost=0.0):
        self._items = list(items)
        self.params = (("version", version),)
        self.boom_on = set(boom_on)
        self.cost = cost
        self.processed_keys: list[str] = []

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
    priority = block.no_priority                       # default 0.0 → stable sort preserves order
    age = block.no_age                                 # default 0.0 → Aging inert (the seam's second signal)


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
blk = FakeBlock(["a", "BOOM", "c"], boom_on=["BOOM"], cost=0.0)
probe = ProbeProgress()
rep = block.run(blk, root=r1, progress=probe)

assert rep.stage == "fake"
assert rep.examined == 3, f"all three examined: {rep}"
assert rep.processed == 2 and rep.errored == 1, f"a,c done; BOOM isolated: {rep}"
assert rep.outputs == 2, f"two output blobs: {rep}"
assert rep.pending == 1, f"the errored item is the remaining backlog (no marker → still un-done): {rep}"
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

# the driver drove the Progress PROTOCOL: start() got the driver-computed counts, tick() fired once per
# item with the SINGLE outcome (a→done, BOOM→errored, c→done), stop() ran. The driver constructed nothing.
assert probe.started == (3, 3, 0), f"driver computed total/todo/already and passed them to start: {probe.started}"
assert probe.backlog == 3, f"start() got the full pre-limit un-done backlog: {probe.backlog}"
assert [(t["key"], t["outcome"]) for t in probe.ticks] == [("a", "done"), ("BOOM", "errored"), ("c", "done")]
assert probe.ticks[0]["outputs"] == 1, "a done tick carries its outputs"
assert probe.ticks[1]["outputs"] == 0, "an errored tick carries no outputs"
assert probe.stopped, "the driver stopped the progress after the loop"

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
assert rep_a.pending == 0, f"a full drain leaves nothing pending: {rep_a}"

b_v1_again = FakeBlock(["x", "y", "z"], version="v1")
probe_idem = ProbeProgress()
rep_b = block.run(b_v1_again, root=r2, progress=probe_idem)
assert rep_b.processed == 0 and rep_b.skipped == 3, f"re-run skips all (same param): {rep_b}"
assert b_v1_again.processed_keys == [], "no item re-processed on an unchanged re-run"
# a fully-done re-run: start sees 0 todo, every tick is the single "skipped" outcome.
assert probe_idem.started == (3, 0, 3), f"all already-done: 0 todo, 3 already: {probe_idem.started}"
assert probe_idem.backlog == 0 and rep_b.pending == 0, f"nothing un-done → no backlog: {probe_idem.backlog}, {rep_b}"
assert [t["outcome"] for t in probe_idem.ticks] == ["skipped", "skipped", "skipped"], "every item ticked skipped"

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
assert rep_bud.pending == 2, f"budget stop leaves the un-processed remainder pending (i4,i5): {rep_bud}"
assert b_budget.processed_keys == ["i1", "i2", "i3"], "committed-so-far is exactly the first three"
# the stop is CLEAN: the three that landed are durable (blob + marker); the rest never ran.
assert ("i3", "v1") in block.done_index("fake", r3), "i3 (the last before stop) is marked"
assert ("i4", "v1") not in block.done_index("fake", r3), "i4 never processed → no marker"
# resuming finishes the rest (the budget stop left a resumable state, fix #2).
b_resume = FakeBlock(["i1", "i2", "i3", "i4", "i5"], cost=0.10)
rep_res = block.run(b_resume, root=r3, progress=None)
assert rep_res.skipped == 3 and rep_res.processed == 2, f"resume does the remaining two: {rep_res}"
assert rep_res.pending == 0, f"resume drained the backlog to zero: {rep_res}"
print("OK §3 — --max-usd: clean stop, committed-so-far persists, resumable")


# === 4. --limit caps items EXAMINED (before the done-skip), --dry-run lists without processing ========
r4 = fresh_root("limit")
b_lim = FakeBlock(["p", "q", "r", "s", "t"], cost=0.0)
probe_lim = ProbeProgress()
rep_lim = block.run(b_lim, root=r4, limit=2, progress=probe_lim)
assert rep_lim.examined == 2 and rep_lim.processed == 2, f"--limit caps examination: {rep_lim}"
assert b_lim.processed_keys == ["p", "q"], "exactly the first two were processed"
# --limit feeds the bar's TOTAL: the driver enumerated, capped to 2, and that 2 is what start sees.
assert probe_lim.started == (2, 2, 0), f"--limit caps the progress total too: {probe_lim.started}"
# amortization visibility: the bar's total is this tick's slice (2), but backlog is the FULL un-done set
# (5) and pending is what survives the tick (5-2=3) — so a capped run advertises the work it deferred.
assert probe_lim.backlog == 5, f"start() saw the full pre-limit backlog, not the capped slice: {probe_lim.backlog}"
assert rep_lim.pending == 3, f"--limit 2 over 5 un-done leaves 3 pending for the next tick: {rep_lim}"

# --limit caps EXAMINED, not processed: re-running a done corpus with --limit still examines `limit`
# items and stops (it does not scan past them hunting for undone work). p,q are done → both skip.
b_lim2 = FakeBlock(["p", "q", "r", "s", "t"], cost=0.0)
rep_lim2 = block.run(b_lim2, root=r4, limit=2, progress=None)
assert rep_lim2.examined == 2 and rep_lim2.skipped == 2 and rep_lim2.processed == 0, \
    f"--limit examines the first two (both done) and stops: {rep_lim2}"

# --dry-run: list-only, no process, no marker, no blob.
r4b = fresh_root("dryrun")
b_dry = FakeBlock(["m", "n"], cost=0.5)
probe_dry = ProbeProgress()
rep_dry = block.run(b_dry, root=r4b, dry_run=True, progress=probe_dry)
assert rep_dry.would_process == 2 and rep_dry.processed == 0, f"dry-run lists only: {rep_dry}"
assert rep_dry.cost_usd == 0.0, "dry-run spends nothing"
assert b_dry.processed_keys == [], "dry-run never called process"
assert not blobstore.has(blobstore.blob_hash("payload::m"), r4b), "dry-run ingested no blob"
assert block.done_index("fake", r4b) == set(), "dry-run wrote no marker"
# each item ticked with the single "dry_run" outcome (not a done/skip).
assert [(t["key"], t["outcome"]) for t in probe_dry.ticks] == [("m", "dry_run"), ("n", "dry_run")], \
    "progress ticked each as the dry_run outcome"
print("OK §4 — --limit caps examination; --dry-run lists without processing/committing")


# === 5. --source-id scopes enumeration; --quiet (progress=None) suppresses output =====================
r5 = fresh_root("scope")
b_one = FakeBlock(["alpha", "beta", "gamma"], cost=0.0)
rep_one = block.run(b_one, root=r5, source_id="beta", progress=None)
assert rep_one.examined == 1 and rep_one.processed == 1, f"--source-id processes just that one: {rep_one}"
assert b_one.processed_keys == ["beta"], "only the named source was enumerated"
assert block.done_index("fake", r5) == {("beta", "v1")}, "only beta marked"
# progress=None is the --quiet path: the driver runs the whole loop without ever touching a Progress.
b_quiet = FakeBlock(["solo"], cost=0.0)
rep_quiet = block.run(b_quiet, root=fresh_root("quiet"), progress=None)
assert rep_quiet.processed == 1, "the run completes with no Progress injected (quiet path)"
print("OK §5 — --source-id scopes items(); --quiet passes progress=None (the driver never builds Progress)")


# === 6. dream's finalize exception: commits_per_item=False → NO per-item marker; finalize commits ======
# Mirrors dream: process() records on the INSTANCE (no durable write, returns (0, cost)); the driver
# writes no marker and skips none of the budget accounting; finalize reads the block's OWN recorded
# state (the driver passes finalize NO list now, #6) and does the real work.

class FinalizeBlock:
    """A commits_per_item=False harness (dream's shape). process records the item + cost on the instance
    (no blob, no marker — returns 0 outputs); finalize reads that instance state (NOT a driver-passed
    list) and is where commits happen. Proves the driver skips the per-item marker yet still gates budget
    on process()'s cost, and that finalize takes only (root, run_id)."""
    name = "dreamlike"
    commits_per_item = False

    def __init__(self, items, *, cost=0.0):
        self._items = list(items)
        self.params = (("prompt_version", "d1"), ("model", "fake"))
        self.cost = cost
        self.recorded: list[tuple[str, float]] = []   # the block's OWN per-item state (dream's _pending)
        self.finalized_keys: list[str] | None = None

    def items(self, root, *, source_id=None):
        yield from self._items

    def key(self, item):
        return item

    def process(self, item, *, root, run_id):
        self.recorded.append((item, self.cost))      # nothing durable yet; cost feeds the budget gate
        return 0, self.cost

    def finalize(self, *, root, run_id):
        # the driver hands finalize NO item list — the block reads its OWN recorded state. THIS is where
        # dream ingests + writes markers.
        self.finalized_keys = [k for k, _ in self.recorded]
        for k, cost in self.recorded:
            block.write_processed(self.name, k, self.params, n_outputs=1, cost_usd=cost,
                                  run_id=run_id, extra={"event_ids": [k]}, root=root)

    marker_extra = block.no_marker_extra
    priority = block.no_priority
    age = block.no_age                                 # default 0.0 → Aging inert (the seam's second signal)

r6 = fresh_root("finalize")
fb = FinalizeBlock(["c1", "c2", "c3"], cost=0.0)
probe_fin = ProbeProgress()
rep_fin = block.run(fb, root=r6, progress=probe_fin)

assert [k for k, _ in fb.recorded] == ["c1", "c2", "c3"], "process ran per item (synthesis happened)"
assert rep_fin.processed == 3 and rep_fin.outputs == 0, \
    f"process committed nothing durable per item (0 outputs): {rep_fin}"
# CRUCIAL: the driver wrote NO per-item marker mid-loop (commits_per_item=False) ...
# ... yet finalize, running after the loop, reads the block's recorded items and writes the markers itself.
assert fb.finalized_keys == ["c1", "c2", "c3"], "finalize acted on the block's own recorded state"
done6 = block.done_index("dreamlike", r6)
assert done6 == {("c1", "d1", "fake"), ("c2", "d1", "fake"), ("c3", "d1", "fake")}, \
    f"finalize wrote the markers (with the ordered two-param key): {done6}"
# progress still streamed per item even though commit is deferred (each a "done" outcome, 0 outputs).
assert [(t["key"], t["outcome"], t["outputs"]) for t in probe_fin.ticks] == \
    [("c1", "done", 0), ("c2", "done", 0), ("c3", "done", 0)], \
    "progress streamed per item even though commit is deferred to finalize"

# a finalize block also honors --dry-run: process AND finalize are both skipped (no commit at all).
r6b = fresh_root("finalize-dry")
fb_dry = FinalizeBlock(["c1", "c2"])
rep_fin_dry = block.run(fb_dry, root=r6b, dry_run=True, progress=None)
assert rep_fin_dry.would_process == 2 and fb_dry.recorded == [], "dry-run skips process()"
assert fb_dry.finalized_keys is None, "dry-run skips finalize too (no commit on a dry run)"
assert block.done_index("dreamlike", r6b) == set(), "dry-run on a finalize block wrote nothing"
print("OK §6 — commits_per_item=False: driver writes no per-item marker; finalize commits off the block's own state")


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


# === 9. Progress is a self-contained subsystem: it renders from start/tick/stop ALONE (smoke) =========
# The decoupling means Progress carries NO knowledge of the driver or any stage — a test drives it
# directly through its own protocol with a piped (non-TTY) stream and reads the idempotent per-item log
# + the startup line. It must render all three line shapes (done, errored) without raising. The bar
# itself is TTY-only; piped, each landed item is one self-contained line (its own key·outputs·cost).
import io
buf = io.StringIO()                                 # not a TTY → the piped, idempotent-line path
prog = block.Progress("glean", cap=0.50, params={"prompt_version": "glean/2", "model": "haiku"},
                      out_noun="events", stream=buf)
prog.start(total=812, todo=47, already=765)         # the driver-computed counts arrive in start(), not __init__
prog.tick("a1b2c3d4e5f6", "done", outputs=3, cost=0.21)
prog.tick("skipthis0000", "skipped")                # a skip is a bare counter — NO per-item line
prog.tick("would00dry00", "dry_run")                # a dry-run is a bare counter — NO per-item line
prog.tick("0badf00d1234", "errored")
prog.stop()
lines = [ln for ln in buf.getvalue().splitlines() if ln]
# the startup line carries the stage, total, the todo/already split, the cap, and the params
assert lines[0].startswith("glean: 812 items · 47 to do · 765 done"), lines[0]
assert "cap" in lines[0] and "$0.50" in lines[0] and "prompt_version=glean/2" in lines[0], lines[0]
# the done item logged its own self-contained line with the out_noun and its OWN outputs+cost
assert lines[1] == "  glean a1b2c3d4e5f6 · 3 events · $0.2100", lines[1]
# the errored item logged an errored line; the skipped/dry-run items logged NOTHING (they are counters)
assert lines[2] == "  glean 0badf00d1234 · errored", lines[2]
assert len(lines) == 3, f"skip + dry_run logged no per-item line (counters only): {lines}"
# the aggregate counters tracked the outcomes (the bar would read these on a TTY)
assert prog.done == 1 and prog.skipped == 1 and prog.errored == 1 and prog.outputs == 3
assert abs(prog.cost - 0.21) < 1e-9, prog.cost
print("OK §9 — Progress renders from its own start/tick/stop protocol alone (decoupled from the driver)")


# === 10. priority() makes enumeration a PRIORITY QUEUE: highest-value first, --limit takes the top ===
# ADR-0010 §8's one composable knob. The driver stably sorts items DESCENDING by priority(item) BEFORE
# the --limit slice, so the highest-priority work runs first and the cap takes the top-`limit`. The
# default `no_priority` (0.0 everywhere) leaves enumeration order untouched (stable sort) — that is why
# §1-9 above, all on the default, are byte-for-byte unchanged.

class PriorityBlock(FakeBlock):
    """A FakeBlock that scores each item by a supplied map — so the test reads the driver's sort, not a
    stage's. Items enumerate in their given order; priority() re-orders them descending before --limit."""
    name = "prio"

    def __init__(self, items, scores, **kw):
        super().__init__(items, **kw)
        self._scores = scores

    def priority(self, item):
        return self._scores[item]

r10 = fresh_root("priority")
# enumeration order [lo, hi, mid]; priorities hi=9 > mid=5 > lo=1 → process order must be [hi, mid, lo].
pb = PriorityBlock(["lo", "hi", "mid"], {"lo": 1.0, "hi": 9.0, "mid": 5.0})
probe_p = ProbeProgress()
rep_p = block.run(pb, root=r10, progress=probe_p)
assert pb.processed_keys == ["hi", "mid", "lo"], \
    f"the driver processed highest-priority first (descending sort): {pb.processed_keys}"
assert [t["key"] for t in probe_p.ticks] == ["hi", "mid", "lo"], "progress streamed in priority order"

# --limit takes the TOP-`limit` by priority (the sort precedes the --limit slice): only hi+mid run.
r10b = fresh_root("priority-limit")
pb2 = PriorityBlock(["lo", "hi", "mid"], {"lo": 1.0, "hi": 9.0, "mid": 5.0})
rep_p2 = block.run(pb2, root=r10b, limit=2, progress=None)
assert rep_p2.examined == 2 and pb2.processed_keys == ["hi", "mid"], \
    f"--limit takes the top-2 by priority, not the first two enumerated: {pb2.processed_keys}"

# the DEFAULT no_priority is a stable no-op: equal scores preserve enumeration order exactly.
r10c = fresh_root("priority-default")
pd = FakeBlock(["a", "b", "c", "d"])               # all default 0.0 priority
block.run(pd, root=r10c, progress=None)
assert pd.processed_keys == ["a", "b", "c", "d"], \
    f"default priority (0.0) + stable sort preserves enumeration order: {pd.processed_keys}"
print("OK §10 — priority(): descending priority queue, --limit takes the top slice, default is a stable no-op")


print("\nOK — block driver: per-item commit/crash-safety, idempotency, budget/limit/dry-run, priority "
      "queue, DECOUPLED progress (start/tick(outcome)/stop protocol), dream's finalize exception, protocol structure")
