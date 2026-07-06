# 0034 — the sources registry and `ratchet pull`: one command to sweep every source

- Status: accepted — implemented 2026-07-05 (offline suites green, incl. the new `test_sources`)
- Date: 2026-07-05
- Supersedes: —
- Extends: ADR-0031 (the document source), ADR-0033 (the url source — this cashes its closing
  "URL lists/feeds and a re-fetch registry are ADR-0034's"), ADR-0009 (the Block driver — `pull`
  reuses it, never re-implements a stage), ADR-0025 (tap's self-skip — `pull` inherits it),
  ADR-0027 (explained knobs, no hidden rules — the duplicate refusal).

Code (`sources.py`: the registry load/save/add/remove + `parse_feed`/`fetch_feed` + the per-feed
seen cursor + the `sources` CLI; `pull.py`: `resolve_plan`/`run` + the `pull` CLI) is the source of
truth; this records the *why*.

## Context

ADR-0031 and ADR-0033 gave ratchet three source kinds — transcripts (the datastore sweep), files
(`--file`), pages (`--url`) — each tapped by a separate hand-run. ADR-0033 closed by naming what it
deferred: "URL lists/feeds and a re-fetch registry (which pages to re-ask, how often) are
ADR-0034's." sulin's ask states the same pressure from the operator's side: "call a single function
to at least tap updated sources" — re-tap the projects dir, his registered `CLAUDE.md`, his
registered pages, and a polling feed (Anthropic's blog) — without standing up a daemon.

Two things were missing:

1. **A place to declare what the sources ARE.** The tap flags name a source per invocation; nothing
   records the STANDING set the operator wants swept every time. Without it, "pull my sources" is a
   shell script he maintains by hand, and a forgotten `--file` silently stops updating.

2. **A feed is not a page.** A blog's RSS/Atom is a *stream of new posts*, not a page to re-read:
   pulling it means fetching the feed, discovering the new entry links, and tapping each ONCE. A
   registered `url` is the opposite — a page you re-ask every pull to catch an edit. The two want
   different cursors, and neither existed.

## Decision

**A human-owned sources registry (`state/sources.json`) plus `ratchet pull`: one $0 command that
taps the implicit projects sweep + every registered file/url/feed, then runs the idempotent prep
(weave, chunk); `--max-usd C` adds a budgeted glean tick after prep.**

1. **The registry is CONFIG, not a cursor — and it lives beside one to make the distinction
   sharp.** `state/sources.json` sits in the same directory as tap's `fetch_state.json`
   (ADR-0002/0033) and means the OPPOSITE thing. The fetch cursor is a rebuildable optimization:
   lose it and tap re-reads each source once, no information gone. The registry is the operator's
   DECLARATION of which sources exist — lose it and you lose *intent*, which no derived pass can
   reconstruct. So the registry is his to hand-edit (a plain `{"sources": [...]}` object), and no
   code ever rewrites it except the `sources` verbs he drives. The per-feed SEEN cursor
   (`feed_state.json`) is the rebuildable half, kept in a SEPARATE file for exactly this reason: a
   crawler write must never reformat the human's config, and a hand-edit of the config must never
   clobber the crawler's memory. Same directory, opposite epistemology, disjoint files.

2. **Four kinds; `projects` is implicit.** A registry entry is `{"kind": "file", "path": …}` or
   `{"kind": "url"|"feed", "url": …}`. The transcripts sweep (`projects`) is NOT stored: `pull`
   ALWAYS sweeps the datastore, registry or no registry, so a fresh install with an absent
   `sources.json` still mines the owner's Claude Code sessions — the overwhelmingly common case
   needs zero setup. `sources --list` shows the implicit `projects` line beside the registered
   entries so the human sees the WHOLE plan, not just the parts he typed.

3. **A page is re-asked; a feed's entries are tapped once — two cursors, two epistemologies.**
   - A registered `url` is tapped every pull as an ADR-0033 `--url`: the page is re-fetched, and
     the extract-then-fingerprint cursor makes an unchanged page a store no-op (verified in
     `tap._process_url` — the stored blob IS the extracted text, so raw-HTML churn mints nothing).
     Re-asking is the point: you watch the page for edits.
   - A `feed` is fetched (`fetch_feed`, raw XML — NOT the HTML extractor, which would mangle the
     markup), parsed for its entry links (`parse_feed`, stdlib `xml.etree`, tolerating RSS 2.0
     `item/link` text and Atom `entry/link[@href]`), and only the NEW entries are tapped as `--url`
     documents. "New" is by ENTRY ID (RSS `guid` / Atom `id`, falling back to the link) against the
     per-feed seen set. An entry is marked seen only once its URL has a store version, so a
     transiently-dead entry link retries next pull — the same retry idiom the Block driver gives a
     dead `--url` (ADR-0033 §5), lifted to the feed level. Losing the feed cursor re-discovers every
     entry as "new" and re-taps them; each re-tap is a store no-op on unchanged content, so the cost
     is a few wasted GETs, never duplicate data — the rebuildable-state contract, honored.

4. **`pull` orchestrates by REUSING the Block driver, never re-implementing a stage.** `pull.run`
   builds the plan (§3), then drives the SAME `block.run` over the SAME `TapBlock`/`WeaveBlock`/
   `ChunkBlock`/`GleanBlock` a hand-run would (ADR-0009): the projects sweep is one `TapBlock`, the
   files + registered urls + feed-new-entry urls are one explicit-list `TapBlock` (the `files`/`urls`
   path), then `weave --all` and `chunk --all`. Every idempotency, cursor, per-item commit, and
   error-isolation guarantee is inherited, not copied. `pull` prints one honest summary line per
   stage (its own render, not the streaming bar) rather than four interleaved progress bars.

5. **$0 by default; the LLM spend is opt-in and budgeted.** `pull` with no flag never calls a model:
   tap/weave/chunk are all deterministic and free, so a bare `pull` is a safe, cheap "catch me up on
   everything" the operator can run without thinking about cost. `--max-usd C` ADDS a glean tick
   after prep, bounded by the same budget gate every LLM stage honors — one command can now go from
   "new posts on the wire" to "events extracted", still under a ceiling. resolve/synthesize/review
   stay separate hand-runs: they carry human judgment or real wall-clock, and folding them into a
   catch-up verb would hide that.

6. **Network honesty: degrade per source, never abort the sweep.** A source that touches the network
   fails in isolation. A dead FEED (its own fetch/parse) is caught in `resolve_plan`, logged, marked
   failed in the summary, and contributes zero entries — the sweep continues. A dead per-URL tap is
   isolated one level down by the Block driver (counted `errored`, no marker, retried next pull).
   `pull` inherits ADR-0033's raising-fetch idiom rather than re-inventing it.

7. **`--dry-run` is offline.** It enumerates the plan — implicit projects + every registered
   file/url/feed — and touches neither the network nor the store. Feed entries are NOT resolved
   (that needs a fetch), so a dry-run lists the feed SOURCES, noting entries resolve on a real pull.
   This is the honest "what would happen" a cautious operator wants before a real sweep.

8. **The module split: registry (leaf) vs orchestrator (root).** `sources.py` imports only `config`
   and `fetch` (for the shared User-Agent / byte cap / timeout) — it is a LEAF, so a test of the
   registry or the feed parser never drags in glean/completer. `pull.py` is the composition root: it
   imports every stage. Folding `pull` into `sources.py` would make the registry module pull in the
   whole pipeline; the split mirrors the existing `fetch.py` (primitive) / `tap.py` (stage) seam and
   keeps each half independently testable.

## Why this shape

- **CONFIG and CURSOR must not share a writer.** The one non-obvious call here is splitting
  `sources.json` (human) from `feed_state.json` (crawler). Co-locating them in one file — the
  tempting economy — means every crawler write re-serializes the human's declaration (reformatting
  his comments/order away) and every hand-edit races the crawler's next write. Separate files make
  the boundary structural, the same prevent-don't-detect posture as ADR-0031's render-time guard.
- **Two cursors because there are two questions.** "Did this page change?" (url) and "is this post
  new?" (feed entry) are different, and answering them with one mechanism forces one to lie. The
  url cursor asks the server every pull (the only honest way to see an edit); the feed cursor
  remembers what it has seen (the only cheap way to not re-fetch a settled archive). Each is the
  minimum honest mechanism for its question.
- **Reuse over re-implementation is the whole ADR-0009 bet.** `pull` is valuable precisely because
  it adds zero new stage logic — every guarantee it makes is one a tested Block already makes. A
  re-implemented mini-pipeline inside `pull` would be a second place for the done-skip or the
  per-item commit to drift.
- **$0-by-default keeps the catch-up verb safe.** The operator's ask is "tap updated sources" — an
  ingest, not a spend. Making the LLM tick opt-in means `pull` can be run reflexively (before every
  review sitting, say) without a cost calculation; the budget flag is there the moment he wants the
  extraction too.

## Consequences

- `ratchet sources --add-file ~/.claude/CLAUDE.md`, `--add-url …`, `--add-feed …` register standing
  sources; `ratchet pull` sweeps them all + the projects dir + runs prep, $0, in one command;
  `ratchet pull --max-usd 2` also gleans. The RUNBOOK "Pull — one command" section and the cadence
  row document the cadence.
- A registered feed re-taps only genuinely-new posts; a registered page is re-asked every pull and
  no-ops when unchanged; both degrade per source on a network failure.
- The registry is the operator's to hand-edit; the feed cursor is the crawler's to rebuild. Losing
  the cursor costs re-fetches, never data.
- Deferred: **conditional GET** (ETag/Last-Modified) for both url re-asks and feed fetches — the
  cheap tier ADR-0033 §5 already earmarked the cursor slot for; **a scheduler** (the cadence table
  is still documentation, not a daemon — `pull` is the single command a future scheduler would
  invoke); **per-source `--max-usd`/`--limit`** on the glean tick (one budget spans the whole tick
  today); **feed pagination** (only the feed's current window of entries is seen — an archive older
  than the feed's own cutoff is `--url`/`--file`'s job).
</content>
</invoke>
