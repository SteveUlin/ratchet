"""priority POLICY tests: the driver's ordering seam (ADR-0011), exercised with FAKE blocks (no stage,
no LLM, fully deterministic). Priority splits into the per-stage SIGNAL (`block.priority(item)->float`)
and the driver-owned POLICY (`PriorityStrategy.order`); these pin the POLICY half:

  GOLDEN-FILE ORDER — a committed `tests/golden/priority_order.json` fixes the expected process() order
    per strategy over a fabricated items+scores set; the driver runs once per strategy and the recorded
    order must equal the golden (expected-vs-actual, the required methodology). A tie (d1/d2) pins
    Greedy's STABLE tie-break; greedy vs arrival proving the seam re-orders without any stage change.
  BYTE-IDENTICAL DEFAULT — the `greedy` default reproduces the pre-strategy driver exactly: a
    representative existing ordering (the test_block §10 lo/hi/mid scenario) is unchanged whether run
    with no `priority` kwarg, an explicit `Greedy()`, or `priority_strategy("greedy")`.
  SELECTION TWO WAYS — by code (`run(..., priority=Greedy())`) and by name (`priority_strategy("...")`,
    the `--priority` registry) reach the same order; an unknown name raises.

Run: `python tests/test_priority.py`."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-priority-")

from ratchet import block, config  # noqa: E402

config.ensure_layout()

GOLDEN = Path(__file__).resolve().parent / "golden" / "priority_order.json"


def fresh_root(prefix):
    """An isolated data root per run — `commits_per_item=True` writes a `processed` marker per item, so a
    re-run on a shared root would SKIP and never reach process(); each strategy run needs a clean done-set."""
    r = Path(tempfile.mkdtemp(prefix=f"ratchet-test-priority-{prefix}-"))
    config.ensure_layout(r)
    return r


# --- a fake block that RECORDS the order items reach process(), scored by a supplied map. process()
# commits nothing durable (returns 0 outputs, 0 cost) — the test reads ONLY the driver's ordering, not a
# stage's work. priority(item) is the SIGNAL; the strategy under test is the POLICY over it.

class RecordingBlock:
    name = "prio_test"
    commits_per_item = True

    def __init__(self, items, scores):
        self._items = list(items)
        self._scores = scores
        self.params = (("version", "v1"),)
        self.processed_keys: list[str] = []

    def items(self, root, *, source_id=None):
        yield from self._items                       # enumeration order is the given list order

    def key(self, item):
        return item

    def priority(self, item):
        return self._scores[item]

    def process(self, item, *, root, run_id):
        self.processed_keys.append(item)             # the driver's ordering, recorded verbatim
        return 0, 0.0

    finalize = block.no_finalize
    marker_extra = block.no_marker_extra
    age = block.no_age                               # the SECOND signal: 0.0 (fresh) → Aging is inert here


class AgingRecordingBlock(RecordingBlock):
    """RecordingBlock + a controlled per-item AGE (in DAYS) — the second signal the `Aging` policy reads.
    Everything else (enumeration, the score signal, the order recording) is the base block's, so the SAME
    driver path exercises Aging end to end: `effective = score + λ·age` decides the order process() sees."""
    def __init__(self, items, scores, ages):
        super().__init__(items, scores)
        self._ages = ages

    def age(self, item):
        return self._ages[item]


def _diff(expected, actual):
    """A legible per-position diff for a failed order assertion (the golden's whole point is a readable
    mismatch). Marks the first divergence so the cause is obvious at a glance."""
    lines = [f"  expected: {expected}", f"  actual:   {actual}"]
    n = max(len(expected), len(actual))
    for i in range(n):
        e = expected[i] if i < len(expected) else "∅"
        a = actual[i] if i < len(actual) else "∅"
        mark = "  ok" if e == a else "  <-- FIRST DIVERGENCE"
        lines.append(f"    [{i}] expected {e!r:>6}  actual {a!r:>6}{mark}")
        if e != a:
            break
    return "\n".join(lines)


def run_order(strategy, items, scores):
    """Drive the recording block over a fresh root with `strategy`, return the recorded process() order."""
    blk = RecordingBlock(items, scores)
    block.run(blk, root=fresh_root("run"), priority=strategy, progress=None)
    return blk.processed_keys


def run_order_aged(strategy, items, scores, ages):
    """Drive an AgingRecordingBlock (scores AND ages) through the real driver, return the process() order —
    so the Aging assertions exercise `priority.order(items, block.priority, block.age)` end to end, not the
    strategy in isolation."""
    blk = AgingRecordingBlock(items, scores, ages)
    block.run(blk, root=fresh_root("aged"), priority=strategy, progress=None)
    return blk.processed_keys


# === 1. golden-file order per strategy: expected (committed) vs actual (driven) =======================
assert GOLDEN.exists(), f"missing golden file — commit it: {GOLDEN}"
golden = json.loads(GOLDEN.read_text())
items, scores, expected = golden["items"], golden["scores"], golden["expected"]

for name, want in expected.items():
    # selection BY NAME — the `--priority` registry path the CLIs use.
    got = run_order(block.priority_strategy(name), items, scores)
    assert got == want, f"\n{name}: order mismatch\n{_diff(want, got)}"
print(f"OK §1 — golden order matches for every strategy {sorted(expected)} (expected == actual)")


# === 2. selection two ways agree: by-code instance == by-name registry == driver default (greedy) =====
by_code = run_order(block.Greedy(), items, scores)
by_name = run_order(block.priority_strategy("greedy"), items, scores)
assert by_code == by_name == expected["greedy"], f"code/name greedy diverge: {by_code} / {by_name}"

# the DRIVER DEFAULT (no `priority` kwarg at all) must equal explicit greedy — byte-identical guarantee.
blk_default = RecordingBlock(items, scores)
block.run(blk_default, root=fresh_root("default"), progress=None)   # no priority= → default Greedy()
assert blk_default.processed_keys == expected["greedy"], \
    f"the run() default is not greedy: {blk_default.processed_keys}"
print("OK §2 — selection by code == by name == the run() default (all greedy, identical order)")


# === 3. BYTE-IDENTICAL DEFAULT guard: a representative existing ordering is unchanged ==================
# The exact test_block §10 scenario: enumeration [lo, hi, mid], scores hi=9 > mid=5 > lo=1. Under the
# default (and explicit Greedy) the driver must still process [hi, mid, lo] — proving the strategy
# refactor changed NO existing behavior. Arrival ignores the score → enumeration order [lo, hi, mid].
rep_items = ["lo", "hi", "mid"]
rep_scores = {"lo": 1.0, "hi": 9.0, "mid": 5.0}

blk_rep = RecordingBlock(rep_items, rep_scores)
block.run(blk_rep, root=fresh_root("rep-default"), progress=None)    # default path
assert blk_rep.processed_keys == ["hi", "mid", "lo"], \
    f"default no longer reproduces the greedy priority queue: {blk_rep.processed_keys}"
assert run_order(block.Greedy(), rep_items, rep_scores) == ["hi", "mid", "lo"], "explicit Greedy diverged"
assert run_order(block.Arrival(), rep_items, rep_scores) == ["lo", "hi", "mid"], \
    "Arrival must ignore score and keep enumeration order"

# the uniform-0.0 SIGNAL (the no-opt-in stage) is a stable no-op under Greedy: enumeration order survives.
flat = RecordingBlock(["a", "b", "c", "d"], {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0})
block.run(flat, root=fresh_root("flat"), progress=None)
assert flat.processed_keys == ["a", "b", "c", "d"], \
    f"a uniform 0.0 signal must preserve enumeration order under Greedy: {flat.processed_keys}"
print("OK §3 — byte-identical default: greedy reproduces the priority queue; a flat signal is a stable no-op")


# === 4. the seam's shape: strategies satisfy the Protocol; an unknown name raises =====================
assert isinstance(block.Greedy(), block.PriorityStrategy), "Greedy structurally satisfies PriorityStrategy"
assert isinstance(block.Arrival(), block.PriorityStrategy), "Arrival structurally satisfies PriorityStrategy"
# the registry hands out FRESH instances (load-bearing for a future seeded/stateful strategy).
assert block.priority_strategy("greedy") is not block.priority_strategy("greedy"), \
    "the registry must construct a fresh instance per call (not a shared singleton)"
try:
    block.priority_strategy("does-not-exist")
except ValueError as e:
    assert "unknown priority strategy" in str(e), e
else:
    raise AssertionError("an unknown strategy name must raise ValueError")
print("OK §4 — PriorityStrategy is a structural Protocol; the registry is fresh-per-call; unknown name raises")


# === 5. AGING: the anti-starvation policy — effective = score + λ·age (ADR-0021) ======================
# The whole point: a fresh HIGH-score item and an old LOW-score one. Greedy ALWAYS runs the high scorer
# first (it ignores age) — so under a persistent --limit/--max-usd the low scorer never surfaces (starves).
# Aging lifts the old one once λ·age covers the score gap, bounding its worst-case latency. We pin the
# CROSSOVER precisely off the live LAMBDA so the test survives retuning: with a score gap G, the old item
# overtakes at age = G/λ — assert it stays sunk just BELOW and surfaces just ABOVE.
LAMBDA = block.AGING_LAMBDA
GAP = 2.0                                            # fresh_hi score 3.0 − old_lo score 1.0
ag_items = ["fresh_hi", "old_lo"]                    # enumeration order: high scorer first
ag_scores = {"fresh_hi": 3.0, "old_lo": 1.0}
crossover = GAP / LAMBDA                             # the age (days) at which old_lo's λ·age == the score gap

# Greedy IGNORES age — high scorer first no matter how old the low one is (the starvation Aging fixes).
assert run_order_aged(block.Greedy(), ag_items, ag_scores, {"fresh_hi": 0.0, "old_lo": 10 * crossover}) \
    == ["fresh_hi", "old_lo"], "Greedy must order by score alone, ignoring age"

# Aging, age JUST BELOW the crossover → old_lo still sunk (eff 1+λ(cross−1) < 3.0): greedy-like order.
below = run_order_aged(block.Aging(), ag_items, ag_scores,
                       {"fresh_hi": 0.0, "old_lo": crossover - 1.0})
assert below == ["fresh_hi", "old_lo"], f"below the crossover the old item must stay sunk: {below}"

# Aging, age JUST ABOVE the crossover → old_lo's λ·age overtakes the score gap → it surfaces FIRST.
above = run_order_aged(block.Aging(), ag_items, ag_scores,
                       {"fresh_hi": 0.0, "old_lo": crossover + 1.0})
assert above == ["old_lo", "fresh_hi"], \
    f"once λ·age overtakes the score gap the old low-score item must surface first: {above}"
print(f"OK §5a — Aging crossover: old low-score item (score 1.0) overtakes a fresh high-score one "
      f"(score 3.0) at age {crossover:.0f}d (λ={LAMBDA}); sunk at {crossover-1:.0f}d, surfaces at {crossover+1:.0f}d")

# All-equal ages == Greedy ordering: age adds a UNIFORM constant to every effective score → no reorder.
eq_ages = {"fresh_hi": 123.0, "old_lo": 123.0}
assert run_order_aged(block.Aging(), ag_items, ag_scores, eq_ages) \
    == run_order(block.Greedy(), ag_items, ag_scores) == ["fresh_hi", "old_lo"], \
    "Aging with all-equal ages must reproduce Greedy ordering (a uniform age constant cannot reorder)"

# No age signal (the no_age default → 0.0 everywhere) makes Aging INERT == Greedy, through the real driver.
inert = RecordingBlock(rep_items, rep_scores)        # base block: age = no_age (0.0)
block.run(inert, root=fresh_root("aging-inert"), priority=block.Aging(), progress=None)
assert inert.processed_keys == ["hi", "mid", "lo"], \
    f"Aging on a stage with no age signal must collapse to Greedy: {inert.processed_keys}"
print("OK §5b — equal ages (and the no_age default) collapse Aging to Greedy: a uniform age cannot reorder")

# DETERMINISTIC + ORDER-STABLE: equal EFFECTIVE priority (score+λ·age) keeps enumeration order, repeatably.
# t1 (score 2.0, age 0) and t2 (score 1.0, age 1/λ) tie at effective 2.0; t1 enumerated first must stay first.
stab_items = ["t1", "t2", "t3"]
stab_scores = {"t1": 2.0, "t2": 1.0, "t3": 0.0}
stab_ages = {"t1": 0.0, "t2": 1.0 / LAMBDA, "t3": 0.0}   # t1,t2 effective 2.0; t3 effective 0.0
first = run_order_aged(block.Aging(), stab_items, stab_scores, stab_ages)
again = run_order_aged(block.Aging(), stab_items, stab_scores, stab_ages)
assert first == again == ["t1", "t2", "t3"], \
    f"Aging must be deterministic and stable on an effective tie (enumeration order preserved): {first} / {again}"
print("OK §5c — Aging is deterministic and order-stable: an effective-priority tie keeps enumeration order")


# === 6. SELECTION: --priority aging by code and by the CLI registry reach the same order ===============
by_code_ag = run_order_aged(block.Aging(), ag_items, ag_scores,
                            {"fresh_hi": 0.0, "old_lo": crossover + 1.0})
by_name_ag = run_order_aged(block.priority_strategy("aging"), ag_items, ag_scores,
                            {"fresh_hi": 0.0, "old_lo": crossover + 1.0})
assert by_code_ag == by_name_ag == ["old_lo", "fresh_hi"], \
    f"aging by code != by name: {by_code_ag} / {by_name_ag}"
assert isinstance(block.priority_strategy("aging"), block.PriorityStrategy), "Aging satisfies the Protocol"
# the CLI surface: `aging` is in every stage's `--priority` choices (choices=sorted(PRIORITY_STRATEGIES)).
assert "aging" in block.PRIORITY_STRATEGIES and "aging" in sorted(block.PRIORITY_STRATEGIES), \
    "aging must be registered so every stage's --priority offers it"

# Exercise a real stage CLI end-to-end: `dream --dry-run --priority aging` parses the choice, resolves the
# strategy, and runs the aging-aware dry-run preview (over an empty store → 0 events, no LLM, no crash).
import contextlib, io  # noqa: E402
from ratchet import dream  # noqa: E402
with contextlib.redirect_stdout(io.StringIO()):
    dream.main(["--dry-run", "--priority", "aging"])   # argparse choices accept it; the dry-run age path runs
print("OK §6 — --priority aging selectable by code == by name; registered for every CLI; dream --dry-run runs it")


print("\nOK — priority policy: golden order per strategy, byte-identical greedy default, "
      "code/name/default selection agree, structural Protocol seam, Aging anti-starvation crossover")
