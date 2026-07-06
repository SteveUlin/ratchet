"""subject tests: the per-event repo+files scope key (dream-v3 §2.1/§3.0). Fabricate sessions with
KNOWN write-tool geometry, weave them (so the write lines are weave's real rendered forms —
`→ Edit: /path`, NotebookEdit's JSON-arg line), locate evidence spans as byte offsets into the
cleaned blob (glean's exact representation), and assert: narrowing (a span near one Edit keeps just
that file), the whole-session-union fallback (a span far from every write, or an invalid span), the
empty key of a no-edit/no-cwd session (`is_empty`), repo naming via `repo_label` (cwd basename),
the batch `cache`, and the `subject_overlap` math (W_FILE/W_REPO reused; tools fixed at 0;
empty∩empty = 0).

Run: `python tests/test_subject.py` (throwaway dir)."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-subject-")

from ratchet import blobstore, concepts, config, subject, weave  # noqa: E402

R = config.ensure_layout()


# --- synthetic transcripts: one record per content block, like Claude Code writes ----------------

def rec(uuid, parent, typ, **kw):
    r = {"type": typ, "uuid": uuid, "parentUuid": parent}
    r.update(kw)
    return r

def umsg(text):
    return {"role": "user", "content": text}

def amsg(mid, *blocks):
    return {"role": "assistant", "id": mid, "content": list(blocks)}

def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def add_session(records, *, sid, origin):
    raw_h, _ = blobstore.ingest("\n".join(json.dumps(r) for r in records) + "\n",
                                source_kind="transcript", source_id=sid, origin_ref=origin, root=R)
    ch, _, doc = weave.materialize(raw_h, root=R)
    return ch, doc.text

def bspan(cleaned_text, needle):
    """The byte span of `needle` in the cleaned blob — evidence as glean stores it: byte offsets
    into the UTF-8 bytes, resolving as get(cleaned_hash)[start:end]."""
    data, nd = cleaned_text.encode("utf-8"), needle.encode("utf-8")
    start = data.find(nd)
    assert start != -1, f"sentinel not rendered: {needle!r}"
    return (start, start + len(nd))


# Session 1: an Edit of alpha.py, >SPAN_WINDOW bytes of padding either side of a far sentinel,
# then the lesson + an Edit of beta.py. Padding stays under weave's 4000+1000+40 truncation limit.
PAD1 = "alpha-side padding words " * 196          # 4900 chars, survives untruncated
PAD2 = "beta-side padding words " * 204           # 4896 chars
FAR = "SENTINEL: a remark far from every write"
LESSON = "SENTINEL: beta needs frobnication before flush"
S1 = [
    rec("u0", None, "user", message=umsg("tighten the widget")),
    rec("a1", "u0", "assistant", message=amsg("A1", tool_use("t1", "Edit", file_path="/w/alpha.py",
                                                             old_string="x", new_string="y"))),
    rec("u1", "a1", "user", message=umsg(PAD1 + " " + FAR)),
    rec("u2", "u1", "user", message=umsg(PAD2)),
    rec("a2", "u2", "assistant", message=amsg("A2", {"type": "text", "text": LESSON},
                                              tool_use("t2", "Edit", file_path="/w/beta.py",
                                                       old_string="p", new_string="q"))),
]
ch1, doc1 = add_session(S1, sid="sess-1",
                        origin={"project": "-home-sulin-projects-widget", "session_id": "sess-1",
                                "cwd": "/home/sulin/projects/widget"})

# pin the geometry the assertions below depend on: the far sentinel is out of window range of BOTH
# writes; the lesson is in range of beta only.
alpha_line, beta_line = bspan(doc1, "→ Edit: /w/alpha.py"), bspan(doc1, "→ Edit: /w/beta.py")
far, lesson = bspan(doc1, FAR), bspan(doc1, LESSON)
assert far[0] - alpha_line[1] > subject.SPAN_WINDOW, "geometry: far sentinel must clear alpha's window"
assert beta_line[0] - far[1] > subject.SPAN_WINDOW, "geometry: far sentinel must clear beta's window"
assert lesson[0] - alpha_line[1] > subject.SPAN_WINDOW, "geometry: lesson must clear alpha's window"
assert beta_line[0] - lesson[1] < subject.SPAN_WINDOW, "geometry: lesson must sit inside beta's window"


# === (a) narrowing: a span near ONE Edit line yields just that file; (d) repo via repo_label ======

k_lesson = subject.subject_key(R, ch1, lesson)
assert k_lesson == {"repo": "widget", "files": ["/w/beta.py"]}, \
    f"the lesson's co-located write is beta.py alone (alpha is a window away): {k_lesson}"
assert not subject.is_empty(k_lesson)
# repo is the origin CWD's basename (concepts.repo_label), not the datastore slug.
assert k_lesson["repo"] == "widget", k_lesson


# === (b) fallback: a span far from any write → the whole-session union (files only, never tools) ===

k_far = subject.subject_key(R, ch1, far)
assert k_far == {"repo": "widget", "files": ["/w/alpha.py", "/w/beta.py"]}, \
    f"no co-located write → session_facts union: {k_far}"
assert "tools" not in k_far, "the key carries repo+files ONLY — tools deliberately dropped (§2.1)"
# an invalid span (out of bounds) also falls back — recall-safe, never fatal.
assert subject.subject_key(R, ch1, (0, 10 ** 9)) == k_far, "unvalidatable span → fallback union"
assert subject.subject_key(R, ch1, None) == k_far, "missing span → fallback union"


# === cache: one parse per cleaned blob, shared across a batch =====================================

cache = {}
assert subject.subject_key(R, ch1, lesson, cache=cache) == k_lesson
assert ch1 in cache and cache[ch1]["writes"], "the cache holds the per-blob parse"
assert subject.subject_key(R, ch1, far, cache=cache) == k_far, "cache-hit path narrows/falls back too"


# === session 2: Write + NotebookEdit co-located (NotebookEdit renders a JSON arg, not a bare path) =

LESSON2 = "SENTINEL: keep the notebook and its module in lockstep"
S2 = [
    rec("u0", None, "user", message=umsg("sync beta with the notebook")),
    rec("b1", "u0", "assistant", message=amsg("B1", {"type": "text", "text": LESSON2},
                                              tool_use("t1", "Write", file_path="/w/beta.py",
                                                       content="hi"),
                                              tool_use("t2", "NotebookEdit",
                                                       notebook_path="/w/nb.ipynb",
                                                       new_source="cells"))),
]
ch2, doc2 = add_session(S2, sid="sess-2",
                        origin={"project": "-home-sulin-projects-widget", "session_id": "sess-2",
                                "cwd": "/home/sulin/projects/widget"})
assert "→ NotebookEdit: {" in doc2, "weave renders NotebookEdit's input as JSON (no scalar-arg key)"
k2 = subject.subject_key(R, ch2, bspan(doc2, LESSON2))
assert k2 == {"repo": "widget", "files": ["/w/beta.py", "/w/nb.ipynb"]}, \
    f"both co-located writes kept; NotebookEdit's notebook_path parsed from its JSON arg: {k2}"


# === (c) no-edit session with no cwd/project → the EMPTY key; is_empty gates it ====================

S3 = [
    rec("u0", None, "user", message=umsg("what is a monad, roughly?")),
    rec("c1", "u0", "assistant", message=amsg("C1", {"type": "text", "text": "a burrito, roughly"},
                                              tool_use("t1", "Read", file_path="/w/notes.md"),
                                              tool_use("t2", "Bash", command="ls"))),
]
ch3, doc3 = add_session(S3, sid="sess-3", origin={"session_id": "sess-3"})
k3 = subject.subject_key(R, ch3, bspan(doc3, "a burrito, roughly"))
assert k3 == {"repo": None, "files": []}, f"a Read/Bash-only session writes nothing: {k3}"
assert subject.is_empty(k3), "no repo + no files → empty subject (seed-only in resolve, §3.1)"


# === (e) subject_overlap: W_FILE per shared file + W_REPO for a shared repo; tools weigh 0 =========

W_FILE, W_REPO = concepts.W_FILE, concepts.W_REPO
assert subject.subject_overlap(k_lesson, k2) == W_FILE + W_REPO, \
    "one shared file (beta.py) + the shared repo"
assert subject.subject_overlap(k_far, k2) == W_FILE + W_REPO, "sharing is a SET overlap, not count of pairs"
assert subject.subject_overlap({"repo": "widget", "files": []}, k_lesson) == W_REPO, "repo alone → W_REPO once"
assert subject.subject_overlap({"repo": "other", "files": ["/w/beta.py"]}, k_lesson) == W_FILE, \
    "a shared file across different repos still scores W_FILE (soft scope, no repo requirement)"
assert subject.subject_overlap(
    {"repo": None, "files": [], "tools": ["Edit", "Bash"]},
    {"repo": None, "files": [], "tools": ["Edit", "Bash"]}) == 0.0, \
    "tools weigh 0 — CC tool names don't discriminate subjects, identical tools are not overlap"
assert subject.subject_overlap(k3, k3) == 0.0, "empty∩empty = 0 — an empty subject never 'overlaps' (§3.1)"

print("test_subject: all assertions passed")
