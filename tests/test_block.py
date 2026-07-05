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
  PARALLEL (opt-in) — `run(parallel=N)` pools process() calls for a `parallel_safe = True` block:
    the end-state is IDENTICAL to serial (completion order invisible), the budget gate's overshoot is
    bounded (cap + up to `parallel` calls' cost — the leash), a block that hasn't opted in clamps to
    serial with a stderr note, and a raising item stays isolated mid-pool.
  BREAKER — K CONSECUTIVE failures abort the tick (a systemic wall, not K flaky items): serial trips
    at exactly K with earlier successes marked and the remainder pending; any success or skip resets
    the count (scattered failures never trip); the pool stops dispatch and drains in-flight (bounded
    extra collections); `breaker_errors=0` disables (the escape hatch). Loud: one stderr line + the
    Report's `breaker_tripped`.
  LEASHED BAR — the TTY bar's fraction tracks what ENDS the tick: spend/cap under --max-usd (labeled
    `$so-far/$cap`, reaching 100% exactly when the run stops on budget), processed/limit under
    --limit, the CLOSER leash when both are set; unleashed rendering pinned byte-identical to the
    historical item walk; skips never drive a leashed bar (their own `· N skip` counter).

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


# === 11. parallel: a pooled run lands the IDENTICAL end-state serial does (order is invisible) ========
# The pooled lane changes WHEN a call runs, never WHAT lands: same blob set, same markers, same Report
# counts. Content-addressing makes completion order invisible, so a serial root and a parallel root must
# be indistinguishable. The block sleeps unevenly so the pool genuinely scrambles completion order.
import time
from contextlib import redirect_stderr


class ParallelBlock(FakeBlock):
    """A FakeBlock that OPTS IN (`parallel_safe = True`) and sleeps unevenly in process(), so a pooled
    run finishes out of dispatch order — proving end-state identity is completion-order-free.
    processed_keys keeps recording (list.append is atomic); order-sensitive asserts use sets."""
    name = "par"
    parallel_safe = True

    def __init__(self, items, *, naps=None, **kw):
        super().__init__(items, **kw)
        self.naps = naps or {}

    def process(self, item, *, root, run_id):
        time.sleep(self.naps.get(item, 0.0))
        return super().process(item, root=root, run_id=run_id)


ITEMS11 = ["s1", "s2", "s3", "s4", "s5", "s6"]
NAPS11 = {"s1": 0.05, "s2": 0.0, "s3": 0.03, "s4": 0.01, "s5": 0.04, "s6": 0.0}

r11s = fresh_root("par-serial")
b11s = ParallelBlock(ITEMS11, naps=NAPS11)
rep11s = block.run(b11s, root=r11s, progress=None)              # parallel defaults to 1: the serial lane

r11p = fresh_root("par-pool")
b11p = ParallelBlock(ITEMS11, naps=NAPS11)
probe_par = ProbeProgress()
rep11p = block.run(b11p, root=r11p, parallel=3, progress=probe_par)

for f in ("examined", "processed", "skipped", "errored", "outputs", "pending"):
    assert getattr(rep11s, f) == getattr(rep11p, f), \
        f"Report.{f} diverged: serial {getattr(rep11s, f)} vs parallel {getattr(rep11p, f)}"
assert abs(rep11s.cost_usd - rep11p.cost_usd) < 1e-9, "identical spend"
for it in ITEMS11:                                              # identical blob set, both roots
    assert blobstore.has(blobstore.blob_hash(f"payload::{it}"), r11s), f"serial blob for {it}"
    assert blobstore.has(blobstore.blob_hash(f"payload::{it}"), r11p), f"parallel blob for {it}"
assert block.done_index("par", r11p) == block.done_index("par", r11s) == \
    {(it, "v1") for it in ITEMS11}, "identical marker (done) set"
assert sorted(b11p.processed_keys) == sorted(ITEMS11), "each item processed exactly once (order scrambles)"
assert probe_par.started == (6, 6, 0) and probe_par.stopped, "the pool still drives start/stop"
assert sorted(t["key"] for t in probe_par.ticks) == sorted(ITEMS11) and \
    all(t["outcome"] == "done" for t in probe_par.ticks), "one done tick per item, order-free"
