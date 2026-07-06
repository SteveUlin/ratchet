"""fetch — pull a webpage over HTTP and reduce it to readable text (ADR-0033). Pure stdlib.

`fetch_url(url)` returns (extracted_text, meta): the page's reading flow as plain text, plus the
fetch facts ({url, final_url, content_type, http_status, fetched_at}). `tap --url` ingests the
extracted text as a `document` raw blob — ADR-0031's "pilot for fetched sources", cashed for
webpages; the raising seam is clean (network/refusal errors carry a message, the Block driver
isolates the item), and nothing here writes.

WHY NO READABILITY HEURISTIC: the extractor is deliberately CONSERVATIVE — a fixed drop-list of
non-content containers (script/style/nav/…), a flat rendering of everything else, no scoring, no
boilerplate detection. Beyond the pure-stdlib ethos (a readability port is a project, not a
module), the deeper reason is auditability: every claim gleaned from a page must survive the human
gate READING THE EVIDENCE. A wrong-but-verbatim extraction (some banner text survives) is
auditable — the reviewer sees exactly what the extractor saw and rejects the noise; a
clever-but-lossy one (a scoring pass that silently drops the paragraph a claim leans on) is not —
the loss is invisible at review. Noise costs a few wasted glean lines; silent loss costs the
evidence. When in doubt, keep.
"""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.request import Request, urlopen

from . import config

FETCH_MAX_BYTES = 2 * 1024 * 1024  # refuse a response beyond 2 MiB. A document source is prose for
                                   # the human gate to read; real article/docs pages run tens-to-
                                   # hundreds of KB, so anything past this is a dump or a binary
                                   # behind a misdirected URL — refusing beats flooding chunk/glean
                                   # (and the store) with megabytes nobody will review. One edit here.
FETCH_TIMEOUT_S = 30.0             # generous for a slow origin, small enough that a dead host errors
                                   # this ITEM within the tick instead of hanging the whole run.
USER_AGENT = "ratchet/0.1 (+https://github.com/SteveUlin/ratchet)"  # truthful: who we are and where
                                   # the code lives, so an operator seeing us in a server log can
                                   # look us up — never a spoofed browser string.

# The fixed drop-list: subtrees that are page CHROME, never the reading flow. `title` joins the
# spec'd set (script/style/nav/header/footer/aside/noscript/svg/form) because it is tab metadata —
# it usually duplicates the h1, and the `[document] <url>` header turn already names the page.
_DROP = frozenset({"script", "style", "nav", "header", "footer", "aside", "noscript", "svg",
                   "form", "title"})

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# Tags whose edges end/begin a line of reading flow — the inline buffer flushes there. Deliberately
# NOT the full HTML block-element list: just what keeps distinct thoughts on distinct lines. An
# unlisted container simply flows inline — conservative: text is never lost, only joined.
_FLUSH = frozenset({"p", "div", "section", "article", "main", "ul", "ol", "table", "tr",
                    "blockquote", "dt", "dd", "figure", "figcaption", "details", "summary",
                    "br", "hr"})


