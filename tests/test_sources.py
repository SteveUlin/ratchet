"""sources registry + pull tests (ADR-0034). The registry round-trips (add/list/remove) and refuses
duplicates LOUDLY (ADR-0027); feed parsing handles RSS 2.0 and Atom (inline fixtures); the per-feed
seen cursor makes a second pull find nothing new; a dead feed is isolated per source (the sweep goes
on); `pull --dry-run` enumerates every source WITHOUT touching the network; and pull is $0 by default
(no glean tick unless --max-usd). NO real network anywhere — the feed + url fetchers are injected the
way test_fetch fakes `fetch_url`. Run: `python tests/test_sources.py` (throwaway dirs)."""
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-sources-")

from ratchet import blobstore, config, fetch, pull, sources  # noqa: E402

config.ensure_layout()


def fresh_root(tag: str) -> Path:
    """A throwaway data root, layout ensured — each concern gets its own so a registry mutation in one
    can't leak into another's assertions (every sources/pull entry point takes root explicitly)."""
    return config.ensure_layout(Path(tempfile.mkdtemp(prefix=f"ratchet-test-{tag}-")))


# --- 1. the registry: add / list / remove round-trip, duplicates refused loudly (ADR-0027) --------

r1 = fresh_root("reg")
assert sources.list_sources(r1) == [], "a fresh install has no registry (projects is implicit)"

docf = Path(tempfile.mkdtemp(prefix="ratchet-test-regfile-")) / "CLAUDE.md"
e_file = sources.add_source(r1, sources.KIND_FILE, str(docf))
e_url = sources.add_source(r1, sources.KIND_URL, "https://example.com/guide")
e_feed = sources.add_source(r1, sources.KIND_FEED, "https://example.com/feed.xml")

# a file's handle is its RESOLVED absolute path (one source no matter how it's typed); url/feed verbatim
assert sources.handle_of(e_file) == str(docf.resolve()), "file handle is the resolved path"
handles = {sources.handle_of(e) for e in sources.list_sources(r1)}
assert handles == {str(docf.resolve()), "https://example.com/guide", "https://example.com/feed.xml"}
# persisted: a fresh read off disk sees all three (the registry is durable config, not in-memory)
assert len(sources.load_registry(r1)) == 3, "the registry persisted to state/sources.json"

# a re-add of the same handle is refused LOUDLY, not silently reinterpreted
try:
    sources.add_source(r1, sources.KIND_URL, "https://example.com/guide")
    raise AssertionError("a duplicate url must be refused")
except sources.DuplicateSource as ex:
    assert "already registered" in str(ex), f"the refusal explains itself: {ex}"
# the SAME handle under a DIFFERENT kind is contradictory — refused just as loudly (names the old kind)
try:
    sources.add_source(r1, sources.KIND_FEED, "https://example.com/guide")
    raise AssertionError("the same handle under a different kind must be refused")
except sources.DuplicateSource as ex:
    assert "url" in str(ex), f"the refusal names the existing kind: {ex}"

# remove by handle round-trips; the ~ / path form also matches (removal names WHAT to drop)
removed = sources.remove_source(r1, str(docf))
assert removed["kind"] == sources.KIND_FILE
assert str(docf.resolve()) not in {sources.handle_of(e) for e in sources.list_sources(r1)}
# removing an unknown handle errors, listing what IS registered
try:
    sources.remove_source(r1, "https://nope.example/x")
    raise AssertionError("removing an unknown handle must error")
except sources.UnknownSource as ex:
    assert "no registered source" in str(ex)

print("OK — registry: add/list/remove round-trip, duplicate + cross-kind refused loudly, "
      "unknown-remove errors")


# --- 2. feed parsing: RSS 2.0 and Atom, tolerated by local-name matching (no namespace hard-code) --

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <title>The Blog</title>
  <item><title>Post One</title><link>https://blog.example/1</link><guid>tag:blog,1</guid></item>
  <item><title>Post Two</title><link>https://blog.example/2</link></item>
</channel></rss>"""

# RSS: id = guid when present, else the link; url = <link> text.
assert sources.parse_feed(RSS) == [
    ("tag:blog,1", "https://blog.example/1"),
    ("https://blog.example/2", "https://blog.example/2"),
], sources.parse_feed(RSS)

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>The Blog</title>
  <entry><title>A</title><id>urn:uuid:aaa</id>
    <link rel="alternate" href="https://blog.example/a"/>
    <link rel="self" href="https://blog.example/a.atom"/></entry>
  <entry><title>B</title><link href="https://blog.example/b"/></entry>
</feed>"""

# Atom: id = <id>, else the link; url = <link href> preferring rel="alternate" over rel="self".
assert sources.parse_feed(ATOM) == [
    ("urn:uuid:aaa", "https://blog.example/a"),
    ("https://blog.example/b", "https://blog.example/b"),
], sources.parse_feed(ATOM)

# malformed XML raises (pull isolates it per feed) — never a silent empty parse.
try:
    sources.parse_feed("<rss><channel><item><link>x")
    raise AssertionError("malformed feed XML must raise")
except Exception:
    pass

print("OK — parse_feed: RSS 2.0 (item/link/guid) and Atom (entry/link[@href]/id) both parsed, "
      "malformed raises")


# --- 3. pull + the per-feed seen cursor: a second pull sees nothing new (offline fakes) ------------