# the markers written at collection are real 0007 markers: a pooled re-run skips everything.
b11r = ParallelBlock(ITEMS11)
rep11r = block.run(b11r, root=r11p, parallel=3, progress=None)
assert rep11r.skipped == 6 and rep11r.processed == 0, f"pooled re-run skips via the done-set: {rep11r}"
print("OK §11 — parallel: pooled end-state (blobs, markers, Report) identical to serial; re-run skips")


# === 12. parallel budget: the gate checks COLLECTED spend at dispatch — a bounded overshoot ===========
# Per-call cost $1, cap $2, parallel=3, 8 items. The serial gate would land 2 (it trips at $2 collected);
# the pool may add up to (parallel-1)=2 calls already in flight when the gate trips. Assert the
# DOCUMENTED bound, never an exact count — the budget is a leash, not a contract.
r12 = fresh_root("par-budget")
b12 = ParallelBlock([f"b{i}" for i in range(8)], cost=1.0)
rep12 = block.run(b12, root=r12, max_usd=2.0, parallel=3, progress=None)
assert rep12.stopped_on_budget, f"budget stop flagged: {rep12}"
assert 2 <= rep12.processed <= 4, \
    f"bound: at least the serial gate's 2, at most 2 + (parallel-1) = 4 landed: {rep12}"
assert rep12.cost_usd <= 4.0 + 1e-9, f"spend bounded by cap + parallel calls' cost: {rep12}"
assert len(block.done_index("par", r12)) == rep12.processed, "everything that landed is marked (drained, not orphaned)"
assert rep12.pending == 8 - rep12.processed, f"the un-dispatched remainder is pending for the next tick: {rep12}"
print(f"OK §12 — parallel budget: clean stop, {rep12.processed} landed (documented bound 2..4), remainder pending")


# === 13. clamps: a parallel_safe=False block runs SERIAL under parallel=N; N caps at PARALLEL_MAX =====
# Opt-in is per Block: the default False protects stages whose serial order is load-bearing (resolve's
# read-your-writes fold). Both clamps are LOUD (one stderr line saying why) and the run still completes.
r13 = fresh_root("par-clamp")
b13 = FakeBlock(["a", "b", "c"])                    # FakeBlock does NOT declare parallel_safe → False
err13 = io.StringIO()
with redirect_stderr(err13):
    rep13 = block.run(b13, root=r13, parallel=3, progress=None)
assert rep13.processed == 3, f"clamped run still completes: {rep13}"
assert b13.processed_keys == ["a", "b", "c"], "serial lane: dispatch order preserved exactly"
assert "parallel" in err13.getvalue().lower() and "serial" in err13.getvalue().lower(), \
    f"the clamp says why on stderr: {err13.getvalue()!r}"

r13b = fresh_root("par-cap")                        # a request past PARALLEL_MAX clamps down, loudly
b13b = ParallelBlock(["x", "y"])
err13b = io.StringIO()
with redirect_stderr(err13b):
    rep13b = block.run(b13b, root=r13b, parallel=99, progress=None)
assert rep13b.processed == 2 and "clamp" in err13b.getvalue().lower(), \
    f"over-cap request clamped to PARALLEL_MAX with a note: {err13b.getvalue()!r}"
print("OK §13 — clamps: non-opted-in block runs serial (why on stderr); N caps at PARALLEL_MAX")


# === 14. parallel error isolation: one raising item doesn't kill the tick or its siblings =============
# (default breaker: 1 failure among 3 siblings never nears BREAKER_ERRORS — isolation, not a wall)
r14 = fresh_root("par-boom")
b14 = ParallelBlock(["g1", "BOOM", "g2", "g3"], boom_on=["BOOM"],
                    naps={"g1": 0.03, "g2": 0.02})
probe14 = ProbeProgress()
rep14 = block.run(b14, root=r14, parallel=3, progress=probe14)
assert rep14.processed == 3 and rep14.errored == 1, f"BOOM isolated mid-pool; siblings landed: {rep14}"
assert sorted(b14.processed_keys) == ["g1", "g2", "g3"], "every non-booming sibling committed"
assert block.done_index("par", r14) == {("g1", "v1"), ("g2", "v1"), ("g3", "v1")}, \
    "no marker for the failer — it retries next run, exactly the serial contract"
