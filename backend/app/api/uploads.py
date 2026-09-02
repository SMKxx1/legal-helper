"""Shared upload guards for the engine routes (review + support_task).

Both the ``/v1/reviews`` review path and the ``/v1/support_task/generate-nda`` generation path accept
internet-reachable ``.docx`` uploads, so they share the same decompression-bomb defence here rather
than each carrying its own copy.
"""

from __future__ import annotations

import io
import zipfile

from app.api.errors import EngineError

#: Reject a DOCX/zip whose parts inflate past this when decompressed (a tiny upload that explodes in
#: memory). 300 MB uncompressed is far above any real NDA yet well below an OOM.
ZIP_DECOMPRESS_CAP = 300 * 1024 * 1024


def guard_zip_bomb(data: bytes) -> None:
    """Raise :class:`EngineError` (413) if ``data`` is a zip whose parts decompress past the cap."""
    if data[:2] != b"PK":
        return  # not a zip container (txt/pdf) — nothing to guard here
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(i.file_size for i in zf.infolist())
    except zipfile.BadZipFile:
        return  # not a real zip; the parser will reject it normally
    if total > ZIP_DECOMPRESS_CAP:
        raise EngineError(
            413, "request_too_large", "Document expands too large to process."
        )
