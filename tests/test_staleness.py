"""decay/staleness tests (ADR-0024): a QUIET concept — un-corroborated for longer than the disuse horizon,
never contradicted, just untouched — surfaces a `retire` PROPOSAL for the tier-2 human gate. RECALL-FIRST:
it FLAGS, never auto-retires; the human re-confirms (reject → kept + suppressed) or retires (accept → the
3c-i `retire` fires). The DETERMINISTIC sibling of recency-trust (ADR-0023): same valid-time signal, but on
the concept layer, and NO LLM. Exercised OFFLINE, fully DETERMINISTIC: session mtimes are fabricated RELATIVE
to an injected `now` (never absolute wall-clock dates that age with real time), and every staleness fn takes
that `now`.

The load-bearing properties:

  last-corroborated — the MOST-RECENT valid-time among a concept's evidence's sessions (a re-lived concept,
    with one fresh session, is NOT stale however old its other evidence); an undateable concept reads None →
    treated FRESH (recall-safe — never propose retiring what we cannot date).
  (1) STALE → a retire proposal — a concept past the horizon queues a `retire`-stale `garden_proposal` that
    surfaces in `pending_proposals`; a fresh / re-lived / undateable concept does NOT.
  (2) ACCEPT retires — `accept_proposal` applies the 3c-i `retire`; the concept drops from `valid_concepts`.
  (3) REJECT + SUPPRESSION — `reject_proposal` keeps the concept; a re-run does NOT re-queue (decision-sourced
    suppression — the gardener remembers the verdict, never re-nags).
  (4) SELF-CLEARING — a stale concept RE-CORROBORATED by a fresh session is no longer stale → no proposal.

Run: `python tests/test_staleness.py` (throwaway dir)."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-staleness-")

from ratchet import blobstore, config, dream, garden, review, weave  # noqa: E402
from ratchet.garden import STALENESS_DAYS  # noqa: E402


# A fixed reference instant the whole suite ages against — never `config.now()`, so no wall-clock flakiness.
# Every fabricated session mtime is `NOW − k days`; dates are RELATIVE to STALENESS_DAYS so a retune of the
# horizon can't silently flip a case.
NOW = "2026-06-28T00:00:00+00:00"
_NOW_DT = datetime.fromisoformat(NOW)
H = STALENESS_DAYS

STALE_MTIME = (_NOW_DT - timedelta(days=H + 30)).isoformat()    # past the horizon → stale
FRESH_MTIME = (_NOW_DT - timedelta(days=10)).isoformat()        # well inside the horizon → fresh
OLD_MTIME = (_NOW_DT - timedelta(days=H + 120)).isoformat()     # very old (a re-lived concept's stale half)
RECENT_MTIME = (_NOW_DT - timedelta(days=5)).isoformat()        # the re-lived concept's fresh half


# --- synthetic transcript → cleaned blob → concept (mirrors test_garden_propose's harness) ----------

def rec(uuid, parent, typ, **kw):
    r = {"type": typ, "uuid": uuid, "parentUuid": parent}
    r.update(kw)
    return r

def amsg(mid, *blocks):
    return {"role": "assistant", "id": mid, "content": list(blocks)}

def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def jsonl(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def seed_cleaned(tag, mtime, root):
    """Ingest a one-edit transcript carrying its VALID-TIME in `origin_ref.mtime` (the shape `tap.read_origin`
    stamps), weave it → a real cleaned blob a concept's evidence can cite. `mtime=None` omits the date — the
    undateable session (its valid-time reads None → the concept is treated fresh, recall-safe). The cleaned
    blob → `derived_from` (raw) → `source_id` (sess-tag) is the lineage `session_of` walks; the raw's
    `origin_ref.mtime` is the valid-time `_session_valid_times` folds."""
    recs = [rec(f"{tag}-u", None, "user", message={"role": "user", "content": f"edit {tag}"}),
            rec(f"{tag}-1", f"{tag}-u", "assistant",
                message=amsg(f"{tag.upper()}1", tool_use(f"{tag}t1", "Edit", file_path=f"/{tag}/x.py",
                                                         old_string="alpha", new_string="beta")))]
    origin = {"project": f"proj-{tag}", "session_id": f"sess-{tag}"}
    if mtime is not None:
        origin["mtime"] = mtime
    raw_h, _ = blobstore.ingest(jsonl(recs), source_kind="transcript", source_id=f"sess-{tag}",
                                origin_ref=origin, fetched_at=(mtime or NOW), root=root)
    ch, _, _ = weave.materialize(raw_h, root=root)
    return ch


def ev_ptr(event_id, cleaned_hash):
    return {"event_id": event_id, "cleaned_hash": cleaned_hash, "byte_start": 0, "byte_end": 3,
            "quote": "q", "context": "q"}


def mint(cid, title, cleaned_hashes, root):
    """A reviewed concept blob (review.accept's shape) citing one+ cleaned blobs as evidence."""
    evidence = [ev_ptr(f"ev-{cid}-{i}", ch) for i, ch in enumerate(cleaned_hashes)]
    concept = {"id": cid, "title": title, "statement": f"the {title} lesson",
               "evidence": evidence, "source_takeaway": f"t-{cid}"}
    blobstore.ingest(blobstore.canonical_json(concept), source_kind="concept", source_id=cid,
                     origin_ref={"stage": "test"}, root=root)


def use_store(prefix):
    d = tempfile.mkdtemp(prefix=f"ratchet-test-staleness-{prefix}-")
    os.environ["RATCHET_DATA_DIR"] = d
    return config.ensure_layout()


def valid_ids(root):
    return {c["id"] for c in review.valid_concepts(root)}

def stale_ids(root):
    return {s["concept"]["id"] for s in garden.stale_concepts(root, now=NOW)}

def concept_of(cid, root):
    return next(c for c in dream.load_concepts(root) if c["id"] == cid)


# === 0. last-corroborated = the most-recent valid-time; the horizon; undateable → fresh ==============

R = use_store("detect")
mint("c-stale", "quiet lesson", [seed_cleaned("stale", STALE_MTIME, R)], R)
mint("c-fresh", "live lesson", [seed_cleaned("fresh", FRESH_MTIME, R)], R)
# a RE-LIVED concept: one ancient session + one recent one — the recent one is its last-corroborated.
mint("c-relived", "re-lived lesson",
     [seed_cleaned("rel-old", OLD_MTIME, R), seed_cleaned("rel-new", RECENT_MTIME, R)], R)
mint("c-undated", "undateable lesson", [seed_cleaned("undated", None, R)], R)
mint("c-rej", "second quiet lesson", [seed_cleaned("rej", STALE_MTIME, R)], R)

vt = dream._session_valid_times(R)
assert garden.concept_last_corroborated(concept_of("c-stale", R), vt, R, now=NOW) == STALE_MTIME, \
    "a single-session concept's last-corroborated is that session's valid-time"
assert garden.concept_last_corroborated(concept_of("c-relived", R), vt, R, now=NOW) == RECENT_MTIME, \
    "a re-lived concept's last-corroborated is the MOST-RECENT of its evidence's sessions, not the oldest"
assert garden.concept_last_corroborated(concept_of("c-undated", R), vt, R, now=NOW) is None, \
    "an undateable concept (no session mtime) has NO last-corroborated → None (treated fresh downstream)"
# the horizon: only the genuinely-quiet concepts are stale; fresh / re-lived / undateable are not.
assert stale_ids(R) == {"c-stale", "c-rej"}, \
    f"only the past-horizon concepts are stale (fresh/re-lived/undateable excluded): {stale_ids(R)}"
print("OK §0 — last-corroborated = the newest evidence valid-time (re-lived ⇒ fresh); undateable ⇒ None ⇒ "
      "fresh; only past-horizon concepts are stale.")


# === 1. a STALE concept queues a retire-stale proposal that surfaces in pending_proposals ============

recs = garden.propose_stale(R, now=NOW)
by_cid = {r["concept_id"]: r for r in recs}
assert set(by_cid) == {"c-stale", "c-rej"}, f"propose_stale surfaces exactly the stale concepts: {set(by_cid)}"
assert all(not r["suppressed"] for r in recs), "the first pass queues fresh proposals (nothing suppressed yet)"

stale_pid = by_cid["c-stale"]["proposal_id"]
props = review.pending_proposals(R)
by_pid = {p["proposal_id"]: p for p in props}
assert stale_pid in by_pid, "the stale concept's retire proposal surfaces in the tier-2 pending_proposals queue"
sp = by_pid[stale_pid]
assert sp["op"] == "retire" and sp["params"]["concept_id"] == "c-stale", \
    f"the proposal is a retire targeting the quiet concept: {sp['op']} {sp['params']}"
assert sp["rationale"].startswith("stale:") and "Re-confirm or retire?" in sp["rationale"], \
    f"the rationale names DISUSE (re-confirm or retire), the human-facing justification: {sp['rationale']!r}"
assert sp["stakes"] == garden.op_stakes("retire") > garden.AUTO_APPLY_MAX_STAKES, \
    "a retire is HIGH-stakes → it QUEUES for the human gate, never auto-applies (recall-first)"
# a fresh / re-lived / undateable concept gets NO retire proposal.
retired_targets = {p["params"].get("concept_id") for p in props if p["op"] == "retire"}
assert retired_targets == {"c-stale", "c-rej"}, \
    f"no proposal for the fresh/re-lived/undateable concepts: {retired_targets}"
# the proposal is INERT: c-stale is still valid (nothing auto-retired for being quiet).
assert "c-stale" in valid_ids(R), "queuing a retire proposal does NOT retire the concept (recall-first)"
print("OK §1 — a stale concept queues a retire-stale proposal (high-stakes, in pending_proposals); the "
      "fresh/re-lived/undateable concepts do not; nothing auto-retires.")


# === 2. accept_proposal APPLIES the retire — the concept drops from valid_concepts ===================

res = review.accept_proposal(stale_pid, root=R, reviewer="sulin", assessment="confirmed abandoned")
assert res["status"] == "accepted" and res["op"] == "retire", f"the accept applied the retire op: {res}"
assert "c-stale" not in valid_ids(R), "accepting the proposal RETIRES the concept (drops from valid_concepts)"
assert stale_pid not in {p["proposal_id"] for p in garden.open_proposals(R)}, \
    "an accepted proposal leaves the open queue (decision-sourced lifecycle)"
print("OK §2 — accept_proposal applies the 3c-i retire; the quiet concept drops from the valid set.")


# === 3. reject_proposal keeps the concept + SUPPRESSES re-surfacing (no re-queue on re-run) ==========

rej_pid = by_cid["c-rej"]["proposal_id"]
review.reject_proposal(rej_pid, root=R, reviewer="sulin", reason="still relevant — keep it")
assert "c-rej" in valid_ids(R), "rejecting a retire proposal KEEPS the concept (reject ≠ retire)"
assert rej_pid not in {p["proposal_id"] for p in garden.open_proposals(R)}, \
    "a rejected proposal leaves the open queue"
# re-run the deterministic pass: c-rej is STILL stale, but the rejection SUPPRESSES a re-queue (no re-nag).
recs2 = garden.propose_stale(R, now=NOW)
rej2 = {r["concept_id"]: r for r in recs2}.get("c-rej")
assert rej2 is not None and rej2["suppressed"] is True, \
    "the still-stale rejected concept is detected but SUPPRESSED (the gardener remembers the verdict)"
assert rej_pid not in {p["proposal_id"] for p in garden.open_proposals(R)}, \
    "a re-run does NOT re-open the dismissed proposal — decision-sourced suppression holds"
print("OK §3 — reject keeps the concept; a re-run does NOT re-queue the dismissed retire (suppression).")


# === 4. SELF-CLEARING: a stale concept re-corroborated by a fresh session is no longer stale =========

R2 = use_store("self-clearing")
old_ch = seed_cleaned("rf-old", STALE_MTIME, R2)
mint("c-refresh", "lesson that gets re-lived", [old_ch], R2)
assert "c-refresh" in stale_ids(R2), "the concept starts STALE (its only evidence is past the horizon)"

# RE-CORROBORATE: a NEW concept version (latest-wins) adding a FRESH session's evidence — the loop's real
# re-acceptance, modeled as a re-ingest. Its last-corroborated advances to the fresh session → not stale.
fresh_ch = seed_cleaned("rf-new", RECENT_MTIME, R2)
mint("c-refresh", "lesson that gets re-lived", [old_ch, fresh_ch], R2)
assert "c-refresh" not in stale_ids(R2), \
    "fresh corroboration advances last-corroborated inside the horizon → no longer stale (self-clearing)"
assert garden.propose_stale(R2, now=NOW) == [], "a re-lived concept queues NO retire proposal"
assert garden.open_proposals(R2) == [], "and the tier-2 queue stays empty — nothing surfaced for it"
print("OK §4 — a stale concept re-corroborated by a fresh session clears itself (no proposal); re-living a "
      "still-true preference is what keeps it off the queue.")


print("\nall staleness tests passed.")
