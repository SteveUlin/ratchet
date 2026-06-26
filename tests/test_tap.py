"""tap regression tests (from the adversarial review): per-file error isolation, idempotent
re-tap, and a touched file re-read at most once (not forever).
Run: `python tests/test_tap.py` (throwaway dirs)."""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-data-")

from ratchet import blobstore, config, tap  # noqa: E402

config.ensure_layout()
ds = Path(tempfile.mkdtemp(prefix="ratchet-test-ds-"))
proj = ds / "proj-x"
proj.mkdir()
(proj / "good.jsonl").write_text('{"cwd":"/p","gitBranch":"main"}\n', encoding="utf-8")
(proj / "bad.jsonl").write_text("whatever\n", encoding="utf-8")
_orig_read = tap.read_origin


def run():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tap.tap(ds)
    return buf.getvalue()


# 1. error isolation: one file raising OSError must NOT abort the run
def boom(p):
    if p.name == "bad.jsonl":
        raise OSError("simulated unreadable")
    return _orig_read(p)

tap.read_origin = boom
out = run()
tap.read_origin = _orig_read
assert "errored 1" in out, f"bad file counted, run not aborted:\n{out}"
assert blobstore.latest_version("good") is not None, "good copied despite a broken sibling"

# settle: bad.jsonl is actually readable; a normal run copies it
run()

# 2. idempotent re-tap copies nothing new
out2 = run()
assert "copied 0" in out2, f"idempotent re-tap:\n{out2}"

# 3. touch (mtime bump, content identical) is re-read at most ONCE, then cheap-skipped
reads = {"n": 0}

def counting(p):
    reads["n"] += 1
    return _orig_read(p)

os.utime(proj / "good.jsonl", None)  # bump mtime; content unchanged
tap.read_origin = counting
reads["n"] = 0; run()                 # cheap tier misses -> reads good once, updates cursor
first = reads["n"]
reads["n"] = 0; run()                 # cursor now current -> no read
second = reads["n"]
tap.read_origin = _orig_read
assert first >= 1 and second == 0, f"touched file re-read once then skipped (got {first}, {second})"

print("OK — tap error isolation, idempotent re-tap, touched file not re-read forever")
