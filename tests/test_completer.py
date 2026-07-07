"""completer tests: the pure hygiene helpers + the CLI binding's RESILIENCE. The headline is the
two-budget retry — a transient THROTTLE (rate limit / overload) is waited out on its own larger,
exponential, capped budget so a backfill rides out a 429 window; any OTHER error fails fast on the
small budget; a success short-circuits. `subprocess.run` + `time.sleep` are faked so the suite is
offline and instant (no CLI, no real waiting). Run: `python tests/test_completer.py`."""
import json
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ratchet import completer  # noqa: E402
from ratchet.completer import Completion, CompleterError  # noqa: E402


# --- pure helpers (no CLI) -----------------------------------------------------------------------

assert completer.clean_score("0.5") == 0.5 and completer.clean_score("x", 0.3) == 0.3
assert completer.clean_score(float("nan"), 0.2) == 0.2 and completer.clean_score(2.0) == 1.0
assert completer.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}, "strips the ```json fence"
assert completer.parse_json_object("not json at all") is None
assert completer.cost_of(Completion("", "haiku", input_tokens=1_000_000, output_tokens=0)) == 1.0, \
    "price-table fallback when the binding reports no cost"
assert completer.cost_of(Completion("", "haiku", cost_usd=0.02)) == 0.02, "reported cost wins"
assert completer._is_rate_limited({"result": "Server is temporarily limiting requests"}, "")
assert completer._is_rate_limited({}, "Error: 429 Too Many Requests")
assert not completer._is_rate_limited({"subtype": "error_max_turns"}, "")
print("OK — pure helpers: scores clamped, JSON fence stripped, cost report-or-table, throttle detected.")


# --- a scripted fake for subprocess.run + a sleep capture ----------------------------------------

class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

def ok_env(result="hi", cost=0.001):
    return json.dumps({"result": result, "total_cost_usd": cost,
                       "usage": {"input_tokens": 5, "output_tokens": 2}})

def err_env(subtype, result=""):
    return json.dumps({"is_error": True, "subtype": subtype, "result": result})

THROTTLE = FakeProc(err_env("rate_limit_error", "Server is temporarily limiting requests"), returncode=1)
REAL_ERR = FakeProc(err_env("error_max_turns"), returncode=1)


class Scripted:
    """Stand in for the subprocess module: `run` returns the next queued FakeProc (or raises a queued
    exception). `TimeoutExpired` is preserved so the binding's except clause still resolves."""
    TimeoutExpired = subprocess.TimeoutExpired
    def __init__(self, seq):
        self.seq, self.calls, self.argvs = list(seq), 0, []
    def run(self, *a, **k):
        self.argvs.append(list(a[0]))        # capture the FULL argv (flag audit for R1)
        item = self.seq[self.calls]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def install(seq):
    """Swap the binding's module-global subprocess + time; return (waits, scripted) and a restore fn."""
    waits = []
    real_sub, real_time = completer.subprocess, completer.time
    completer.subprocess = Scripted(seq)
    completer.time = types.SimpleNamespace(sleep=lambda s: waits.append(s))
    def restore():
        completer.subprocess, completer.time = real_sub, real_time
    return waits, completer.subprocess, restore


# --- 1. success short-circuits: no retry, no wait ------------------------------------------------

waits, sub, restore = install([FakeProc(ok_env())])
try:
    c = make = completer.make_cli_completer("haiku", cwd=Path("/tmp"))("sys", "usr")
    assert c.text == "hi" and c.cost_usd == 0.001 and c.input_tokens == 5
    assert sub.calls == 1 and waits == [], "one call, no backoff on success"
finally:
    restore()
print("OK — success: returns the parsed envelope on the first call, no retry/sleep.")


# --- 2. a THROTTLE is waited out on the exponential budget, then succeeds -------------------------

waits, sub, restore = install([THROTTLE, THROTTLE, FakeProc(ok_env("recovered"))])
try:
    c = completer.make_cli_completer("haiku", backoff=2.0, cwd=Path("/tmp"))("sys", "usr")
    assert c.text == "recovered", "rides out the throttle then returns the real result"
    assert sub.calls == 3, "two throttled attempts + one success"
    assert waits == [2.0, 4.0], "exponential ride-out waits (2*2^0, 2*2^1), NOT the linear fast-fail"
finally:
    restore()
print("OK — throttle: waited out on the larger exponential budget, recovers without dropping the item.")


# --- 3. a REAL error fails fast on the small budget (does NOT sit and wait) -----------------------