outcomes14 = {t["key"]: t["outcome"] for t in probe14.ticks}
assert outcomes14["BOOM"] == "errored" and all(outcomes14[k] == "done" for k in ("g1", "g2", "g3"))
assert rep14.pending == 1, f"the errored item stays pending: {rep14}"
print("OK §14 — parallel isolation: a mid-pool raise ticks errored, writes no marker, siblings commit")


# === 15. the breaker (serial): K CONSECUTIVE failures abort the tick — a wall, not K flaky items ======
# The failure mode this closes: the account's usage window goes up mid-run and every further LLM call
# burns fast-fail retries + backoff against a dead seam (1,300+ consecutive doomed calls, live). From
# item k onward EVERY process() raises; the breaker trips at exactly K, earlier successes stay marked,
# the aborted remainder (errored + never-reached) is simply pending next tick, and the trip is LOUD:
# `breaker_tripped` on the Report + one stderr line saying why.
r15 = fresh_root("breaker")
ok15 = ["w1", "w2"]
bad15 = [f"x{i}" for i in range(8)]                 # the wall: everything from here on fails
b15 = FakeBlock(ok15 + bad15 + ["z1", "z2", "z3"], boom_on=bad15)
err15 = io.StringIO()
with redirect_stderr(err15):
    rep15 = block.run(b15, root=r15, breaker_errors=5, progress=None)
assert rep15.breaker_tripped, f"the breaker tripped: {rep15}"
assert rep15.errored == 5, f"exactly K=5 consecutive failures, then STOP (no 6th doomed call): {rep15}"
assert rep15.examined == 7, f"2 successes + 5 failures examined; the tick ended there: {rep15}"
assert rep15.processed == 2 and b15.processed_keys == ["w1", "w2"], "earlier successes landed"
assert block.done_index("fake", r15) == {("w1", "v1"), ("w2", "v1")}, \
    "earlier successes are marked; no aborted item got a marker"
assert rep15.pending == 11, f"aborted items (5 errored + 6 never reached) are simply pending: {rep15}"
line15 = err15.getvalue().strip()
assert "5 consecutive errors" in line15 and "tripping the breaker" in line15 and \
    "11 items left pending" in line15 and "usage window" in line15, \
    f"the trip says why on stderr: {line15!r}"
# recovery is the normal pending path: once the wall lifts (nothing booms), a re-run drains the rest.
b15r = FakeBlock(ok15 + bad15 + ["z1", "z2", "z3"], boom_on=[])
rep15r = block.run(b15r, root=r15, progress=None)
assert rep15r.processed == 11 and rep15r.skipped == 2 and rep15r.pending == 0, \
    f"next tick retries every aborted item (no marker was written): {rep15r}"
print("OK §15 — breaker (serial): trips at exactly K, successes stay marked, remainder pending, loud")


# === 16. the breaker resets on success/skip (scattered failures never trip); 0 disables ===============
# The counter measures an UNBROKEN run — the wall signature — so any success or skip resets it: a
# corpus with scattered bad items is isolation's job (§1/§7), never the breaker's.
r16 = fresh_root("breaker-reset")
b16 = FakeBlock(["f1", "s1", "f2", "s2", "f3", "s3"], boom_on=["f1", "f2", "f3"])
rep16 = block.run(b16, root=r16, breaker_errors=2, progress=None)
assert not rep16.breaker_tripped, f"alternating fail/success never reaches 2 consecutive: {rep16}"
assert rep16.errored == 3 and rep16.processed == 3, f"every item attempted, failures isolated: {rep16}"

r16b = fresh_root("breaker-skip")                   # a SKIP resets too (it breaks the unbroken run)
block.run(FakeBlock(["mid"]), root=r16b, progress=None)              # mark "mid" done
b16b = FakeBlock(["f1", "mid", "f2"], boom_on=["f1", "f2"])
rep16b = block.run(b16b, root=r16b, breaker_errors=2, progress=None)
assert not rep16b.breaker_tripped and rep16b.skipped == 1 and rep16b.errored == 2, \
    f"fail, skip, fail — the skip reset the counter, no trip: {rep16b}"

r16c = fresh_root("breaker-off")                    # breaker_errors=0 — the escape hatch: never trips
all_bad = [f"d{i}" for i in range(block.BREAKER_ERRORS + 2)]         # more than the default K
b16c = FakeBlock(all_bad, boom_on=all_bad)
rep16c = block.run(b16c, root=r16c, breaker_errors=0, progress=None)
assert not rep16c.breaker_tripped and rep16c.errored == len(all_bad) and \
    rep16c.examined == len(all_bad), f"0 disables: every item attempted despite the unbroken run: {rep16c}"
