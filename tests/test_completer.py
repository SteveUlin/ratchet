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
        self.seq, self.calls = list(seq), 0
    def run(self, *a, **k):
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

print("\nall completer tests passed.")
