"""temporal — the valid-time oracle: WHEN evidence happened, and what a belief is worth NOW
(recency trust ADR-0023, the maturity bar ADR-0027, sitting coalescing — the ADR-0028 backlog line).

Entrenchment is ratchet's one unit of belief: distinct supporting SITTINGS minus contradicting ones
(the ADR-0012 symmetry), each weighted by its VALID-TIME — when the conversation actually happened,
never when ratchet ingested it. Five modules read this fold (resolve's gates, review's queue,
synthesize's bar, the gardener's staleness pass, status's census) and no stage owns it; it grew up
inside dream v2 and moved here when dream was tombstoned (ADR-0032). Everything is
recompute-on-read (ADR-0013): session valid-times fold from the raw transcript metas in one scan,
sittings re-group at fold time, and nothing is ever stored on a takeaway or claim.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import blobstore, config

MATURITY_SESSIONS = 2                    # the raw distinct-session count kept for audit/back-compat (net_sessions)

# Recency-trust weighting (ADR-0023): entrenchment is weighted by each evidence's VALID-TIME (the session's
# date — when the conversation happened), so a months-long backfill of OLD conversations can neither
# re-entrench a stale takeaway nor strongly overturn a current one. "Newer = higher trust", a continuous
# curve, not a gate. Both dials are UNTUNED — they want a gold set, like every weight in dream.
RECENCY_HALF_LIFE_DAYS = 180.0          # UNTUNED — "how fast does the world change": the age at which a piece
                                        # of evidence is worth HALF a fresh one. A months scale (~6mo) so a
                                        # year-old corroboration counts ~1/4, a 2-year-old ~1/16. One edit retunes.
MATURITY_WEIGHT = 1.5                   # UNTUNED — the FLOAT review bar net_entrenchment must cross. Sits in the
                                        # integer gap [MATURITY_SESSIONS-1, MATURITY_SESSIONS): with FRESH evidence
                                        # (every weight ≈1, net_entrenchment integer-valued) `>= 1.5` is byte-
                                        # identical to the old `net_sessions >= 2`, so today's graduations are
                                        # preserved — yet it leaves headroom so mildly-aged-but-recent evidence
                                        # still matures (2 sessions a few weeks old don't fall under the bar).
COALESCE_HOURS = 12.0                   # UNTUNED — the SITTING window (hours). A /clear-split or crash-
                                        # restarted afternoon writes 2+ transcript files, which read as 2+
                                        # "distinct sessions" — so ONE sitting of work could mature a claim
                                        # at the ~2-session bar (the cheapest fake-maturity path; the
                                        # ADR-0028 backlog line, closed here). Wherever distinct sessions
                                        # are COUNTED (support, contradictions, maturity, resolve's
                                        # same-session gate), same-repo sessions whose valid-times fall
                                        # within this window coalesce into ONE sitting; different repos stay
                                        # distinct — a repo jump is a genuine context switch, the very
                                        # independence distinct-session support measures. 12h spans a long
                                        # working day; sleep is the natural sitting boundary. 0 = off (the
                                        # escape hatch); --coalesce-hours on resolve/review overrides per run.


def net_sessions(tk: dict) -> int:
    """NET distinct-session entrenchment, the RAW INTEGER COUNT (ADR-0012): supporting distinct sessions
    MINUS contradicting distinct sessions. Kept for AUDIT / back-compat and as the human-legible "sessions
    to go" signal (`incubating.needs`); the maturity GATE itself now reads the recency-WEIGHTED
    `net_entrenchment` (ADR-0023), which reduces to this count when all evidence is fresh. Read defensively:
    a dream/2 blob with no `contradictions` field reads as 0, so net == support and behaviour is identical
    to before any contradiction arrives. NO clamp: net may go NEGATIVE (a strongly-contested takeaway)."""
    sup = (tk.get("support") or {}).get("sessions", 0)
    con = (tk.get("contradictions") or {}).get("sessions", 0)
    return sup - con


def recency_weight(valid_time: str | None, now: str | None = None) -> float:
    """The recency TRUST of ONE piece of evidence, by its VALID-TIME age (ADR-0023): exponential decay,
    `0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)` — weight 1.0 at age 0, 0.5 at one half-life, halving every
    half-life thereafter and → 0 as the evidence recedes. "Newer = higher trust", a smooth curve, not a
    cliff. A missing/unparseable valid-time → 1.0: `config.age_days` degrades a missing stamp to 0.0 age →
    weight 1.0, so we treat undateable evidence as FRESH — RECALL-SAFE (never silently DISCOUNT evidence we
    can't date; the costly error is dropping a real learning). `now` is the reference instant (default real
    now); net_entrenchment pins one `now` and threads it so every session decays against the same clock."""
    return 0.5 ** (config.age_days(valid_time, now=now) / RECENCY_HALF_LIFE_DAYS)


def session_valid_times(root: Path | None = None) -> dict[str, str | None]:
    """session_id → its raw transcript's VALID-TIME — `origin_ref.mtime`, when the conversation ACTUALLY
    happened (the session's date), NOT when ratchet ingested it (`fetched_at`). The valid-time/transaction-
    time split is the whole point: a months-late backfill has a recent `fetched_at` but an OLD valid-time,
    and entrenchment must weight by the latter (ADR-0023).

    RECOMPUTE-ON-READ in ONE scan over the transcript metas (ADR-0013 ethos — never stored on the takeaway):
    a takeaway already lists its support/contradiction SESSION IDS (`sessions_seen`/`contradiction_evidence`),
    so the dates recompute from those exactly like `concepts._cleaned_facets` recompute facets from evidence
    — no date field to desync. The session id IS the raw transcript's `source_id`; a transcript appended-to
    over time has multiple versions, so keep the LATEST version's mtime (most recent session activity, the
    same recency fold `latest_version` does). A session with no transcript / no mtime → None → weight 1.0
    (fresh) downstream. Built ONCE per gate pass and threaded into `net_entrenchment` so the scan is paid
    once, not per takeaway."""
    root = root or config.data_root()
    best: dict[str, tuple[tuple[str, str], str | None]] = {}
    for m in blobstore.iter_meta(root):
        # both SESSION-bearing raw kinds: a transcript session is dated by its file's mtime, and a
        # DOCUMENT session (its stable path, ADR-0031) by the file's save time — its valid-time.
        # Latest version wins = the newest save, so a long-untouched rules file decays exactly like
        # an unlived lesson (documents joined decay tracking on purpose).
        if m.get("kind", "raw") != "raw" or m.get("source_kind") not in ("transcript", "document"):
            continue
        sid = m.get("source_id")
        if not sid:
            continue
        key = (m.get("fetched_at", ""), m.get("content_hash", ""))     # the latest-version recency fold
        if sid not in best or key > best[sid][0]:
            best[sid] = (key, (m.get("origin_ref") or {}).get("mtime"))
    return {sid: vt for sid, (key, vt) in best.items()}


def _parse_time(stamp) -> datetime | None:
    """An ISO valid-time as an AWARE datetime, or None (absent/unparseable) — the coalescing twin of
    `config.age_days`'s degrade path: a stamp we can't read must never crash the fold, it just refuses
    to coalesce. A naive stamp reads as UTC (tap stamps tz-aware; this guards hand-written bodies)."""
    if not stamp:
        return None
    try:
        ts = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def coalesce_sessions(session_ids, valid_times: dict, session_repos: dict, *,
                      hours: float = COALESCE_HOURS) -> list[list[str]]:
    """Group sessions into SITTINGS — the distinct-support unit the maturity fold counts. A /clear or
    a crash-restart splits one afternoon's work across 2+ transcript files; counting those as 2
    "distinct sessions" lets a single sitting mature a claim at the ~2-session bar (the ADR-0028
    backlog's cheapest fake-maturity path). So sessions sharing a REPO whose valid-times sit within
    `hours` of each other coalesce into ONE group — a greedy chain over the repo's sorted timeline:
    each session joins the current sitting if its gap FROM THE PREVIOUS session fits the window, so a
    long sitting extends session by session. Different repos stay distinct even minutes apart: a repo
    jump is a genuine context switch, the very independence distinct-session support measures. A
    session with NO known repo or NO parseable valid-time never coalesces (the sharing/ordering can't
    be established; recall-safe — never suppress support on doubt, the mirror of recency_weight's
    undated=fresh). `hours <= 0` is the OFF escape hatch: every session its own group, the exact
    pre-sitting counting. Deterministic (ids de-duped + sorted; timeline ties break by id), and
    recomputed at fold time from the threaded maps — nothing stored (ADR-0013)."""
    sids = sorted({s for s in session_ids if s})
    if hours <= 0:
        return [[s] for s in sids]
    groups: list[list[str]] = []
    by_repo: dict[str, list[tuple[datetime, str]]] = {}
    for s in sids:
        repo, ts = (session_repos or {}).get(s), _parse_time((valid_times or {}).get(s))
        if not repo or ts is None:
            groups.append([s])                     # unhomed/undateable: its own sitting, never merged
        else:
            by_repo.setdefault(repo, []).append((ts, s))
    for repo in sorted(by_repo):
        last: datetime | None = None
        for ts, s in sorted(by_repo[repo]):
            if last is not None and (ts - last).total_seconds() <= hours * 3600.0:
                groups[-1].append(s)               # within the window of the sitting's LAST activity
            else:
                groups.append([s])
            last = ts
    return groups


def _sitting_valid_time(group: list[str], valid_times: dict) -> str | None:
    """One sitting's valid-time = its LATEST member's stamp — the same most-recent-activity fold
    `session_valid_times` applies across one transcript's versions, lifted to the sitting: recency
    trust keys on when the sitting last touched the lesson. A lone undateable session yields None
    (→ weight 1.0 downstream, the recall-safe fresh default)."""
    stamps = [(ts, valid_times.get(s)) for s in group
              if (ts := _parse_time(valid_times.get(s))) is not None]
    return max(stamps)[1] if stamps else None


def same_sitting(session_id: str | None, others, valid_times: dict, session_repos: dict, *,
                 hours: float = COALESCE_HOURS) -> bool:
    """Would `session_id` coalesce into a sitting with ANY of `others`? Resolve's same-session gate
    reads THIS — one helper for the gate and the count, so the two can never disagree: an event from
    the second half of a split sitting must not adjudicate-corroborate the first half's claim, for
    exactly the reason the count groups them — its "yes" buys no distinct-sitting support. An exact
    same id is same-sitting at ANY hours (including the hours<=0 escape hatch, which restores the
    plain same-session membership test byte-exact)."""
    others = {s for s in others if s}
    if session_id in others:
        return True
    if not session_id or hours <= 0:
        return False
    for g in coalesce_sessions(others | {session_id}, valid_times, session_repos, hours=hours):
        if session_id in g:
            return len(g) > 1                      # any co-member is one of `others` (groups partition)
    return False


def net_entrenchment(tk: dict, now: str | None = None, *, valid_times: dict | None = None,
                     root: Path | None = None, coalesce_hours: float = COALESCE_HOURS) -> float:
    """The RECENCY-WEIGHTED net entrenchment the maturity gate now reads (ADR-0023) — the recency-aware
    replacement for `net_sessions` AT THE GATE, counted over SITTINGS (`coalesce_sessions`): same-repo
    sessions within `coalesce_hours` fold into one, so a /clear-split or crash-restarted afternoon
    cannot fake 2-session maturity. Each supporting DISTINCT SITTING contributes
    `recency_weight(the sitting's LATEST valid-time)`, each contradicting sitting SUBTRACTS the same
    (grouped identically — the ADR-0012 symmetry: a split sitting can no more fake a 2-session
    overturn than a 2-session graduation), all evaluated at one pinned `now`:

        Σ recency_weight(latest valid_time(g)) over support sittings  −  Σ over contradiction sittings

    So recent corroboration matures a takeaway while OLD-only corroboration decays below the bar (a stale
    backfill can't re-entrench), and a RECENT contradiction subtracts hard while a years-old one barely
    moves (a stale backfill can't overturn a current belief). The self-sorting consequence: a still-true
    preference keeps getting RE-LIVED — fresh evidence sustains its weight — while a moved-on fact simply
    stops being re-evidenced and fades on its own, so no timeless-vs-changing classification is needed.

    Reads the session-ID LISTS (`sessions_seen`; `contradiction_evidence[].session_id`) — the same ground
    truth `support.sessions`/`events.contradiction_stats` count — so with FRESH, cross-sitting evidence
    (every weight ≈1, nothing coalesces) this REDUCES to `net_sessions` as a float, and the gate behaves
    exactly as today. `net_sessions` (the raw int) is kept for audit/back-compat. Each session's REPO folds
    off the claim's own evidence (`_session_repos`, the in-memory field `resolve._fold_claim` derives —
    never stored, ADR-0013); a view without it (a v2 takeaway, a hand-built dict) coalesces nothing, so v2
    counting is byte-identical. `coalesce_hours=0` is the off switch — every session its own sitting, the
    exact pre-sitting sum. `valid_times` is built once per gate pass and threaded in; absent, it recomputes
    from `root`. `now` is pinned ONCE (default real now) so every sitting decays against the same
    reference (deterministic when a test injects it)."""
    now = now or config.now()
    if valid_times is None:
        valid_times = session_valid_times(root)
    repos = tk.get("_session_repos") or {}
    sup = {s for s in (tk.get("sessions_seen") or []) if s}
    con = {e.get("session_id") for e in (tk.get("contradiction_evidence") or []) if e.get("session_id")}

    def weight(ids) -> float:
        return sum(recency_weight(_sitting_valid_time(g, valid_times), now)
                   for g in coalesce_sessions(ids, valid_times, repos, hours=coalesce_hours))

    return weight(sup) - weight(con)