print("OK §16 — breaker resets on success AND skip (scattered failures never trip); 0 disables it")


# === 17. the breaker (parallel twin): trip stops DISPATCH; in-flight drains (the budget-stop shape) ====
# Successes dispatch first (no nap) and land before the napping failures collect, so the streak counts
# only failures. Errored is a documented BOUND, not an exact count: K collected at the trip, plus up to
# (workers-1) already in flight that the drain lands — same leash shape as §12's budget overshoot.
r17 = fresh_root("par-breaker")
ok17 = ["g1", "g2", "g3"]
bad17 = [f"h{i}" for i in range(20)]
b17 = ParallelBlock(ok17 + bad17, boom_on=bad17, naps={k: 0.01 for k in bad17})
err17 = io.StringIO()
with redirect_stderr(err17):
    rep17 = block.run(b17, root=r17, parallel=3, breaker_errors=5, progress=None)
assert rep17.breaker_tripped, f"the pool tripped the breaker: {rep17}"
assert rep17.processed == 3 and sorted(b17.processed_keys) == ok17, "every success landed (drained, not orphaned)"
assert 5 <= rep17.errored <= 7, \
    f"bound: K=5 at the trip, plus up to (workers-1)=2 in flight when it tripped: {rep17}"
assert rep17.examined < len(ok17 + bad17), f"dispatch STOPPED — the remainder was never attempted: {rep17}"
assert block.done_index("par", r17) == {(k, "v1") for k in ok17}, \
    "successes marked; no failed/aborted item got a marker (all retry next tick)"
assert rep17.pending == 20, f"everything but the 3 successes is pending (errored included): {rep17}"
assert "tripping the breaker" in err17.getvalue() and "20 items left pending" in err17.getvalue(), \
    f"same loud stderr line as serial, with the FINAL pending count (post-drain): {err17.getvalue()!r}"
print(f"OK §17 — breaker (parallel): dispatch stops, in-flight drains ({rep17.errored} errored, bound 5..7), loud")


# === 18. scores_report: the pending queue's value curve — pure string, exact math, no writes ==========
# The operator surface behind --scores: enumerate exactly as run() would (items() + done_index split),
# then render stats + an equal-width histogram + the two ends of the processing order. The checks pin
# the bin/count math byte-exactly, the two graceful degradations (all-equal → one closed row; empty
# queue → no div-by-zero), the aging twin (an old item's EFFECTIVE score climbs the order and a second
# histogram appears), and purity (a report writes nothing — no marker, no blob, no process call).

r18 = fresh_root("scores")
# scores [0,0,1,2,4,4,4] over 4 bins of width 1.0: [0,1)→2, [1,2)→1, [2,3)→1, [3,4] (closed)→3.
sb = PriorityBlock(["i1", "i2", "i3", "i4", "i5", "i6", "i7"],
                   {"i1": 0.0, "i2": 0.0, "i3": 1.0, "i4": 2.0, "i5": 4.0, "i6": 4.0, "i7": 4.0})
done_before = block.done_index("prio", r18)
rep18 = block.scores_report(sb, root=r18, buckets=4)
assert isinstance(rep18, str), "the report is a pure string"
lines18 = rep18.splitlines()
assert lines18[0] == "prio scores — 7 pending · 0 done · policy greedy", lines18[0]
assert lines18[1] == "score: min 0.000 · median 2.000 · mean 2.143 · max 4.000", lines18[1]
# the histogram rows, byte-exact: count math, the half-open bins, the CLOSED last bin (max lands
# inside), and the 1-#-per-item bar (peak 3 fits SCORES_BAR_WIDTH).
assert lines18[3] == "score histogram (4 equal-width bins over 7 pending):", lines18[3]
assert lines18[4] == "  [0.000, 1.000)     2  ##", lines18[4]
assert lines18[5] == "  [1.000, 2.000)     1  #", lines18[5]
assert lines18[6] == "  [2.000, 3.000)     1  #", lines18[6]
assert lines18[7] == "  [3.000, 4.000]     3  ###", lines18[7]
# the two ends of the greedy order (stable: score ties keep enumeration order): top-5 = the 4.0s then
# the 2.0; bottom = whatever the top didn't show, worst last.
assert "top 5 — the next tick buys these first:" in lines18, lines18
i_top = lines18.index("top 5 — the next tick buys these first:")
assert lines18[i_top + 1:i_top + 6] == ["  i5  score 4.000", "  i6  score 4.000", "  i7  score 4.000",
                                        "  i4  score 2.000", "  i3  score 1.000"], lines18[i_top + 1:i_top + 6]
