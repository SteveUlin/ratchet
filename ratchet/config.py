"""Resolve ratchet's data root and stamp producer runs. Code lives in the repo; DATA lives
elsewhere, local-only."""
from __future__ import annotations

import getpass
import itertools
import os
from datetime import datetime, timezone
from pathlib import Path

_SEQ = itertools.count()   # per-process monotonic disambiguator (see run_id)


def now() -> str:
    """A wall-clock UTC timestamp — what producers stamp on artifact versions and decisions; the
    blobstore folds recency on `meta.fetched_at`, tie-broken by content_hash (ADR-0007)."""
    return datetime.now(timezone.utc).isoformat()


def age_days(stamp: str | None, now: str | None = None) -> float:
    """Wall-clock DAYS (fractional) between an ISO `stamp` and a reference instant — the wait-time AGE the
    `Aging` priority policy adds to a backlogged item's score (`effective = score + λ·age`, ADR-0021), and
    the VALID-TIME age the recency-trust weighting decays evidence by (ADR-0023). The blobstore stamps every
    version's recency on `meta.fetched_at`; this turns a stamp into "how long ago".

    `now` (ISO) injects the reference instant; default (None) is real wall-clock UTC. Recency weighting
    passes a FIXED `now` so its decay is deterministic and testable — every piece of evidence on one
    takeaway decays against the same reference, and a test never ages with real time. An unparseable/empty
    `now` falls back to real now (the same recall-safe degrade as a missing stamp), so injecting it can
    never crash the read.

    Degrades to 0.0 ("treat as FRESH") on a missing or unparseable `stamp` — a recency we can't read must
    NEVER crash the ordering/gate, and 0.0 just leaves that item un-boosted / un-discounted (the safe
    direction). A naive stamp is read as UTC (every producer stamps `now()`, which is tz-aware, so this only
    guards a legacy/hand-written body); a future-dated stamp (clock skew) clamps to 0.0 — negative age is
    meaningless."""
    if not stamp:
        return 0.0
    try:
        then = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    ref: datetime | None = None
    if now:
        try:
            ref = datetime.fromisoformat(now)
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            ref = None
    if ref is None:
        ref = datetime.now(timezone.utc)
    days = (ref - then).total_seconds() / 86400.0
    return days if days > 0.0 else 0.0


def run_id() -> str:
    """A unique, recency-sortable id per producer run: timestamp + pid + a ZERO-PADDED process-local
    counter + a small RANDOM suffix. Every part earns its place: `strftime` is only second-precision,
    so the counter disambiguates (and orders) runs within one process-second; the random suffix
    removes the last collision (a recycled pid in the same second across *sequential* processes resets
    the counter to 0). Recorded on every version/decision as provenance (origin_ref.run_id)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{os.getpid()}-{next(_SEQ):06d}-{os.urandom(2).hex()}"


def reviewer() -> str:
    """The operator a decision blob's provenance names — `$RATCHET_REVIEWER` if set, else the login
    user. Provenance identifies whoever ACTUALLY makes the call: the env var is the explicit override,
    the login name the honest default. A hardcoded name would forge every other user's audit trail —
    stamp their decisions with someone else's identity — so nothing is baked in. Resolved at CALL time,
    never frozen at import, so the answer tracks the running operator. `getpass.getuser` can raise where
    no login name resolves (a bare container, an unnamed uid); the last-ditch "reviewer" keeps
    provenance writable rather than crashing the decision on its identity field."""
    try:
        return os.environ.get("RATCHET_REVIEWER") or getpass.getuser()
    except Exception:
        return "reviewer"


def data_root() -> Path:
    """`$RATCHET_DATA_DIR`, else `$XDG_DATA_HOME/ratchet`, else `~/.local/share/ratchet`."""
    env = os.environ.get("RATCHET_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "ratchet"


def ensure_layout(root: Path | None = None) -> Path:
    """Create the data subtree and return the root. `blobs/` holds EVERY artifact — events, takeaways/
    claims, concepts, decisions, proposals — as versioned blobs (ADR-0007); `tmp/` is the blobstore's
    same-filesystem staging ground (`*.partial` write-then-rename, atomic because tmp and blobs share
    `root`). `state/` is deliberately NOT made here: it is tap's rebuildable fetch-cursor home, created
    on demand by `tap.save_fetch_state`."""
    root = root or data_root()
    for sub in ("blobs", "tmp"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