proot = fresh_root("pullroot")
emptyds = Path(tempfile.mkdtemp(prefix="ratchet-test-pullds-"))   # empty datastore → projects sweep no-ops
FEED = "https://blog.example/feed.xml"
sources.add_source(proot, sources.KIND_FEED, FEED)

feed_calls = {"n": 0}


def fake_feed_fetcher(url):
    """The injected feed-XML seam — returns the RSS fixture (2 entries), no network."""
    feed_calls["n"] += 1
    assert url == FEED, url
    return RSS


tap_calls = []


def fake_tap_fetcher(url):
    """The injected --url page seam (TapBlock(fetcher=…)) — runs the REAL extractor over an in-memory
    page so the tap→weave→chunk flow is exercised end-to-end, and records each fetch so 'only new posts
    fetch' is assertable."""
    tap_calls.append(url)
    if "unreachable" in url:
        raise urllib.error.URLError("simulated: host unreachable")
    return fetch.extract_html(f"<h1>Post</h1><p>Body of {url}.</p>"), {
        "url": url, "final_url": url, "content_type": "text/html",
        "http_status": 200, "fetched_at": config.now()}


# first pull: both entries are NEW → both fetched + ingested as document sources.
r_first = pull.run(root=proot, datastore=emptyds, feed_fetcher=fake_feed_fetcher,
                   tap_fetcher=fake_tap_fetcher, out=io.StringIO())
assert r_first["glean"] is None, "pull is $0 by default — NO glean tick without --max-usd"
assert r_first["tap"]["feed_new_entries"] == 2, r_first
assert r_first["tap"]["feed_failures"] == 0, r_first
assert set(tap_calls) == {"https://blog.example/1", "https://blog.example/2"}, tap_calls
assert blobstore.latest_version("https://blog.example/1", proot) is not None, "entry tapped as a url doc"
assert blobstore.latest_version("https://blog.example/2", proot) is not None
# the cursor now records BOTH entry ids (guid for #1, link-as-id for #2)
assert sources.feed_seen(proot, FEED) == {"tag:blog,1", "https://blog.example/2"}, \
    sources.feed_seen(proot, FEED)
# weave/chunk ran over the new document sources ($0 prep)
assert r_first["weave"]["woven"] >= 2 and r_first["chunk"]["chunksets"] >= 2, r_first

# second pull: same feed, nothing new — the cursor gates every entry, so NO entry re-fetches.
tap_calls.clear()
r_second = pull.run(root=proot, datastore=emptyds, feed_fetcher=fake_feed_fetcher,
                    tap_fetcher=fake_tap_fetcher, out=io.StringIO())
assert r_second["tap"]["feed_new_entries"] == 0, "the seen cursor sees no new entries on re-pull"
assert tap_calls == [], "no entry re-fetched — the feed cursor gates them (only new posts fetch)"

print("OK — pull: feed new-entries tapped as url docs (weave/chunk end-to-end), seen cursor makes "
      "a second pull find nothing new, $0 by default (glean is None)")


# --- 4. network honesty: a dead feed is isolated per source; the good feed still resolves ----------

froot = fresh_root("deadfeed")
GOOD, DEAD = "https://blog.example/good.xml", "https://blog.example/dead.xml"
sources.add_source(froot, sources.KIND_FEED, GOOD)
sources.add_source(froot, sources.KIND_FEED, DEAD)


def flaky_feed_fetcher(url):
    if url == DEAD:
        raise urllib.error.URLError("simulated: feed host down")
    return RSS


r_flaky = pull.run(root=froot, datastore=emptyds, feed_fetcher=flaky_feed_fetcher,
                   tap_fetcher=fake_tap_fetcher, out=io.StringIO())
assert r_flaky["tap"]["feed_failures"] == 1, f"the dead feed is marked failed: {r_flaky}"
assert r_flaky["tap"]["feed_new_entries"] == 2, f"the good feed's entries still resolve: {r_flaky}"

print("OK — network honesty: a dead feed is isolated + marked failed, the sweep continues")


# --- 5. pull --dry-run: enumerate every source WITHOUT touching the network ------------------------

droot = fresh_root("dryrun")
dfile = Path(tempfile.mkdtemp(prefix="ratchet-test-dryfile-")) / "notes.md"
sources.add_source(droot, sources.KIND_FILE, str(dfile))
sources.add_source(droot, sources.KIND_URL, "https://static.example/page")
sources.add_source(droot, sources.KIND_FEED, "https://static.example/feed.xml")

feed_calls["n"] = 0
tap_calls.clear()
buf = io.StringIO()
r_dry = pull.run(root=droot, datastore=emptyds, dry_run=True, feed_fetcher=fake_feed_fetcher,
                 tap_fetcher=fake_tap_fetcher, out=buf)
assert feed_calls["n"] == 0, "--dry-run fetches no feed (offline)"
assert tap_calls == [], "--dry-run taps nothing"
assert r_dry["glean"] is None, "--dry-run makes no LLM spend"
text = buf.getvalue()
assert "projects" in text and str(emptyds) in text, "the implicit projects line is listed"
assert str(dfile.resolve()) in text, "the file source is listed"
assert "https://static.example/page" in text and "https://static.example/feed.xml" in text, \
    "url and feed sources are listed"
# nothing was written to the store on a dry run.
assert blobstore.latest_version("https://static.example/page", droot) is None, "dry-run wrote no blob"

print("OK — pull --dry-run: enumerates projects + file + url + feed offline, no fetch, no write")

print("\nall sources + pull tests passed.")