class _TextExtractor(HTMLParser):
    """HTML → reading-flow text: headings as `#`-prefixed lines, paragraphs/list items as lines,
    `<pre>` fenced verbatim, links as their anchor text (href in parens only when the text isn't
    already the URL), the `_DROP` chrome gone entirely. Blocks join with ONE blank line — blank-run
    collapse is structural (an empty flush emits nothing), and the `\\n\\n` separator is exactly
    weave's paragraph-turn split, so each block becomes one document turn downstream.

    Malformed HTML degrades in the guard's direction: an unclosed `_DROP` tag drops to EOF (when in
    doubt about chrome, exclude — `strip_generated`'s posture), an unterminated `<pre>` still emits
    what it captured (when in doubt about CONTENT, keep), an `<a>` split by a block boundary keeps
    its text and loses only the parenthesized href."""

    def __init__(self) -> None:
        super().__init__()               # convert_charrefs=True: entities arrive decoded in handle_data
        self._blocks: list[str] = []     # finished blocks; "\n\n"-joined by text()
        self._buf: list[str] = []        # the current inline run
        self._prefix = ""                # heading '#'s / list '- ', applied at the next flush
        self._drop = 0                   # depth inside _DROP subtrees — everything there is ignored
        self._pre = 0                    # depth inside <pre> — captured verbatim, fenced on exit
        self._pre_buf: list[str] = []
        self._links: list[tuple[str, int]] = []   # (href, buf index at <a>) — closed on </a>

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())   # HTML whitespace collapse (newlines/runs → one space)
        self._buf, self._links = [], []
        prefix, self._prefix = self._prefix, ""
        if not text:
            return                                    # empty flush emits nothing → no blank-line runs
        line = prefix + text
        if prefix == "- " and self._blocks and self._blocks[-1].startswith("- "):
            self._blocks[-1] += "\n" + line           # consecutive list items stay ONE block — a list
            return                                    # reads (and chunks) as a unit, not confetti
        self._blocks.append(line)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP:
            self._drop += 1
            return
        if self._drop or self._pre:                   # inside chrome, or inside a verbatim fence:
            return                                    # markup carries nothing
        if tag == "pre":
            self._flush()
            self._pre += 1
        elif tag in _HEADINGS:
            self._flush()
            self._prefix = "#" * _HEADINGS[tag] + " "
        elif tag == "li":
            self._flush()
            self._prefix = "- "
        elif tag == "code":
            self._buf.append("`")                     # inline code; inside <pre> the fence covers it
        elif tag == "a":
            href = next((v for k, v in attrs if k == "href"), "") or ""
            self._links.append((href, len(self._buf)))
        elif tag in ("td", "th"):
            self._buf.append(" ")                     # a cell boundary is at least a word boundary
        elif tag in _FLUSH:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            self._drop = max(0, self._drop - 1)       # a stray close never underflows the guard
            return
        if self._drop:
            return
        if tag == "pre":
            if self._pre:
                self._pre -= 1
                if not self._pre:
                    self._fence()
            return
        if self._pre:
            return
        if tag in _HEADINGS or tag == "li" or tag in _FLUSH:
            self._flush()
        elif tag == "code":
            self._buf.append("`")
        elif tag == "a" and self._links:
            href, start = self._links.pop()
            text = " ".join("".join(self._buf[start:]).split())
            # the href earns its parens only when the anchor text doesn't already say it — and a
            # fragment/javascript href is navigation, not a citable location.
            if href and text and text != href and not href.startswith(("#", "javascript:")):
                self._buf.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if self._drop:
            return
        if self._pre:
            self._pre_buf.append(data)                # verbatim — code must survive byte-for-byte
        else:
            self._buf.append(data)

    def _fence(self) -> None:
        code = "".join(self._pre_buf).strip("\n")
        self._pre_buf = []
        if code:
            self._blocks.append(f"```\n{code}\n```")

    def text(self) -> str:
        if self._pre:                                 # unterminated <pre>: keep what it captured —
            self._pre = 0                             # in doubt about CONTENT, keep (contrast _DROP)
            self._fence()
        self._flush()
        return "\n\n".join(self._blocks) + ("\n" if self._blocks else "")


def extract_html(html: str) -> str:
    """Reduce an HTML page to its reading flow (see `_TextExtractor`). Deterministic and offline —
    the extraction seam tests hit directly, and the reason raw-page churn (ads, nonces, comments,
    reshuffled chrome) is invisible downstream: none of it survives into this text."""
    p = _TextExtractor()
    p.feed(html)
    p.close()
    return p.text()


def fetch_url(url: str, *, timeout: float = FETCH_TIMEOUT_S) -> tuple[str, dict]:
    """Fetch `url` and return (extracted_text, meta) — meta is {url, final_url, content_type,
    http_status, fetched_at}. HTML (incl. xhtml) goes through `extract_html`; any other `text/*`
    passes through RAW (a served README/.txt is already reading flow — extraction would only
    mangle it); anything else refuses. Raises on every failure — bad scheme, HTTP error, timeout,
    over-cap, non-text — with a message, not a traceback: the caller (tap's Block driver) isolates
    a raising item as errored and the URL simply retries next run.

    The decode is errors="replace", the OPPOSITE of `tap.read_document`'s strict decode —
    deliberately: a file's true bytes are local and the owner can fix them, but a remote page's
    stray bytes are not ours to fix, and the store's artifact is the EXTRACTED text this function
    mints anyway — one replacement char in boilerplate must not error the whole page."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"--url speaks http(s); {url!r} does not (a local file is --file's job)")
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:       # follows redirects; HTTPError/URLError propagate
        body = resp.read(FETCH_MAX_BYTES + 1)
        if len(body) > FETCH_MAX_BYTES:
            raise ValueError(
                f"{url} exceeds FETCH_MAX_BYTES ({FETCH_MAX_BYTES} bytes) — a document source is "
                f"prose for review, not a dump; raise the cap in fetch.py if a page is genuinely "
                f"that large")
        # an ABSENT Content-Type reads as HTML: for a URL someone hands ratchet, a web page is the
        # overwhelmingly likely case (email.Message's own default, text/plain, would silently skip
        # extraction and ingest raw markup as prose).
        ctype = resp.headers.get_content_type() if resp.headers.get("Content-Type") else "text/html"
        charset = resp.headers.get_content_charset() or "utf-8"
        meta = {"url": url, "final_url": resp.geturl(), "content_type": ctype,
                "http_status": getattr(resp, "status", None), "fetched_at": config.now()}
    if ctype in ("text/html", "application/xhtml+xml"):
        return extract_html(body.decode(charset, errors="replace")), meta
    if ctype.startswith("text/"):
        return body.decode(charset, errors="replace"), meta
    raise ValueError(
        f"{url} is {ctype} — not text; refusing to ingest. PDF (and other binary formats) is a "
        f"future ADR; for now save the content to a text file and tap it with --file")
