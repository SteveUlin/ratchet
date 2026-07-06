# 0033 — the url source: fetched pages join the document layer

- Status: accepted — implemented 2026-07-05 (offline suites green, incl. the new `test_fetch`)
- Date: 2026-07-05
- Supersedes: —
- Extends: ADR-0031 (the document source — this cashes its "pilot for fetched sources" claim),
  ADR-0002 (tap the fetcher), ADR-0027 (explained knobs, no hidden rules).

Code (`fetch.fetch_url`/`extract_html`/`FETCH_MAX_BYTES`/`USER_AGENT`; `tap --url` /
`TapBlock(urls=…, fetcher=…)`/`_process_url`/`key()`'s per-run URL stamp; `blobstore.project_of`'s
url fallback) is the source of truth; this records the *why*.

## Context

ADR-0031 built the document source and named its successor in one sentence: "the pilot for fetched
sources (PDFs, webpages) — same mechanism, plus a fetcher." A page the owner trusts — a style
guide, a tool's docs, a postmortem — is a hand-written rules file that happens to live on someone
else's server: it asserts once, it never corroborates itself, its claims mature only through lived
sessions or a human accept. The document machinery already assumes nothing conversational
(verbatim ingest, passthrough render, paragraph turns, "most of this is signal" prompt), so what
was missing was exactly the fetcher — plus two pressures files never had:

1. **Raw pages churn.** Two fetches of the same article differ in bytes even when the prose is
   identical — ad slots, nonces, timestamps, CSRF tokens, reshuffled chrome. Content-addressing the
   raw HTML would mint a new "version" per fetch: churn masquerading as revision, flooding the
   version chain and re-running the whole prep pipeline for nothing.
2. **Extraction can lie invisibly.** HTML→text is a lossy reduction, and how it loses matters: the
   human gate reads the EVIDENCE, so the extraction's failure mode must be visible at review, not
   silent.

## Decision

**`tap --url URL` fetches a page (`fetch.fetch_url`, pure stdlib) and ingests its EXTRACTED TEXT
as the same `document` source ADR-0031 built; the URL is its source AND its session; the
extraction — not the raw HTML — is what gets fingerprinted.**

1. **URL-as-session — the epistemology, inherited whole.** `source_id = session_id = the URL,
   verbatim as the operator gave it` (no normalizer: `…/page` vs `…/page/` vs `…#section` is an
   aliasing question that belongs to the operator, not a canonicalization pass). All versions of
   one page are ONE session, so a page asserts its rules once no matter how often it is re-fetched;
   maturity comes only from lived sessions or review-accept — ADR-0031 §2 unchanged. No
   `project`/`cwd`: subject stays empty, scope derives global; the `--source` FOCUS handle is
   `origin_ref.url` (the `project_of` fallback chain grows one link). `--url` refuses the
   datastore-sweep selectors exactly as `--file` does (silently inert flags are hidden rules).

2. **EXTRACT, THEN FINGERPRINT — churn immunity by construction.** The stored raw blob IS the
   extracted text. Both dedup tiers (the cursor's recorded hash, the blobstore's content address)
   therefore see only the reading flow: a re-fetch whose ads/nonces/comments churned but whose
   prose didn't hashes identically and mints nothing; a real edit mints a new prev-linked VERSION
   of the same source (the edited-file rule, one level up). The owned cost: the raw HTML is NOT
   kept, so a better future extractor cannot re-run over past fetches — weave re-renders from raw,
   fetch cannot. Accepted deliberately: keeping the churning raw would either version-flood the
   store or demand a second fingerprint channel, and the extracted text is the evidence the human
   gate actually reads. ADR-0031's "the raw blob is the true bytes" still holds for what tap
   ingested — the extraction is the artifact; `origin_ref` records where and when it came from
   (`url`, `final_url`, `content_type`, `http_status`, `fetched_at`).

