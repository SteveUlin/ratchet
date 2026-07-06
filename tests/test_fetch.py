"""url source tests (ADR-0033): `fetch.extract_html` reduces a page to its reading flow (golden-
pinned: headings, list grouping, fenced <pre>, link rendering, chrome dropped, blank-run collapse);
`fetch_url` refuses non-http schemes, over-cap bodies, and non-text content (PDF is a future ADR),
passes text/* through raw, and sends the truthful User-Agent; `tap --url` (fetcher injected — NO
network anywhere here) ingests the EXTRACTED text as the ADR-0031 document shape end-to-end (weave
renders, chunk chunks), an unchanged page no-ops even when its raw HTML churns (extract-then-
fingerprint), a changed page mints a new prev-linked version, and a dead host errors cleanly while
its siblings still ingest. Run: `python tests/test_fetch.py` (throwaway dirs)."""
import contextlib
import email.message
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-url-")

from ratchet import blobstore, block, chunk, config, fetch, tap, temporal, weave  # noqa: E402

config.ensure_layout()
root = config.data_root()


# --- 1. the extractor golden: reading flow in, chrome out --------------------------------------

HTML = """<!DOCTYPE html>
<html><head>
<title>Tab Title To Drop</title>
<style>body { color: red }</style>
<script>var tracking = "chrome, not content";</script>
</head>
<body>
<header><p>Site banner and menu</p></header>
<nav><a href="/home">Home</a> <a href="/about">About</a></nav>
<main>
<h1>Getting Started</h1>
<p>The first paragraph explains the <code>init</code> command.</p>


<p>See <a href="https://example.com/docs">the docs</a> and <a href="https://example.com/plain">https://example.com/plain</a> and <a href="#local">a local anchor</a>.</p>
<h2>Install</h2>
<ul>
<li>step one</li>
<li>step two</li>
</ul>
<pre><code>$ ratchet tap --url https://example.com

# blank line above survives verbatim</code></pre>
<aside>A pull-quote to drop.</aside>
<form><input name="q"><button>Search</button></form>
<svg><text>chart label</text></svg>
<noscript>Enable JS</noscript>
</main>
<footer>&copy; 2026 nobody</footer>
</body></html>"""

# the golden: every block is one paragraph turn downstream (weave splits on the "\n\n" separator);
# blank-run collapse is structural — the double blank line between the fixture's paragraphs
# arrives as exactly ONE separator.
GOLDEN = """# Getting Started

The first paragraph explains the `init` command.

See the docs (https://example.com/docs) and https://example.com/plain and a local anchor.

## Install

- step one
- step two

```
$ ratchet tap --url https://example.com

# blank line above survives verbatim
```
"""

got = fetch.extract_html(HTML)
assert got == GOLDEN, f"extractor golden drifted:\n---got---\n{got}\n---want---\n{GOLDEN}"

# malformed-input degrades, each in the guard's direction:
assert fetch.extract_html("<p>kept</p><nav>menu<p>never closed") == "kept\n", \
    "an unclosed _DROP tag drops to EOF — in doubt about chrome, exclude"
assert fetch.extract_html("<p>a</p><pre>code never closed") == "a\n\n```\ncode never closed\n```\n", \
    "an unterminated <pre> keeps what it captured — in doubt about CONTENT, keep"
assert fetch.extract_html("<!-- ts: 1 --><p>x</p>") == fetch.extract_html("<!-- ts: 2 --><p>x</p>"), \
    "comments never reach the output (comment churn is invisible)"
assert fetch.extract_html("") == "", "an empty page extracts to the empty string"

print("OK — extract_html: golden pinned (headings/list/fence/links/chrome/blank-collapse), and")
print("     malformed input degrades conservatively (drop-to-EOF for chrome, keep for content)")


# --- 2. fetch_url over a fake urlopen: content types, the cap, the UA ---------------------------

class FakeResponse:
    """Duck-types the slice of http.client.HTTPResponse fetch_url reads: read(n), geturl(), status,
    and .headers as a real email.message.Message (the same get_content_type()/get_content_charset()
    surface urllib hands back)."""
    def __init__(self, body: bytes, ctype: str | None, url: str, status: int = 200):
        self._body, self._url, self.status = body, url, status
        self.headers = email.message.Message()
        if ctype:
            self.headers["Content-Type"] = ctype

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


served = {}      # url -> FakeResponse
requests = []    # every Request fetch_url built — the UA assertion reads these
_orig_urlopen = fetch.urlopen


def fake_urlopen(req, timeout=None):
    requests.append(req)
    return served[req.full_url]


fetch.urlopen = fake_urlopen

