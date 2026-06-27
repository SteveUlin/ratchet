"""Resolve ratchet's data root. Code lives in the repo; DATA lives elsewhere, local-only."""
from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    """`$RATCHET_DATA_DIR`, else `$XDG_DATA_HOME/ratchet`, else `~/.local/share/ratchet`."""
    env = os.environ.get("RATCHET_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "ratchet"


def ensure_layout(root: Path | None = None) -> Path:
    """Create the data subtree and return the root. `concepts/` is the curated-knowledge layer the
    human-review gate writes and `dream` reads (empty until review exists) — the source of truth that
    skills/CLAUDE.md are later generated *from*, kept distinct from that generated output."""
    root = root or data_root()
    for sub in ("blobs", "state", "tmp", "events", "concepts"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
