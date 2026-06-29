"""Resolve ratchet's data root and stamp producer runs. Code lives in the repo; DATA lives
elsewhere, local-only."""
from __future__ import annotations

import itertools
import os
from datetime import datetime, timezone
from pathlib import Path

_SEQ = itertools.count()   # per-process monotonic disambiguator (see run_id)


def now() -> str:
    """A wall-clock UTC timestamp — what producers stamp on artifact versions and decisions; the
    blobstore folds recency on `meta.fetched_at`, tie-broken by content_hash (ADR-0007)."""
    return datetime.now(timezone.utc).isoformat()


def age_days(stamp: str | None) -> float:
    """Wall-clock DAYS (fractional) between an ISO `fetched_at` stamp and now — the wait-time AGE the
    `Aging` priority policy adds to a backlogged item's score (`effective = score + λ·age`, ADR-0021). The
    blobstore stamps every version's recency on `meta.fetched_at`; this turns it into "how long has this
    item waited". Degrades to 0.0 ("treat as FRESH") on a missing or unparseable stamp — a recency we can't
    read must NEVER crash the ordering, and 0.0 just leaves that item un-boosted (the safe direction). A
    naive stamp is read as UTC (every producer stamps `now()`, which is tz-aware, so this only guards a
    legacy/hand-written body); a future-dated stamp (clock skew) clamps to 0.0 — negative age is meaningless."""
    if not stamp:
        return 0.0
    try:
        then = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - then).total_seconds() / 86400.0
    return days if days > 0.0 else 0.0


def run_id() -> str:
    """A unique, recency-sortable id per producer run: timestamp + pid + a ZERO-PADDED process-local
    counter + a small RANDOM suffix. Every part earns its place: `strftime` is only second-precision,
    so the counter disambiguates (and orders) runs within one process-second; the random suffix
    removes the last collision (a recycled pid in the same second across *sequential* processes resets
    the counter to 0). Recorded on every version/decision as provenance (origin_ref.run_id)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{os.getpid()}-{next(_SEQ):06d}-{os.urandom(2).hex()}"


def data_root() -> Path:
    """`$RATCHET_DATA_DIR`, else `$XDG_DATA_HOME/ratchet`, else `~/.local/share/ratchet`."""
    env = os.environ.get("RATCHET_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "ratchet"


def ensure_layout(root: Path | None = None) -> Path:
    """Create the data subtree and return the root. All four artifact kinds (event/takeaway/concept/
    decision) now live under `blobs/` as versioned blobs — the `events/` stream and `state/` ledger
    retired with runlog (ADR-0007 §5: the blobstore's content-then-meta commit is the only atomicity
    primitive). `concepts/` is the curated-knowledge layer the human-review gate writes and `dream`
    reads (empty until review exists) — the source of truth that skills/CLAUDE.md are later generated
    *from*, kept distinct from that generated output."""
    root = root or data_root()
    for sub in ("blobs", "tmp", "concepts"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