waits, sub, restore = install([REAL_ERR, REAL_ERR, REAL_ERR])
try:
    try:
        completer.make_cli_completer("haiku", retries=2, backoff=2.0, cwd=Path("/tmp"))("sys", "usr")
        assert False, "a persistent real error must raise"
    except CompleterError as e:
        assert "error_max_turns" in str(e)
    assert sub.calls == 3, "initial + 2 retries"
    assert waits == [2.0, 4.0], "linear fast-fail backoff (2*1, 2*2), small budget"
finally:
    restore()
print("OK — real error: fast-fails on the small linear budget, raises CompleterError (stages isolate it).")


# --- 4. the throttle budget is bounded + the wait is CAPPED; on_throttle fires each wait ----------

seen = []
waits, sub, restore = install([THROTTLE] * 4)   # never recovers
try:
    try:
        completer.make_cli_completer("haiku", backoff=10.0, rate_limit_retries=3,
                                     rate_limit_max_wait=25.0, on_throttle=seen.append,
                                     cwd=Path("/tmp"))("sys", "usr")
        assert False, "an unending throttle must eventually raise (bounded budget)"
    except CompleterError:
        pass
    assert sub.calls == 4, "initial + 3 throttle retries, then give up"
    assert waits == [10.0, 20.0, 25.0], "exponential then CAPPED at rate_limit_max_wait (10, 20, 25<40)"
    assert seen == [10.0, 20.0, 25.0], "on_throttle fires once per wait (the Progress-surfacing hook)"
finally:
    restore()
print("OK — throttle budget: bounded retries, wait capped at rate_limit_max_wait, on_throttle hook fires.")


# --- 5. the slim TOOL-LESS flags ride every call (R1/ADR-0035) -----------------------------------

waits, sub, restore = install([FakeProc(ok_env())])
try:
    completer.make_cli_completer("haiku", cwd=Path("/tmp"))("sys", "usr")
    argv = sub.argvs[0]
    for flag in ("--disallowedTools", "--strict-mcp-config", "--disable-slash-commands", "--settings"):
        assert flag in argv, f"{flag} is on every call (payload trim / no side effects)"
    assert argv[argv.index("--disallowedTools") + 1] == "*", "disallow ALL tools (removes the schemas)"
    assert argv[argv.index("--settings") + 1] == '{"disableAllHooks":true}', "hooks off via --settings JSON"
    assert "--allowedTools" in argv, "the empty allowlist is KEPT as the proven max-turns guard"
    assert argv[argv.index("--max-turns") + 1] == "1", "still one turn (a pure function)"
finally:
    restore()
print("OK — R1: the slim tool-less flags (disallow-all + strict-mcp + no-slash + hooks-off) ride every call.")


# --- 6. cache fields parse from the envelope usage (R0/ADR-0035) ---------------------------------

def cache_env(read, create):
    return json.dumps({"result": "hi", "total_cost_usd": 0.001,
                       "usage": {"input_tokens": 40, "output_tokens": 2,
                                 "cache_read_input_tokens": read, "cache_creation_input_tokens": create}})

waits, sub, restore = install([FakeProc(cache_env(4096, 128))])
try:
    c = completer.make_cli_completer("haiku", cwd=Path("/tmp"))("sys", "usr")
    assert c.cache_read_tokens == 4096 and c.cache_creation_tokens == 128, "cache fields parsed from usage"
    assert c.input_tokens == 40, "input_tokens (the NON-cached input) still parsed alongside"
finally:
    restore()

waits, sub, restore = install([FakeProc(ok_env())])                 # usage without any cache fields
try:
    c = completer.make_cli_completer("haiku", cwd=Path("/tmp"))("sys", "usr")
    assert c.cache_read_tokens == 0 and c.cache_creation_tokens == 0, "absent cache fields default to 0"
finally:
    restore()
print("OK — R0: cache_read/cache_creation parse from the envelope usage (0 when the binding omits them).")


# --- 7. the completer is a single oneshot callable — no session chain (ADR-0036 removed warm-base) ---
# glean extracts BLIND (ADR-0036), so there is no digest to amortize and no warm-base fork; the CLI
# completer is exactly the one text→JSON call, with none of the `.warm`/`.fork` session plumbing.
wc = completer.make_cli_completer("haiku", cwd=Path("/tmp"))
assert not hasattr(wc, "warm") and not hasattr(wc, "fork"), "no session-chain seam — the completer is oneshot only"
waits, sub, restore = install([FakeProc(ok_env("events"))])
try:
    comp = wc("SYS", "CHUNK")
    assert comp.text == "events" and sub.calls == 1, "one oneshot call, no resume/fork"
    assert "--resume" not in sub.argvs[0] and "--session-id" not in sub.argvs[0], "no session flags on a oneshot call"
finally:
    restore()
print("OK — the completer is a single oneshot callable (no warm/fork session chain, ADR-0036).")

print("\nall completer tests passed.")