U = "https://example.com/guide"
served[U] = FakeResponse(HTML.encode("utf-8"), "text/html; charset=utf-8", U + "/", status=200)
text, meta = fetch.fetch_url(U)
assert text == GOLDEN, "fetch_url routes HTML through extract_html"
assert meta["url"] == U and meta["final_url"] == U + "/", "meta records both ends of the redirect"
assert meta["content_type"] == "text/html" and meta["http_status"] == 200 and meta["fetched_at"]
assert requests[-1].get_header("User-agent") == fetch.USER_AGENT, "the truthful UA rides every fetch"

# text/* (non-HTML) passes through RAW — already reading flow, extraction would only mangle it.
RAW = "line one\n\n\n\nline   four, spacing intact\n"
served[U] = FakeResponse(RAW.encode("utf-8"), "text/plain", U)
text, meta = fetch.fetch_url(U)
assert text == RAW and meta["content_type"] == "text/plain", "text/* is a verbatim passthrough"

# an ABSENT Content-Type reads as HTML (a URL handed to ratchet is overwhelmingly a web page).
served[U] = FakeResponse(b"<p>untyped</p>", None, U)
assert fetch.fetch_url(U)[0] == "untyped\n", "no Content-Type header → treated as HTML"

# non-text refuses, and the message says why + what to do (PDF is a future ADR).
served[U] = FakeResponse(b"%PDF-1.7", "application/pdf", U)
try:
    fetch.fetch_url(U)
    raise AssertionError("application/pdf must be refused")
except ValueError as e:
    assert "PDF" in str(e) and "future ADR" in str(e) and "--file" in str(e), f"unhelpful refusal: {e}"

# the byte cap refuses rather than truncating (a silent truncation would glean a half-document).
served[U] = FakeResponse(b"x" * (fetch.FETCH_MAX_BYTES + 10), "text/html", U)
try:
    fetch.fetch_url(U)
    raise AssertionError("an over-cap body must be refused")
except ValueError as e:
    assert "FETCH_MAX_BYTES" in str(e), f"the refusal names the knob: {e}"

# a non-http(s) scheme refuses BEFORE any I/O — the filesystem is --file's job.
n_before = len(requests)
try:
    fetch.fetch_url("file:///etc/passwd")
    raise AssertionError("file:// must be refused")
except ValueError as e:
    assert "--file" in str(e) and len(requests) == n_before, "refused without opening anything"

fetch.urlopen = _orig_urlopen

print("OK — fetch_url: HTML→extract, text/* raw, absent-type→HTML, PDF/over-cap/scheme refused")
print("     with named reasons, truthful User-Agent on the wire")


# --- 3. tap --url end-to-end: the ADR-0031 document shape, one level up -------------------------

PAGE = "https://example.com/style-guide"
DEAD = "https://gone.invalid/x"
RULE_HTML = ("<html><head><script>let x = 'v1';</script></head><body>"
             "<nav><a href='/'>home</a></nav>"
             "<h1>House Style</h1>"
             "<p>Always compress prose; every sentence earns its place.</p>"
             "</body></html>")
page = {"html": RULE_HTML}


class FakeFetcher:
    """The injected fetcher seam (TapBlock(fetcher=…)) — runs the REAL extractor over an in-memory
    'server' so churn immunity is proven end-to-end, and counts calls so fetch-every-run (a URL is
    never done-skipped) is assertable."""
    def __init__(self):
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        if url == DEAD:
            raise urllib.error.URLError("simulated: host unreachable")
        return fetch.extract_html(page["html"]), {
            "url": url, "final_url": url, "content_type": "text/html",
            "http_status": 200, "fetched_at": config.now()}


ff = FakeFetcher()


def tap_urls(*urls, **kw):
    """A fresh TapBlock per call mirrors a fresh process (cursor + latest reload in items())."""
    return block.run(tap.TapBlock(urls=tuple(urls), fetcher=ff), **kw)


# 3a. first tap: the extracted text lands as a `document` raw blob, url-as-session.
rep = tap_urls(PAGE)
assert rep.processed == 1 and rep.outputs == 1, f"the page ingested: {rep}"
raw1 = blobstore.latest_version(PAGE)
assert raw1 is not None, "the URL is the source id"
m1 = blobstore.get_meta(raw1)
assert m1["source_kind"] == "document" and m1["source_id"] == PAGE
o = m1["origin_ref"]
assert o["url"] == PAGE and o["session_id"] == PAGE, "url-as-session (ADR-0031 §2, inherited)"
assert o["final_url"] == PAGE and o["content_type"] == "text/html" and o["http_status"] == 200
assert o["fetched_at"] and o["mtime"] == o["fetched_at"], \
    "the fetch instant fills the shared valid-time slot (origin_ref.mtime)"
assert "project" not in o and "cwd" not in o and "path" not in o, \
    "no repo-facet fields — a URL document stays subject-empty"
assert blobstore.get(raw1) == fetch.extract_html(RULE_HTML), \
    "the STORED artifact is the extracted text, not the raw HTML"
