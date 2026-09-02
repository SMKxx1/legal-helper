"""Playbook RELEASE identity — the content-cache version key (audit #3).

The review content caches (exact-bytes + normalized-text) used to key on (document, mode)
only, so a review graded under one playbook release was served forever after the playbook
changed — a stale result with no TTL and no version key. This module derives a short, stable
id for the CURRENTLY-RESOLVED playbook source so those caches only reuse a review that the
SAME release produced; a release change makes every legacy row miss (fresh review — correct).

Identity is the GLOBAL release, not the per-document variant: the cache lookups run BEFORE the
router picks a variant, so the key hashes the playbook SOURCE the whole engine is running against:
  * an explicit ``settings.engine_playbook_path`` override (tests / a single-file pin) -> that
    file's content;
  * else the v4 per-variant manifest (``playbook/v4/manifest.json``) -> the manifest bytes PLUS the
    bytes of every variant playbook + baseline file the manifest references. Hashing the manifest
    alone would miss a CONTENT edit to a variant JSON or a baseline ``.md`` (the mapping is
    unchanged), and the caches would keep serving reviews graded under the old content. Filenames
    are folded into the hash too, so a rename also changes the release.
  * else the legacy default v3 playbook.

Cached per resolved path (``lru_cache``): like the ``_v4_manifest`` cache in ``routes_v1``, a
PROCESS RESTART picks up on-disk playbook edits — matching the existing deploy invariant that a
new playbook build ships as a new process, so a running process never mixes two releases.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from app.config import settings

_REPO = (
    Path(__file__).resolve().parents[3]
)  # release.py -> playbook/ -> app/ -> backend/ -> repo
_DEFAULT_PLAYBOOK = _REPO / "playbook" / "playbook_nda_v3.json"
_V4_MANIFEST = _REPO / "playbook" / "v4" / "manifest.json"


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


@lru_cache(maxsize=8)
def _release_id_for_path(path: str) -> str:
    """sha256 of the file at ``path``, truncated to 16 hex chars, or "" when unreadable.

    A blank/missing source yields "" — persisted as NULL, which never matches a filtered
    lookup, so an unresolvable playbook falls through to a fresh review rather than reusing one
    keyed on an empty release.

    When ``path`` is the v4 manifest, the digest covers the manifest bytes AND every variant
    playbook + baseline file it references (see the module docstring) — so a content-only edit to
    any of them changes the release id.
    """
    if not path:
        return ""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    if Path(path) != _V4_MANIFEST:
        return _digest_bytes(raw)
    return _manifest_release_id(raw)


def _manifest_release_id(manifest_bytes: bytes) -> str:
    """Hash the manifest bytes plus the content of every referenced playbook/baseline file.

    Deterministic: referenced files are folded in sorted by their manifest-relative path, and each
    file's path is hashed alongside its bytes so a rename changes the id too. An unreadable
    referenced file contributes a sentinel so a later fix (making it readable) still shifts the id.
    """
    hasher = hashlib.sha256()
    hasher.update(manifest_bytes)
    refs: set[str] = set()
    try:
        man = json.loads(manifest_bytes)
        for entry in man.get("playbooks", []):
            if not isinstance(entry, dict):
                continue
            for key in ("playbook", "baseline"):
                rel = entry.get(key)
                if isinstance(rel, str) and rel.strip():
                    refs.add(rel)
    except (ValueError, TypeError):
        # Unparseable manifest: the manifest-bytes hash alone still yields a stable id.
        return _digest_bytes(hasher.digest())
    for rel in sorted(refs):
        hasher.update(b"\x00")
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        try:
            hasher.update((_REPO / rel).read_bytes())
        except OSError:
            hasher.update(b"<unreadable>")
    return _digest_bytes(hasher.digest())


def _resolved_source_path() -> str:
    """The playbook source the engine is currently grading against (see module docstring)."""
    override = getattr(settings, "engine_playbook_path", "") or ""
    if override:
        return override
    if _V4_MANIFEST.exists():
        return str(_V4_MANIFEST)
    return str(_DEFAULT_PLAYBOOK)


def playbook_release_id() -> str:
    """Short hex id of the resolved playbook release, or "" when the source is unreadable."""
    return _release_id_for_path(_resolved_source_path())