assert lines18[i_top + 6] == "bottom 2 — waits longest:", lines18[i_top + 6]
assert lines18[i_top + 7:] == ["  i1  score 0.000", "  i2  score 0.000"], lines18[i_top + 7:]
# PURITY: rendering the report processed nothing and wrote nothing.
assert sb.processed_keys == [], "scores_report never calls process()"
assert block.done_index("prio", r18) == done_before, "scores_report writes no marker"

# the done-split: after a --limit 2 tick takes the top slice (i5,i6), the report sees 5 pending, 2 done.
block.run(PriorityBlock(["i1", "i2", "i3", "i4", "i5", "i6", "i7"],
                        {"i1": 0.0, "i2": 0.0, "i3": 1.0, "i4": 2.0, "i5": 4.0, "i6": 4.0, "i7": 4.0}),
          root=r18, limit=2, progress=None)
rep18b = block.scores_report(sb, root=r18, buckets=4)
assert rep18b.splitlines()[0] == "prio scores — 5 pending · 2 done · policy greedy", rep18b.splitlines()[0]
assert "score: min 0.000 · median 1.000 · mean 1.400 · max 4.000" in rep18b, rep18b

# DEGENERATE: all scores equal (FakeBlock's uniform 0.0) → ONE closed row, no div-by-zero.
r18c = fresh_root("scores-flat")
fb18 = FakeBlock(["a", "b", "c"])
rep18c = block.scores_report(fb18, root=r18c)
assert "fake scores — 3 pending · 0 done · policy greedy" in rep18c, rep18c
assert "  [0.000, 0.000]     3  ###" in rep18c, f"all-equal collapses to one closed bin: {rep18c}"

# DEGENERATE: empty queue → the header + one line, no stats, no histogram, no crash.
rep18d = block.scores_report(FakeBlock([]), root=fresh_root("scores-empty"))
assert rep18d.splitlines() == ["fake scores — 0 pending · 0 done · policy greedy",
                               "  queue empty — nothing pending under the current filters"], rep18d


class AgedScoresBlock(PriorityBlock):
    """A PriorityBlock exposing per-item AGE (the ADR-0021 seam), so the report's aging twin — the
    effective histogram + the eff/score/age item lines — is testable with controlled signals."""
    name = "agedscores"

    def __init__(self, items, scores, ages, **kw):
        super().__init__(items, scores, **kw)
        self._ages = ages

    def age(self, item):
        return self._ages[item]


# AGING: old (score 1.0, age 100d) → effective 1.0 + 0.05·100 = 6.0, overtaking fresh (2.0, age 0).
r18e = fresh_root("scores-aging")
ab = AgedScoresBlock(["fresh", "old", "mid"], {"fresh": 2.0, "old": 1.0, "mid": 1.5},
                     {"fresh": 0.0, "old": 100.0, "mid": 10.0})
rep18e = block.scores_report(ab, root=r18e, priority="aging", buckets=4)
assert "policy aging" in rep18e.splitlines()[0], rep18e.splitlines()[0]
assert "effective histogram (score + λ·age" in rep18e, "aging renders the SECOND histogram"
i_eff = [i for i, ln in enumerate(rep18e.splitlines()) if ln.startswith("effective histogram")][0]
eff_rows = rep18e.splitlines()[i_eff + 1:i_eff + 5]
# effective scores [2.0, 6.0, 2.0] over 4 bins of width 1.0 from 2.0: [2,3)→2, [5,6]→1.
assert eff_rows[0] == "  [2.000, 3.000)     2  ##", eff_rows
assert eff_rows[3] == "  [5.000, 6.000]     1  #", eff_rows
i_top18e = rep18e.splitlines().index("top 3 — the next tick buys these first:")
assert rep18e.splitlines()[i_top18e + 1] == "  old  eff 6.000 · score 1.000 · age 100.0d", \
    "the aged item climbed to the head — aging's effective column shifted it up"
