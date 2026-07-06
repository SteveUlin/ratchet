"""sources — the human-owned registry of what `ratchet pull` sweeps, plus the feed reader (ADR-0034).

Pure stdlib. A LEAF: imports only `config` and `fetch` (for the shared UA / byte cap / timeout), never
a pipeline stage — so a test of the registry or the feed parser never drags in glean/completer. The
orchestrator that reads this is `pull.py`.

THE EPISTEMOLOGY (ADR-0034 §1). Two state files share `state/` and mean OPPOSITE things:

  sources.json    — CONFIG the OPERATOR owns. His declaration of which sources exist; losing it loses
                    INTENT no derived pass can reconstruct. Hand-editable (`{"sources": [...]}`), never
                    rewritten except by the `sources` verbs he drives.
  feed_state.json — a rebuildable CRAWLER cursor (the per-feed set of seen entry ids). Losing it just
                    re-discovers every entry as "new" and re-taps them — a store no-op on unchanged
                    content, so the cost is a few GETs, never data.

They are SEPARATE FILES on purpose: a crawler write must never reformat the human's config, and a
hand-edit of the config must never clobber the crawler's memory (prevent, don't detect).

FOUR KINDS, `projects` IMPLICIT (§2). A registered entry is `{"kind": "file", "path": …}` or
`{"kind": "url"|"feed", "url": …}`. The transcripts sweep (`projects`) is NOT stored — `pull` always
sweeps the datastore, registry or none — so a fresh install with no `sources.json` still mines the
owner's sessions. A `url` is a page re-asked every pull (ADR-0033: unchanged → store no-op); a `feed`
is a stream whose NEW entries are tapped once (see `parse_feed` + the seen cursor).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen

from . import config, fetch

KIND_PROJECTS = "projects"     # implicit — the datastore sweep, never stored in the registry
KIND_FILE = "file"
KIND_URL = "url"
KIND_FEED = "feed"
REGISTERED_KINDS = (KIND_FILE, KIND_URL, KIND_FEED)   # what the registry actually holds


class DuplicateSource(ValueError):
    """`--add-*` a handle already registered — refused LOUDLY (ADR-0027: no silent reinterpretation)."""


class UnknownSource(KeyError):
    """`--remove` a handle not in the registry — refused with the registered handles listed."""


# --- entry shape + identity -----------------------------------------------------------------------

def handle_of(entry: dict) -> str:
    """The entry's stable identity — the string `--remove` names and duplicate-refusal keys on. A
    file's handle is its resolved absolute path (`~/x` and `/home/…/x` are ONE source); a url/feed's
    is the URL verbatim (aliasing is the operator's call, no normalizer — ADR-0033 §1)."""
    return entry["path"] if entry["kind"] == KIND_FILE else entry["url"]


def _make_entry(kind: str, value: str) -> dict:
    if kind == KIND_FILE:
        # path-as-identity, normalized exactly as tap does at ingest (TapBlock.__init__) so the
        # registered handle matches the source id tap will key on.
        return {"kind": KIND_FILE, "path": str(Path(value).expanduser().resolve())}
    if kind in (KIND_URL, KIND_FEED):
        return {"kind": kind, "url": value}
    raise ValueError(f"unknown source kind {kind!r} (one of {REGISTERED_KINDS})")


# --- the registry: CONFIG the operator owns (state/sources.json) -----------------------------------

def _registry_path(root: Path) -> Path:
    return root / "state" / "sources.json"


def _atomic_write_json(path: Path, obj) -> None:
    """Write-then-rename, atomic because the `.partial` shares the target's dir (same filesystem).
    No fsync: both files here are recoverable (the registry is the operator's, versioned in his head;
    the cursor is rebuildable), so a lost write costs a re-type or a re-fetch, never corruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_registry(root: Path) -> list[dict]:
    """The registered file/url/feed entries; `[]` if the file is absent or unreadable (a fresh
    install has no registry and still pulls projects). Tolerant of a hand-edit: accepts either the
    canonical `{"sources": [...]}` or a bare `[...]`, and drops any entry not a known-kind dict —
    a malformed line must degrade the registry, never crash pull."""
    p = _registry_path(root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("sources", [])
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("kind") in REGISTERED_KINDS]


def save_registry(root: Path, entries: list[dict]) -> None:
    _atomic_write_json(_registry_path(root), {"sources": entries})


def list_sources(root: Path) -> list[dict]:
    """The registered entries (NOT the implicit projects source — that is `pull`'s, never stored)."""
    return load_registry(root)


def add_source(root: Path, kind: str, value: str) -> dict:
    """Register a file/url/feed. Raises `DuplicateSource` if the handle is already registered under ANY
    kind — catching the contradictory case (the same URL as both a `url` and a `feed`) as loudly as a
    plain re-add. Returns the stored entry."""
    entry = _make_entry(kind, value)
    h = handle_of(entry)
    entries = load_registry(root)
    for e in entries:
        if handle_of(e) == h:
            raise DuplicateSource(
                f"{h!r} is already registered as a {e['kind']} source — refusing to add it again "
                f"(ADR-0027: no silent reinterpretation). Remove it first, or hand-edit "
                f"{_registry_path(root)}.")
    entries.append(entry)
    save_registry(root, entries)
    return entry


def remove_source(root: Path, handle: str) -> dict:
    """Drop the entry with this handle and return it. Matches the exact handle, and — for
    convenience, not reinterpretation (removal names WHAT to drop, unambiguously) — also the
    resolved-path form, so `--remove ~/x` drops the stored absolute path. Raises `UnknownSource`
    (listing the registered handles) if nothing matches."""
    candidates = {handle}
    try:
        candidates.add(str(Path(handle).expanduser().resolve()))   # a file handle typed as `~/x`
    except (OSError, RuntimeError):
        pass
    entries = load_registry(root)
    kept, removed = [], None
    for e in entries:
        if removed is None and handle_of(e) in candidates:
            removed = e
        else:
            kept.append(e)
    if removed is None:
        listed = ", ".join(handle_of(e) for e in entries) or "(none)"
        raise UnknownSource(f"no registered source with handle {handle!r}. Registered: {listed}")
    save_registry(root, kept)
    return removed


# --- the per-feed seen cursor: rebuildable crawler state (state/feed_state.json) -------------------

def _feed_state_path(root: Path) -> Path:
    return root / "state" / "feed_state.json"


def load_feed_state(root: Path) -> dict[str, list[str]]:
    p = _feed_state_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def feed_seen(root: Path, feed_url: str) -> set[str]:
    """The entry ids already tapped from this feed — the gate that makes a re-pull fetch only NEW
    posts (empty on a lost/rebuilt cursor, which just re-discovers everything, a store no-op)."""
    return set(load_feed_state(root).get(feed_url, []))


def mark_feed_seen(root: Path, feed_url: str, ids) -> None:
    """Union `ids` into this feed's seen set (order-stable). Called AFTER tap, for entries confirmed
    in the store — so a transiently-dead entry link stays unseen and retries next pull (ADR-0034 §3)."""
    state = load_feed_state(root)
    merged = list(dict.fromkeys([*state.get(feed_url, []), *ids]))   # de-dup, preserve first-seen order
    if merged == state.get(feed_url):
        return                                          # nothing new — skip the write
    state[feed_url] = merged
    _atomic_write_json(_feed_state_path(root), state)


# --- the feed reader: fetch raw XML, parse RSS 2.0 / Atom for entry links --------------------------

def _local(tag: str) -> str:
    """An element's local name minus any `{namespace}` — Atom lives in a namespace, RSS core does
    not, so matching on the local name tolerates BOTH without hard-coding either namespace."""
    return tag.rsplit("}", 1)[-1]


def _entry_id(el) -> str | None:
    """The entry's stable id: RSS `<guid>` or Atom `<id>` (the seen-cursor key). None → the caller
    falls back to the link, so an id-less entry still de-dups by URL."""
    for child in el:
        if _local(child.tag) in ("guid", "id") and (child.text or "").strip():
            return child.text.strip()
    return None


def _entry_link(el) -> str | None:
    """The entry's target URL. Atom carries it as `<link href=…>` (prefer `rel="alternate"` — the
    human-readable page — over `self`/`edit`/enclosure); RSS as `<link>text</link>`. Returns the
    alternate/text link if present, else the first `href` link, else None."""
    fallback = None
    for child in el:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href:                                        # Atom
            rel = child.get("rel", "alternate")
            if rel == "alternate":
                return href.strip()
            fallback = fallback or href.strip()
        elif (child.text or "").strip():                # RSS
            return child.text.strip()
    return fallback


def parse_feed(xml_text: str) -> list[tuple[str, str]]:
    """Parse an RSS 2.0 or Atom document into `[(entry_id, entry_url)]`, in feed order. Tolerates
    both by matching on local element names (`item`/`entry`, `link`, `guid`/`id`) regardless of
    namespace. An entry with no resolvable link is skipped (nothing to tap); an id-less entry uses
    its link as the id. Raises `xml.etree.ElementTree.ParseError` on malformed XML — the caller
    (`pull`) isolates a bad feed per source (ADR-0034 §6)."""
    root = ET.fromstring(xml_text)
    out: list[tuple[str, str]] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = _entry_link(el)
        if not link:
            continue
        out.append((_entry_id(el) or link, link))
    return out


def fetch_feed(url: str, *, timeout: float = fetch.FETCH_TIMEOUT_S) -> str:
    """GET a feed URL and return its RAW XML text — NOT `fetch.fetch_url`, whose HTML extractor would
    mangle the markup (and whose non-text refusal would reject `application/rss+xml`). Reuses fetch's
    truthful User-Agent + byte cap + timeout so a feed and a page look identical on the wire. Raises
    on any failure (bad scheme, HTTP error, over-cap) with a message — `pull` isolates a dead feed."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"a feed URL speaks http(s); {url!r} does not")
    req = Request(url, headers={"User-Agent": fetch.USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read(fetch.FETCH_MAX_BYTES + 1)
        if len(body) > fetch.FETCH_MAX_BYTES:
            raise ValueError(
                f"{url} exceeds FETCH_MAX_BYTES ({fetch.FETCH_MAX_BYTES} bytes) — a feed that large is "
                f"a dump or a misdirected URL; raise the cap in fetch.py if it is genuinely that big")
        charset = resp.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


# --- the `sources` CLI: manage the registry -------------------------------------------------------

def _print_list(root: Path, *, out=sys.stdout) -> None:
    """The whole pull plan the human owns — the implicit projects line first (so `--list` shows what
    `pull` does, not just what he typed), then each registered entry as `kind  handle`."""
    print("projects  (Claude Code transcripts)  [implicit — always swept by pull]", file=out)
    entries = load_registry(root)
    if not entries:
        print("(no file/url/feed sources registered — `sources --add-file/--add-url/--add-feed`)", file=out)
        return
    width = max(len(e["kind"]) for e in entries)
    for e in entries:
        print(f"{e['kind']:<{width}}  {handle_of(e)}", file=out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="sources",
        description="Manage the sources registry `ratchet pull` sweeps (files, pages, feeds). "
                    "The transcripts sweep (projects) is implicit — always pulled, never registered.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="show the pull plan (implicit projects + registered)")
    g.add_argument("--add-file", metavar="PATH", help="register a document file (verbatim ingest, ADR-0031)")
    g.add_argument("--add-url", metavar="URL", help="register a page — re-fetched every pull (ADR-0033)")
    g.add_argument("--add-feed", metavar="URL",
                   help="register an RSS/Atom feed — pull taps each NEW entry link once (ADR-0034)")
    g.add_argument("--remove", metavar="HANDLE", help="drop a registered source by its handle (a path or URL)")
    args = ap.parse_args(argv)
    root = config.ensure_layout()
    try:
        if args.list:
            _print_list(root)
        elif args.add_file is not None:
            e = add_source(root, KIND_FILE, args.add_file)
            print(f"added file source: {handle_of(e)}")
        elif args.add_url is not None:
            e = add_source(root, KIND_URL, args.add_url)
            print(f"added url source: {handle_of(e)}")
        elif args.add_feed is not None:
            e = add_source(root, KIND_FEED, args.add_feed)
            print(f"added feed source: {handle_of(e)}")
        elif args.remove is not None:
            e = remove_source(root, args.remove)
            print(f"removed {e['kind']} source: {handle_of(e)}")
    except (DuplicateSource, UnknownSource) as ex:
        ap.error(str(ex))   # loud refusal, exit 2 — never a silent reinterpretation (ADR-0027)


if __name__ == "__main__":
    main()
