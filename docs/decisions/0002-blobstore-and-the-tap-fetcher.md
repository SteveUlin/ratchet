# 0002 — Blobstore: substrate, granularity, header data, versioning, and the tap fetcher

- Status: accepted
- Date: 2026-06-26
- Supersedes: 0001 §2 (per-chunk blob granularity)
- Superseded by: —

Informed by prior art in incremental web crawling, backup deduplication, web archiving
(Memento), and git-scraping (see References), and hardened by an adversarial review.

## Context

The first composable block is the **blobstore** and `tap`, the tool that fills it: locate
new/changed Claude Code transcripts in the datastore and copy them in. No chunking, no
extraction.

## Decisions

### Substrate = plain content-addressed files, not a database

Files are the source of truth. The prior-art consensus (git-scraping, restic/borg, WARC,
org-roam/Datasette) is uniform: a database buys only fast queries, at the cost of filesystem
immutability, greppability, no-daemon operation, and single-writer concurrency we get free.
The store is content-addressed files; **if** linear scan ever gets slow we add a
SQLite/Datasette index built **from** the files and deletable at will — never authoritative.
Deferred until a scan actually hurts.

### Format-agnostic blobstore + per-source adapters at the edges (narrow waist)

All format-specificity lives at the edges; the middle is uniform. **Fetchers (in)** — one
per source (`tap` is the transcript fetcher; `pull-github/slack/email/web` are future
siblings) produce `(source_kind, source_id, origin_ref, raw)`. **Blobstore (middle)** is
format-blind: raw text + a sidecar; `source_kind` tags format, `origin_ref` is the
source-specific backlink. **Renderers/extractors (out)** are per `source_kind`; deferred.

### Blob granularity = the fetched artifact (a whole transcript snapshot)

A blob is the raw content of one fetched artifact — for transcripts, the whole session
`.jsonl` at fetch time — not a pre-chunked window. (Supersedes ADR-0001 §2.)

### Meta sidecars are the single source of truth (no separate ledger)

Each blob has a sidecar `<hash>.meta.json` =
`{ content_hash, source_kind, source_id, origin_ref, fetched_at, prev, bytes }`. These
sidecars are authoritative: the per-source version history is **derived by scanning them**.
There is no separate tracking ledger to desync from the blobs. (`origin_ref` for a transcript
is `{ path, project, session_id, cwd, git_branch, size_bytes, mtime, lines }`.)

### Versioning is a Memento TimeMap, derived from sidecars

A logical source (`source_id`) has many time-stamped snapshots. Each sidecar is a memento
(`hash` + `fetched_at`); `prev` chains them; `latest_version(source_id)` scans sidecars and
returns the newest (ties broken deterministically by hash). This gives "all versions of X"
now and "X as of T" later.

### Crash-safety = content first, meta last (the commit marker)

A blob is written content-first, then the meta sidecar last, each via temp + atomic rename.
`has()` keys on the **meta**, so a crash after the content write but before the meta leaves
only a harmless orphan content file (invisible to every reader) that the next run overwrites
and commits. No partial commit ever corrupts the version history.

### Change detection = a fingerprint cascade with a mutable fetch-state cursor

`tap` keeps a mutable `state/fetch_state.json` (`path -> [size, mtime, hash]`) — separate from
the immutable blobs — and escalates cheap → robust:
1. `(size, mtime)` vs the cursor → skip without reading.
2. content `sha256` → if the blobstore already has it, skip (touch / revert).
3. else copy a new snapshot.
The cursor is updated on **every** processed path, including the dedup-skip, so a touched or
reverted file is re-read at most once, never forever.

### tap is robust to bad files

Each file is processed in isolation; an unreadable / deleted / permission-denied transcript
is counted as `errored` and the run continues. One bad file never aborts the sweep.

### Storage layout (in `$RATCHET_DATA_DIR`)

```
blobs/<hh>/<hash>               immutable content
blobs/<hh>/<hash>.meta.json     sidecar (the source of truth)
state/fetch_state.json          tap's mutable change-detection cursor
tmp/                            atomic-write scratch (swept on tap start)
```

## Known V0 limits (deferred, accepted)

- **Append-grow bloat.** An append-only transcript that gains a turn re-stores a near-
  duplicate snapshot. Fix: content-defined chunking (restic/borg/FastCDC) or an
  append-prefix delta.
- **Revert lag.** Reverting a source to a prior content hash is a content no-op, so
  `latest_version` reflects the last *distinct* content, not the live state. (No data loss.)
- **Concurrent taps may fork a version.** Two overlapping runs that both see a newly-changed
  source can mint two snapshots with the same `prev`. Both are valid immutable blobs; a future
  reader dedups by `(source_id, hash)`. "Idempotent" is per-content, not per-run.
- **UTF-8 only.** `read_origin` decodes with `errors="replace"`; non-UTF-8 sources are stored
  lossily. Transcripts are UTF-8 JSON, so this is fine for V0; bytes-level storage is the fix.
- **GC is best-effort.** `.partial` temps are swept on tap start; orphan content files (no
  meta) are harmless but not yet reclaimed.
- Chunking / active-path reconstruction and event generation (future blocks).

## References

- Incremental crawling — fingerprint (ETag/Last-Modified/checksum) skip:
  <https://stabler.tech/blog/how-to-perform-incremental-web-scraping>
- Content-defined chunking — restic foundations:
  <https://restic.net/blog/2015-09-12/restic-foundation1-cdc/>
- Memento / TimeMap (web-archiving versioning):
  <https://www.emergentmind.com/topics/memento-framework>
- Git scraping (snapshot an evolving source) — Simon Willison:
  <https://simonwillison.net/series/git-scraping/>