# the SAME block under greedy: no effective histogram, and the raw score leads (old sinks).
rep18f = block.scores_report(ab, root=r18e, priority="greedy", buckets=4)
assert "effective histogram" not in rep18f, "greedy shows one histogram — aging's twin is policy-gated"
i_top18f = rep18f.splitlines().index("top 3 — the next tick buys these first:")
assert rep18f.splitlines()[i_top18f + 1] == "  fresh  score 2.000", rep18f.splitlines()[i_top18f + 1]
print("OK §18 — scores_report: pinned histogram math, all-equal + empty degrade, aging's effective "
      "twin re-orders, pure string (no writes)")


# === 19. the leashed bar: the fraction tracks what ENDS the tick (budget / limit / neither) ============
# The lie this closes: on a --max-usd tick the old bar was items-walked / all-enumerated, so a budget
# stop left it stranded at 2% — and its numerator (done+skip+err) disagreed with the `done/total`
# counter beside it. Now the bar tracks the leash that will end the tick: spend/cap under --max-usd
# (labeled `$so-far/$cap`, beside the bar), processed/limit under --limit (labeled `n/limit lim`), and
# when BOTH are set, whichever leash is CLOSER to tripping (max of the fractions — the run ends at the
# FIRST leash to trip). Unleashed rendering is pinned BYTE-IDENTICAL to the historical item walk. Skips
# never drive a leashed bar (they cost nothing against either leash) — they keep the `· N skip` counter.
# The bar is TTY-only, so a fake-TTY stream captures it; the test never start()s the animator thread
# (nondeterministic frames) — it sets the driver-computed total, drives tick(), and renders ONE frame.

class _TTY(io.StringIO):
    def isatty(self):
        return True


def _frame(prog):
    """Render one controlled frame (frame 0 — the animator never runs) and return its exact bytes."""
    prog.stream.seek(0)
    prog.stream.truncate(0)
    prog._draw_bar()
    return prog.stream.getvalue()


GRN, DIM, RST, CYN, YEL = block._GRN, block._DIM, block._RESET, block._CYN, block._YEL


def _bar19(filled):
    return GRN + "▰" * filled + DIM + "▱" * (24 - filled) + RST


def _leash_prog(**kw):
    """A captured Progress mid-tick: 812 enumerated, 205 skipped, 47 processed → 96 events, $3.33."""
    p = block.Progress("glean", out_noun="events", stream=_TTY(), **kw)
    p.total = 812                # the driver-computed count start() would carry (sans animator thread)
    for i in range(205):
        p.tick(f"s{i}", "skipped")
    for i in range(46):
        p.tick(f"d{i}", "done", outputs=2, cost=0.07)
    p.tick("d46", "done", outputs=4, cost=0.11)
    p.cost = 3.33                # exact float for the byte pin (the ticks accumulate ≈3.33 with FP noise)
    return p


# NEITHER leash — the historical item walk, pinned byte-identical: percent numerator is seen
# (done+skip+err = 252 → 31%) while the counter shows done/total (47/812); a full drain reaches 100%.
line19n = _frame(_leash_prog())
assert line19n == ("\r\x1b[2K" + f"{CYN}⠋{RST} glean {_bar19(7)} 31% · 47/812 · 96 events"
                   " · 205 skip · " + f"{YEL}$3.33{RST}"), repr(line19n)

# BUDGET (--max-usd 10) — the bar is spend/cap, labeled beside it; the items counter stays as-is; the
# trailing spend is gone (the label IS the spend); skips keep their own counter.
p19b = _leash_prog(cap=10.0)
line19b = _frame(p19b)
assert line19b == ("\r\x1b[2K" + f"{CYN}⠋{RST} glean {YEL}$3.33/$10.00{RST} {_bar19(8)} 33%"
                   " · 47/812 · 96 events · 205 skip"), repr(line19b)

# skips NEVER drive a leashed bar: 400 more done-skips move only the `· N skip` counter, nothing else.
for i in range(400):
    p19b.tick(f"s2{i}", "skipped")
assert _frame(p19b) == line19b.replace("205 skip", "605 skip"), \
    "400 more skips moved ONLY the skip counter — never the budget bar"

