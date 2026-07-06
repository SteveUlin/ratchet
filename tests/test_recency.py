"""recency-trust weighting tests (ADR-0023): entrenchment is weighted by each evidence's VALID-TIME (the
session's date — when the conversation actually happened), so a months-long backfill of OLD conversations
can neither re-entrench a stale takeaway nor strongly overturn a current one. Exercised OFFLINE, fully
DETERMINISTIC: session mtimes are fabricated RELATIVE to an injected `now` (never absolute wall-clock dates
that age with real time), and `recency_weight`/`net_entrenchment`/`current_takeaways` all take that `now`.

The load-bearing properties:

  recency_weight — exponential decay by valid-time age: 1.0 at age 0, 0.5 at one half-life, and 1.0 for a
    missing/unparseable date (recall-safe: discount, never silently drop, evidence we can't date).
  (a) BACKFILL CAN'T RE-ENTRENCH — a takeaway corroborated ONLY by OLD sessions (2× half-life back) has
    net_entrenchment BELOW the bar → does NOT graduate, while the SAME shape with FRESH sessions DOES.
  (b) BACKFILL CAN'T OVERTURN — against a fresh-mature takeaway, an OLD contradiction barely lowers
    net_entrenchment (stays mature) while a FRESH one drops it below the bar (un-graduates).
  (d) BACK-COMPAT — with FRESH evidence net_entrenchment == net_sessions, and the weighted gate graduates
    exactly the takeaways the old `net_sessions >= MATURITY_SESSIONS` count gate did.

Run: `python tests/test_recency.py`."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RATCHET_DATA_DIR"] = tempfile.mkdtemp(prefix="ratchet-test-recency-")

from ratchet import blobstore, config, dream, glean  # noqa: E402
from ratchet.dream import current_takeaways  # noqa: E402
from ratchet.temporal import (  # noqa: E402
    MATURITY_SESSIONS, MATURITY_WEIGHT, RECENCY_HALF_LIFE_DAYS,
    net_entrenchment, net_sessions, recency_weight)


# A fixed reference instant the whole suite decays against — never `config.now()`, so no wall-clock
# flakiness. Every fabricated session mtime is `NOW − k·half_life`, so the weights are exact powers of 0.5.
NOW = "2026-06-28T00:00:00+00:00"
HL = RECENCY_HALF_LIFE_DAYS
_NOW_DT = datetime.fromisoformat(NOW)


def at(days_before: float) -> str:
    """An ISO mtime `days_before` days before NOW (so its recency_weight at NOW is 0.5 ** (days_before/HL))."""
    return (_NOW_DT - timedelta(days=days_before)).isoformat()


def use_store(prefix):
    d = tempfile.mkdtemp(prefix=f"ratchet-test-recency-{prefix}-")
    os.environ["RATCHET_DATA_DIR"] = d
    return config.ensure_layout()


def seed_session(sid, mtime, root):
    """A bare raw transcript blob carrying the session's VALID-TIME in `origin_ref.mtime` (exactly the shape
    `tap.read_origin` stamps). net_entrenchment recomputes the date from here — session_id → raw → mtime."""
    blob = json.dumps({"session": sid, "filler": "λ"}) + "\n"
    blobstore.ingest(blob, source_kind="transcript", source_id=sid,
                     origin_ref={"session_id": sid, "mtime": mtime}, root=root)


def seed_takeaway(*, id, support_sessions, contradiction_sessions=(), confidence=0.8, root):
    """A v2-shape takeaway blob whose support/contradiction SESSION IDS the recency gate weights by date.
    `support_sessions`/`contradiction_sessions` are session ids (seed_session must have stamped their mtime)."""
    rec_ = {"id": id, "title": id, "why": f"why {id}",
            "relation": {"kind": "new", "concept_id": None, "note": ""},
            "cites": [f"{id}-e{i}" for i in range(len(support_sessions))], "evidence": [],
            "support": {"events": len(support_sessions), "sessions": len(set(support_sessions))},
            "sessions_seen": list(support_sessions),
            "contradicted_by": [f"{id}-c{i}" for i in range(len(contradiction_sessions))],
            "contradiction_evidence": [{"event_id": f"{id}-c{i}", "session_id": s}
                                       for i, s in enumerate(contradiction_sessions)],
            "contradictions": {"events": len(contradiction_sessions),
                               "sessions": len(set(contradiction_sessions))},
            "markers": {k: 0.0 for k in glean.MARKER_KINDS}, "confidence": confidence,
            "last_seen": NOW}
    blobstore.ingest(blobstore.canonical_json(rec_), source_kind="takeaway", source_id=id,
                     origin_ref={"stage": "dream", "model": "seed"}, root=root)
    return id


# === 0. recency_weight: the decay curve + the recall-safe missing-date fallback ======================

assert recency_weight(NOW, NOW) == 1.0, "age 0 → weight 1.0 (a session dated NOW is full-trust)"
assert recency_weight(at(HL), NOW) == 0.5, "one half-life → weight 0.5"
assert recency_weight(at(2 * HL), NOW) == 0.25, "two half-lives → weight 0.25 (halves each half-life)"
assert recency_weight(None, NOW) == 1.0, "a MISSING date → weight 1.0 (recall-safe: discount, never drop, the undateable)"
assert recency_weight("not-a-date", NOW) == 1.0, "an UNPARSEABLE date → weight 1.0 (same recall-safe fallback)"
assert recency_weight(at(-30), NOW) == 1.0, "a FUTURE date (clock skew) clamps to age 0 → 1.0 (never over-weights)"
# MATURITY_WEIGHT sits in the integer gap so fresh evidence reproduces the old `>= MATURITY_SESSIONS` gate.
assert MATURITY_SESSIONS - 1 < MATURITY_WEIGHT <= MATURITY_SESSIONS, \
    f"MATURITY_WEIGHT in [{MATURITY_SESSIONS-1}, {MATURITY_SESSIONS}] → fresh-integer gate == old count gate"
print("OK §0 — recency_weight: 1.0 at age 0, 0.5 at one half-life, 0.25 at two; missing/unparseable/future "
      "→ 1.0 (recall-safe); MATURITY_WEIGHT in the integer gap (fresh reproduces the old count gate).")


# === (a) BACKFILL CAN'T RE-ENTRENCH: old-only corroboration decays below the bar =====================

Ra = use_store("re-entrench")
# OLD takeaway: two distinct sessions, BOTH 2× half-life back → each weight 0.25 → net 0.5.
seed_session("a-old1", at(2 * HL), Ra)
seed_session("a-old2", at(2 * HL), Ra)
seed_takeaway(id="t-old", support_sessions=["a-old1", "a-old2"], root=Ra)
# FRESH takeaway: the SAME shape, two distinct sessions dated NOW → each weight 1.0 → net 2.0.
seed_session("a-new1", NOW, Ra)
seed_session("a-new2", NOW, Ra)
seed_takeaway(id="t-fresh", support_sessions=["a-new1", "a-new2"], root=Ra)

tk_old = next(t for t in dream.catalog(Ra) if t["id"] == "t-old")
tk_fresh = next(t for t in dream.catalog(Ra) if t["id"] == "t-fresh")
assert net_sessions(tk_old) == net_sessions(tk_fresh) == 2, "BOTH have raw net_sessions == 2 (the OLD count gate can't tell them apart)"
ne_old = net_entrenchment(tk_old, NOW, root=Ra)
ne_fresh = net_entrenchment(tk_fresh, NOW, root=Ra)
assert abs(ne_old - 0.5) < 1e-9, f"old-only corroboration weighs 2×0.25 = 0.5: {ne_old}"
assert abs(ne_fresh - 2.0) < 1e-9, f"fresh corroboration weighs 2×1.0 = 2.0: {ne_fresh}"
assert ne_old < MATURITY_WEIGHT <= ne_fresh, f"old below the bar, fresh above: {ne_old} < {MATURITY_WEIGHT} <= {ne_fresh}"
mature_ids = {t["id"] for t in current_takeaways(Ra, now=NOW)}
assert "t-fresh" in mature_ids and "t-old" not in mature_ids, \
    f"the backfill (old-only) does NOT graduate; the same shape fresh DOES: {mature_ids}"
print("OK §a — backfill can't re-entrench: two OLD-only support sessions (2× half-life) net 0.5 < the bar → "
      "NOT graduated, while the identical shape with FRESH sessions nets 2.0 → graduated (same net_sessions==2).")


# === (b) BACKFILL CAN'T OVERTURN: an old contradiction barely moves a fresh-mature takeaway ===========

Rb = use_store("overturn")
# A fresh-mature takeaway (two fresh support sessions → net 2.0).
seed_session("b-sup1", NOW, Rb)
seed_session("b-sup2", NOW, Rb)
# One OLD contradiction (2× half-life back → weight 0.25) and one FRESH contradiction (weight 1.0).
seed_session("b-con-old", at(2 * HL), Rb)
seed_session("b-con-fresh", NOW, Rb)
seed_takeaway(id="t-oldcontra", support_sessions=["b-sup1", "b-sup2"],
              contradiction_sessions=["b-con-old"], root=Rb)
seed_takeaway(id="t-freshcontra", support_sessions=["b-sup1", "b-sup2"],
              contradiction_sessions=["b-con-fresh"], root=Rb)

tk_oldc = next(t for t in dream.catalog(Rb) if t["id"] == "t-oldcontra")
tk_freshc = next(t for t in dream.catalog(Rb) if t["id"] == "t-freshcontra")
assert net_sessions(tk_oldc) == net_sessions(tk_freshc) == 1, "raw net_sessions = 2−1 = 1 for BOTH (the count gate can't tell them apart)"
ne_oldc = net_entrenchment(tk_oldc, NOW, root=Rb)
ne_freshc = net_entrenchment(tk_freshc, NOW, root=Rb)
assert abs(ne_oldc - (2.0 - 0.25)) < 1e-9, f"an OLD contradiction barely lowers net: 2.0 − 0.25 = 1.75: {ne_oldc}"
assert abs(ne_freshc - (2.0 - 1.0)) < 1e-9, f"a FRESH contradiction drops it hard: 2.0 − 1.0 = 1.0: {ne_freshc}"
assert ne_freshc < MATURITY_WEIGHT <= ne_oldc, \
    f"the fresh contradiction drops BELOW the bar; the old one stays above: {ne_freshc} < {MATURITY_WEIGHT} <= {ne_oldc}"
mature_b = {t["id"] for t in current_takeaways(Rb, now=NOW)}
assert "t-oldcontra" in mature_b and "t-freshcontra" not in mature_b, \
    f"the OLD contradiction can't overturn (still mature); the FRESH one un-graduates: {mature_b}"
print("OK §b — backfill can't overturn: against a fresh-mature takeaway, an OLD contradiction (2× half-life) "
      "nets 1.75 (stays mature) while a FRESH one nets 1.0 (drops below the bar → un-graduates).")


# === (d) BACK-COMPAT: fresh evidence reproduces the old net_sessions count gate exactly ===============

Rd = use_store("back-compat")
# Span net_sessions ∈ {0, 1, 2, 3}, ALL with fresh-dated sessions, and a 3-support / 1-contradiction case.
specs = [
    ("t-net0", [], []),
    ("t-net1", ["d1"], []),
    ("t-net2", ["d2a", "d2b"], []),
    ("t-net3", ["d3a", "d3b", "d3c"], []),
    ("t-net2c", ["d4a", "d4b", "d4c"], ["d4x"]),   # 3 support − 1 contradiction → net 2
]
seen = set()
for _id, sup, con in specs:
    for s in (*sup, *con):
        if s not in seen:
            seed_session(s, NOW, Rd)               # every session FRESH (dated NOW)
            seen.add(s)
    seed_takeaway(id=_id, support_sessions=sup, contradiction_sessions=con, root=Rd)

for _id, _sup, _con in specs:
    tk = next(t for t in dream.catalog(Rd) if t["id"] == _id)
    ne = net_entrenchment(tk, NOW, root=Rd)
    assert abs(ne - net_sessions(tk)) < 1e-9, f"{_id}: fresh net_entrenchment == net_sessions ({ne} vs {net_sessions(tk)})"

weighted_gate = {t["id"] for t in current_takeaways(Rd, now=NOW)}                       # the new float gate
count_gate = {t["id"] for t in dream.catalog(Rd) if net_sessions(t) >= MATURITY_SESSIONS}  # the OLD count gate
assert weighted_gate == count_gate == {"t-net2", "t-net3", "t-net2c"}, \
    f"fresh: the weighted gate graduates EXACTLY the old count gate's set: {weighted_gate} vs {count_gate}"
print("OK §d — back-compat: with FRESH evidence net_entrenchment == net_sessions for every net ∈ {0,1,2,3}, "
      "and the weighted gate graduates EXACTLY the takeaways the old `net_sessions >= MATURITY_SESSIONS` did.")


print("\nall recency tests passed.")
