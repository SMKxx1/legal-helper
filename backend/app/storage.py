"""Filesystem helpers for safely storing uploaded files.

Upload destinations are derived from a server-generated UUID plus the file's
sanitized extension only — the client-supplied filename is NEVER used to build
the on-disk path (it is kept separately in the DB for display). This prevents
path-traversal writes such as a filename of ``../../../../tmp/evil.pdf``.
"""

from __future__ import annotations

import uuid
from pathlib import Path


def safe_upload_path(base: Path, filename: str) -> Path:
    """Return a collision-free destination inside *base* for an uploaded file.

    The on-disk name is ``<uuid hex><lowercased suffix>`` — no component of the
    client-supplied *filename* (which may contain ``..`` or path separators)
    reaches the path. A defensive containment check guarantees the result can
    never escape *base*.
    """
    base = Path(base)
    suffix = Path(filename or "").suffix.lower()
    dest = base / f"{uuid.uuid4().hex}{suffix}"
    # Defense-in-depth: a uuid+suffix name cannot contain separators, but assert
    # containment so a future change can't silently reintroduce path escape.
    if not dest.resolve().is_relative_to(base.resolve()):
        raise ValueError("computed upload path escapes the base directory")
    return dest
