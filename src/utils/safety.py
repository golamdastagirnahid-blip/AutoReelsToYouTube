"""Safety helpers — file hashing, blocklist loading, basic guards."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
BLOCKLIST_FILE = _PROJECT_ROOT / "data" / "blocklist.txt"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Streamed SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


def load_blocklist() -> set[str]:
    """Load creator handles from data/blocklist.txt (one per line, # for comments)."""
    if not BLOCKLIST_FILE.exists():
        return set()
    out: set[str] = set()
    for line in BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.lower().lstrip("@"))
    return out


def filter_blocked(handles: Iterable[str]) -> list[str]:
    blocked = load_blocklist()
    return [h for h in handles if h.lower().lstrip("@") not in blocked]


def is_corporate_handle(handle: str | None, is_verified: bool | None) -> bool:
    """Heuristic to skip brand/corporate accounts.

    Real verification info comes from instaloader profile data.
    Fallback: keyword check for obvious brand handles.
    """
    if is_verified:
        return True
    if not handle:
        return False
    bad = ("official", "brand", "store", "shop", "media", "tv", "news",
           "company", "corp", "inc", "ltd")
    h = handle.lower()
    return any(b in h for b in bad)
