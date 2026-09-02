"""Archive filename derivation (PLAN §3.10, reference §3.6/§3.11, §8) — pure, golden-testable.

Two naming conventions, ported verbatim from the n8n workflows so a rebuilt file lands under the exact
name the old system produced:

* :func:`archive_filename` — the ``archive`` intent's cache upload name (reference §3.6, §8):
  ``NDA_<sanitized original basename>.pdf``. The extension is stripped
  (``.docx`` / ``.doc`` / ``.pdf`` / ``.rtf`` / ``.odt``), non-``[A-Za-z0-9 _-]`` characters removed,
  trimmed, and ``.pdf`` re-appended (the file is always PDF-normalized before upload).
* :func:`cache_rename_filename` — the watcher's auto-name (reference §3.11, §8):
  ``<yyyyMMdd>_<clean(issuer)>_<mNDA|uNDA>_<clean(recipient)>.pdf`` where :func:`clean_party` removes
  commas/periods but KEEPS ``& ( ) -`` (the ported party-name cleaning).

Plus :func:`envelope_id_from_folder` — the ported ``envelopeId = folderName - 'Envelope_' prefix``
derivation the requester-DM seam keys on (reference §3.11, §7).

Everything here is a pure string function with no I/O, so the whole naming matrix is unit-tested with
zero dependencies.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

#: Document suffixes stripped off the original basename before ``NDA_…`` (reference §3.6 canonical name).
_ARCHIVE_EXT_RE = re.compile(r"\.(docx?|pdf|rtf|odt)$", re.IGNORECASE)
#: Characters kept in the archive intent name (reference §3.6): alphanumerics, space, underscore, hyphen.
_ARCHIVE_KEEP_RE = re.compile(r"[^A-Za-z0-9 _-]")
#: Characters kept in a cleaned party name (reference §3.11): alphanumerics, space, and ``& ( ) -``.
_PARTY_KEEP_RE = re.compile(r"[^A-Za-z0-9 &()\-]")
_WS_RE = re.compile(r"\s+")
#: A valid ported effective-date token (``yyyyMMdd``); anything else is treated as "no stated date".
_YYYYMMDD_RE = re.compile(r"^\d{8}$")

#: The two mutuality codes the watcher classifier emits / the rename uses (reference §3.11).
NDA_TYPE_MUTUAL = "mNDA"
NDA_TYPE_UNILATERAL = "uNDA"
_VALID_NDA_TYPES = frozenset({NDA_TYPE_MUTUAL, NDA_TYPE_UNILATERAL})

#: Fallback stem when the original basename sanitizes to nothing (a signed NDA still needs a name).
_ARCHIVE_FALLBACK_STEM = "document"


def archive_filename(original: str) -> str:
    """The ``archive`` intent's cache upload name: ``NDA_<sanitized basename>.pdf`` (reference §3.6, §8).

    Strips a known document extension, removes non-``[A-Za-z0-9 _-]`` characters, trims, and re-adds
    ``.pdf`` (the file is PDF-normalized before upload). An empty result falls back to ``document`` so a
    pathological name never yields the bare ``NDA_.pdf``.
    """
    base = _ARCHIVE_EXT_RE.sub("", original or "")
    base = _ARCHIVE_KEEP_RE.sub("", base).strip()
    if not base:
        base = _ARCHIVE_FALLBACK_STEM
    return f"NDA_{base}.pdf"


def clean_party(name: str) -> str:
    """Clean a party legal name for the auto-name (reference §3.11): drop commas/periods, KEEP ``& ( ) -``.

    Removes commas and periods first (the ported rule), then strips any character outside
    ``[A-Za-z0-9 &()-]``, and collapses runs of whitespace. Returns ``''`` for an empty/whitespace name.
    """
    s = (name or "").replace(",", "").replace(".", "")
    s = _PARTY_KEEP_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip()


def normalize_effective_date(value: str, *, today: str | None = None) -> str:
    """Return a ``yyyyMMdd`` date for the auto-name: a valid stated date, else today's UTC date.

    The watcher classifier emits the stated effective date (or the last-signature date) as ``yyyyMMdd``,
    or ``''`` when none is derivable (reference §3.11). A blank/malformed value falls back to today's UTC
    date so the auto-name never starts with a bare ``_`` — ``today`` is injectable for a stable golden."""
    v = (value or "").strip()
    if _YYYYMMDD_RE.match(v):
        return v
    return today or datetime.now(UTC).strftime("%Y%m%d")


def is_valid_nda_type(nda_type: str) -> bool:
    """True iff ``nda_type`` is exactly ``mNDA`` or ``uNDA`` (reference §3.11 — the classifier contract)."""
    return nda_type in _VALID_NDA_TYPES


def cache_rename_filename(
    *,
    effective_date: str,
    issuer: str,
    nda_type: str,
    recipient: str,
    today: str | None = None,
) -> str:
    """The watcher's auto-name (reference §3.11, §8): ``<yyyyMMdd>_<issuer>_<mNDA|uNDA>_<recipient>.pdf``.

    ``issuer`` / ``recipient`` are cleaned via :func:`clean_party`; ``effective_date`` is normalized to
    ``yyyyMMdd`` (today's UTC date when unstated). The caller is responsible for having validated that
    issuer / recipient / ``nda_type`` are all present (the ported "namingFailed" guard) BEFORE renaming —
    this function just composes the name.
    """
    date = normalize_effective_date(effective_date, today=today)
    return f"{date}_{clean_party(issuer)}_{nda_type}_{clean_party(recipient)}.pdf"


#: Ported verbatim from the DocuSign Cache Watcher's ``Build Cache File Name`` node (verified against
#: the live n8n workflow 2026-07-04): ``envelopeId = folderName.replace(/^Envelope[_ ]?/i, '')``.
#: DocuSign's native archive-to-Drive integration drops each completed envelope into a cache subfolder
#: named by the DocuSign Envelope GUID (usually the bare GUID; occasionally an ``Envelope_``/``Envelope ``
#: prefix). Case-insensitive, single optional ``_``/space separator, bare-GUID otherwise.
_ENVELOPE_PREFIX_RE = re.compile(r"^Envelope[_ ]?", re.IGNORECASE)


def envelope_id_from_folder(folder_name: str) -> str:
    """The DocuSign Envelope GUID from a cache SUBFOLDER name — the key the requester-DM seam looks up
    in ``nda_envelopes`` (reference §3.11, §7; the mapping the envelope intent writes).

    Strips an optional case-insensitive ``Envelope``/``Envelope_``/``Envelope `` prefix and returns the
    rest; a bare GUID (the common case) and the empty cache-root name pass through unchanged."""
    return _ENVELOPE_PREFIX_RE.sub("", (folder_name or "").strip())


__all__ = [
    "archive_filename",
    "clean_party",
    "normalize_effective_date",
    "is_valid_nda_type",
    "cache_rename_filename",
    "envelope_id_from_folder",
    "NDA_TYPE_MUTUAL",
    "NDA_TYPE_UNILATERAL",
]