# the budget bar reaches 100% exactly when the run stops on budget — §3's tick stream (cost 0.10/item,
# cap 0.25): the gate lets item 3 through (spend 0.20 < 0.25 → 80%), then trips at 0.30 → clamp → 100%.
p19e = block.Progress("fake", cap=0.25, stream=_TTY())
p19e.total = 5
p19e.tick("i1", "done", outputs=1, cost=0.10)
p19e.tick("i2", "done", outputs=1, cost=0.10)
assert f"{YEL}$0.20/$0.25{RST} {_bar19(19)} 80%" in _frame(p19e), repr(_frame(p19e))
p19e.tick("i3", "done", outputs=1, cost=0.10)     # the call the gate let through overshoots the cap ...
assert f"{YEL}$0.30/$0.25{RST} {_bar19(24)} 100%" in _frame(p19e), \
    "... so the label shows the honest overshoot while the bar clamps at 100% (the tick IS over)"

# LIMIT only (--limit 50) — the bar is processed/limit, labeled `n/limit lim`; spend trails as before.
p19c = block.Progress("glean", limit=50, out_noun="events", stream=_TTY())
p19c.total = 50
for i in range(5):
    p19c.tick(f"s{i}", "skipped")
for i in range(12):
    p19c.tick(f"d{i}", "done", outputs=3, cost=0.10)
p19c.cost = 1.20
line19l = _frame(p19c)
assert line19l == ("\r\x1b[2K" + f"{CYN}⠋{RST} glean 12/50 lim {_bar19(6)} 24% · 12/50 · "
                   "36 events · 5 skip · " + f"{YEL}$1.20{RST}"), repr(line19l)

# BOTH leashes — precedence: whichever is CLOSER to tripping drives (max of the fractions), and the
# label names the driving leash. Same counters, only the spend moves the verdict.
p19d = block.Progress("glean", cap=10.0, limit=50, out_noun="events", stream=_TTY())
p19d.total = 50
p19d.done, p19d.outputs, p19d.cost = 12, 36, 3.33     # budget 33% > limit 24% → the budget drives
both_b = _frame(p19d)
assert f"{YEL}$3.33/$10.00{RST} {_bar19(8)} 33%" in both_b and " lim " not in both_b, repr(both_b)
p19d.cost = 1.00                                      # budget 10% < limit 24% → the limit drives
both_l = _frame(p19d)
assert f"12/50 lim {_bar19(6)} 24%" in both_l, repr(both_l)
assert both_l.endswith(f" · {YEL}$1.00{RST}/$10.00"), \
    "when the limit drives, the OTHER leash's spend/cap stays as the trailing counter"
print("OK §19 — leashed bar: budget=spend/cap (100% exactly at the budget stop), limit=processed/limit, "
      "both=closest leash drives, unleashed pinned byte-identical, skips never drive a leashed bar")


# === 20. ProxyReport forwards EVERY uniform Report field (the docstring's claim, enforced) =============
# Generic over dataclasses.fields(Report), so a field added to Report later FAILS here until the proxy
# forwards it — the anti-desync discipline the wrapper exists for.
import dataclasses  # noqa: E402

rep20 = block.Report(stage="probe", run_id="r-proxy", examined=7, processed=5, skipped=1, errored=1,
                     outputs=9, cost_usd=1.25, stopped_on_budget=True, breaker_tripped=True,
                     would_process=3, pending=2)
proxy20 = block.ProxyReport(rep20, None)
for f20 in dataclasses.fields(block.Report):
    assert getattr(proxy20, f20.name) == getattr(rep20, f20.name), \
        f"ProxyReport must forward Report.{f20.name} (it claims to forward every uniform field)"
print("OK §20 — ProxyReport forwards every uniform Report field (checked generically over the dataclass)")


print("\nOK — block driver: per-item commit/crash-safety, idempotency, budget/limit/dry-run, priority "
      "queue, DECOUPLED progress (start/tick(outcome)/stop protocol), dream's finalize exception, protocol "
      "structure, opt-in bounded parallelism (identical end-state, leashed budget, loud clamps), the "
      "consecutive-failure breaker (K unbroken failures abort the tick; success/skip resets; 0 disables), "
      "the read-only scores report (--scores: the pending queue's value curve), and the leashed bar "
      "(the fraction tracks what ends the tick: budget/limit/item-walk)")