3. **A CONSERVATIVE extractor — no readability heuristic.** `extract_html` is a fixed drop-list
   (script/style/nav/header/footer/aside/noscript/svg/form, plus `title` — tab metadata) and a flat
   rendering of everything else: headings as `#`-lines, paragraphs/list items as lines, `<pre>`
   fenced verbatim, links as their anchor text (href in parens only when the text isn't already the
   URL), blocks joined by the exact `\n\n` separator weave's paragraph turns split on. No scoring,
   no boilerplate detection, no DOM heuristics — because a wrong-but-verbatim extraction (banner
   noise survives) is AUDITABLE at review, while a clever-but-lossy one (a scoring pass silently
   drops the paragraph a claim leans on) is not: noise costs a few wasted glean lines, silent loss
   costs the evidence. Malformed HTML degrades directionally: an unclosed chrome tag drops to EOF
   (in doubt about chrome, exclude — `strip_generated`'s posture), an unterminated `<pre>` keeps
   what it captured (in doubt about content, keep).

4. **The byte cap and the non-text refusal.** `FETCH_MAX_BYTES` (2 MiB, one edit to retune)
   refuses rather than truncates — a half-ingested document would glean half-claims silently; real
   article/docs pages run tens-to-hundreds of KB, so past the cap sits a dump or a binary behind a
   misdirected URL. Non-HTML `text/*` passes through RAW (a served README is already reading
   flow); anything else refuses with the reason and the workaround named (save to a file, `--file`)
   — PDF is a future ADR, said in the message, not hidden. The User-Agent is truthful
   (`ratchet/0.1 (+github…)`): a server operator seeing us in a log can look us up.

5. **A URL is never "done" — fetch every run.** A file has a stat; a page does not — whether it
   changed is knowable only by asking the server. So the done-key carries a per-run stamp (a stable
   key would let the driver's done-skip retire the URL after first ingest and silently drop every
   later change), every `tap --url` run fetches, and the processed marker stays Report cosmetics
   exactly as for files. The store-level no-op (§2) keeps the honest cadence cheap: asking is one
   GET; an unchanged answer writes nothing. `--dry-run` stays offline (the driver never reaches the
   fetch). A raising fetch — dead host, non-text, over-cap — is isolated per item by the Block
   driver (a message, never a traceback) and simply retried next run.

6. **The seams.** The fetcher is injectable (`TapBlock(fetcher=…)`), the narrowest test seam —
   offline tests drive the real extractor against an in-memory "server". The valid-time slot every
   document session is dated by (`origin_ref.mtime`, read by `temporal.session_valid_times`) is
   filled with the fetch instant: the honest analogue of a file's save time — when this content was
   last OBSERVED (the server's `Last-Modified` would claim authorship time, but servers omit or
   fake it; don't pretend). A never-re-fetched page thus decays exactly like an untouched rules
   file.

## Why this shape

- **Immunity by construction, not detection.** Filtering churn downstream (diffing raw versions,
  ignoring "small" changes) would be heuristic and fragile; making the fingerprinted artifact BE
  the reading flow means churn is structurally invisible — the same prevent-don't-detect posture as
  ADR-0031's render-time guard and ADR-0025's self-skip.
- **Auditable losses over invisible ones.** The extractor's whole design pressure is that its
  output faces a human gate. Conservative extraction pushes every failure toward the visible side:
  kept noise is rejected at review; nothing signal-bearing is silently gone.
- **Fetch-every-run is the truthful cadence.** Any scheme that skips the fetch (a stable done-key,
  a time-based backoff) encodes "the page didn't change" as an assumption; HTTP's own answer —
  conditional GET on a stored validator — is the only honest cheap tier, and the cursor entry
  already has the slot for it when it lands.
- **Seeding stays a human act.** URL claims incubate at 1 session by construction, exactly like
  file documents; the pipeline moves a trusted page's rules TO the gate, never past it.

## Consequences

- `tap --url https://… ` → weave → chunk → glean → resolve runs the ADR-0031 flow unchanged
  (RUNBOOK "Documents"): document render/chunkset formats, document glean prompt, exact-dup
  corroboration at zero added maturity, review seeding.
- Every `tap --url` run costs one GET per URL (dry-run excepted); unchanged pages write nothing.
  One decision-blob marker lands per URL per run — cosmetic, same as a touched file's.
- Deferred: **PDFs** (a future ADR: a real parser dependency or a text-layer extractor — the
  refusal message points at `--file` meanwhile); **JS-rendered pages** — a static fetch sees only
  the server's HTML, so a client-rendered app extracts to little or nothing; named honestly here
  rather than half-solved (a headless browser is out of the stdlib ethos entirely); **conditional
  GET** (ETag/Last-Modified in the cursor entry — the URL cheap tier); URL lists/feeds and a
  re-fetch registry (which pages to re-ask, how often) are ADR-0034's.