assert "House Style" in blobstore.get(raw1) and "<nav>" not in blobstore.get(raw1)
# the cursor recorded the EXTRACTED text's fingerprint.
assert tap.load_fetch_state(root)[PAGE][2] == blobstore.blob_hash(fetch.extract_html(RULE_HTML))

# 3b. downstream shape: weave renders it (header names the URL), chunk chunks it.
ch, _, rdoc = weave.materialize(raw1)
cleaned = blobstore.get(ch)
assert cleaned.startswith(f"[document] {PAGE}"), "the header turn names the page"
assert "Always compress prose" in cleaned
assert blobstore.get_meta(ch)["format"] == weave.DOC_RENDER_FORMAT
cs, _, chunks = chunk.materialize(raw1)
assert blobstore.get_meta(cs)["format"] == chunk.DOC_CHUNKSET_FORMAT
assert chunks and all(c.kinds == ["document"] for c in chunks)
# the FOCUS handle (--source) and the valid-time map reach URL documents.
assert blobstore.project_of(ch, root) == PAGE, "a URL document's --source handle is its URL"
assert temporal.session_valid_times(root).get(PAGE), \
    "the URL session is dated by its fetch instant (joins decay tracking)"

# 3c. CHURN IMMUNITY: raw HTML changes (script nonce, comment), reading flow doesn't → no-op.
page["html"] = RULE_HTML.replace("'v1'", "'v2-nonce'").replace("<body>", "<body><!-- ts 99 -->")
assert page["html"] != RULE_HTML
assert fetch.extract_html(page["html"]) == fetch.extract_html(RULE_HTML), "churn is chrome-only"
calls = ff.calls
rep2 = tap_urls(PAGE)
assert ff.calls == calls + 1 and rep2.skipped == 0, \
    "a URL is never done-skipped — every run asks the server (the only way to see a change)"
assert rep2.outputs == 0, f"extraction-identical page mints nothing: {rep2}"
assert blobstore.latest_version(PAGE) == raw1, "no new version from raw-HTML churn"

# 3d. a CHANGED page mints a new prev-linked VERSION of the same source (the edited-file rule).
page["html"] = page["html"].replace("every sentence earns its place",
                                    "every sentence and every comment earns its place")
rep3 = tap_urls(PAGE)
assert rep3.outputs == 1, f"a changed page ingests a new snapshot: {rep3}"
raw2 = blobstore.latest_version(PAGE)
assert raw2 != raw1 and blobstore.get_meta(raw2)["prev"] == raw1, "new VERSION, prev-linked"

# 3e. network-error isolation: the dead host errors cleanly; its sibling still ingests; retried.
page["html"] = RULE_HTML  # a revert: content-address no-op for PAGE (nothing new minted)
rep4 = tap_urls(DEAD, PAGE)
assert rep4.errored == 1 and rep4.processed == 1, f"dead URL isolated, sibling processed: {rep4}"
assert blobstore.latest_version(DEAD) is None, "nothing stored for the dead URL"
rep5 = tap_urls(DEAD)
assert rep5.errored == 1, f"the dead URL is retried next run (never marked done): {rep5}"

# 3f. --file and --url compose (both are explicit document sources; one block, both ingested).
docdir = Path(tempfile.mkdtemp(prefix="ratchet-test-urldoc-"))
f = docdir / "notes.md"
f.write_text("A local note.\n", encoding="utf-8")
rep6 = block.run(tap.TapBlock(files=(f,), urls=(PAGE,), fetcher=ff))
assert blobstore.latest_version(str(f)) is not None, "--file ingested alongside --url"

# 3g. --source-id scopes to one URL; --url refuses the sweep selectors (they'd be silently inert).
rep7 = block.run(tap.TapBlock(urls=(PAGE, DEAD), fetcher=ff), source_id=PAGE)
assert rep7.examined == 1 and rep7.errored == 0, f"--source-id enumerates only the one URL: {rep7}"
try:
    with contextlib.redirect_stderr(io.StringIO()):
        tap.main(["--url", PAGE, "--last", "5", "--quiet"])
    raise AssertionError("--url + --last must be refused")
except SystemExit:
    pass

# 3h. --dry-run lists the URL without fetching (no network on a list-only pass).
calls = ff.calls
rep8 = tap_urls(PAGE, dry_run=True)
assert rep8.would_process == 1 and ff.calls == calls, f"--dry-run never fetches: {rep8}"

print("OK — tap --url: extracted text as the document shape (weave/chunk end-to-end, url-as-session,")
print("     subject-empty, valid-time = fetch instant), churn-immune no-op, changed page re-versions,")
print("     dead host isolated + retried, --file composes, source-id scopes, selectors refused,")
print("     dry-run offline")

print("\nall url-source tests passed.")
