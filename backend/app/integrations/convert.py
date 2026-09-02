"""Document → PDF conversion via bundled LibreOffice ``soffice`` (PLAN §3.10, Decisions row "docx→PDF").

The archive path PDF-normalizes any non-PDF attachment (``.docx`` / ``.doc`` / ``.rtf`` / ``.odt``)
before it goes to Google Drive so the cache-folder watcher only ever handles PDFs. The rebuild's
decision (PLAN §2, Decisions) is to reuse the engine image's already-bundled ``soffice`` via a
subprocess + timeout — NOT to run a second Gotenberg container as the old n8n stack did.

This is the one place that shells out to LibreOffice. It is deliberately tiny and dependency-free:

* isolation — input + output live in a per-call :class:`tempfile.TemporaryDirectory`, so nothing leaks
  between conversions and the client filename never reaches disk (the on-disk name is fixed);
* config — the binary (``SOFFICE_BIN``) and the wall-clock cap (``SOFFICE_TIMEOUT``) come from settings;
* typed errors — a MISSING binary is :class:`ConversionUnavailable` (the archive intent degrades to a
  friendly reply — treat conversion like a soft capability), a TIMEOUT is :class:`ConversionTimeout`,
  and a non-zero exit / missing output is :class:`ConversionError`. None of these ever leak a raw
  ``CalledProcessError`` / ``FileNotFoundError`` to the caller.

``soffice`` is invoked headless with a private user profile under the temp dir (``-env:UserInstallation``)
so concurrent worker conversions never fight over the shared ``~/.config/libreoffice`` lock — the classic
"only the first soffice in a burst succeeds" failure.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ..telemetry import get_logger

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger("nda.integrations.convert")

#: Suffixes ``soffice`` can convert to PDF for the archive path (everything else is passed through as-is
#: by the caller, or is already a PDF). Lower-case, dot-prefixed.
CONVERTIBLE_SUFFIXES = frozenset({".docx", ".doc", ".rtf", ".odt", ".txt"})


class ConversionError(RuntimeError):
    """Base for any docx→PDF conversion failure (non-zero exit, missing output, unreadable input)."""


class ConversionUnavailable(ConversionError):
    """``soffice`` is not installed / not on PATH — the feature is politely off (capabilities fail soft)."""


class ConversionTimeout(ConversionError):
    """``soffice`` exceeded the configured wall-clock cap (``SOFFICE_TIMEOUT``)."""


def _pdf_name(filename: str) -> str:
    """The output PDF basename LibreOffice writes: the input stem with a ``.pdf`` suffix."""
    stem = PurePosixPath(filename or "document").stem or "document"
    return f"{stem}.pdf"


def convert_to_pdf(
    data: bytes,
    *,
    filename: str = "document.docx",
    settings: Settings | None = None,
    soffice_bin: str | None = None,
    timeout_s: float | None = None,
) -> bytes:
    """Convert ``data`` (a ``.docx`` / ``.doc`` / ``.rtf`` / ``.odt`` / ``.txt``) to PDF bytes via ``soffice``.

    ``soffice_bin`` / ``timeout_s`` default to ``SOFFICE_BIN`` / ``SOFFICE_TIMEOUT`` from ``settings``
    (or the process settings). Raises :class:`ConversionUnavailable` when the binary is missing,
    :class:`ConversionTimeout` on the wall-clock cap, and :class:`ConversionError` on a non-zero exit or
    a missing/empty output PDF. The whole operation runs inside a private temp dir (input, output, and a
    per-call LibreOffice profile) so concurrent conversions never collide.
    """
    if not data:
        raise ConversionError("nothing to convert (empty input)")
    settings = settings or _get_settings()
    binary = (soffice_bin or settings.soffice_bin or "soffice").strip() or "soffice"
    timeout = float(timeout_s if timeout_s is not None else settings.soffice_timeout)

    # Keep a recognizable input extension so LibreOffice picks the right import filter, but never let
    # the client filename reach disk (only its suffix does).
    suffix = (PurePosixPath(filename or "document.docx").suffix or ".docx").lower()

    with tempfile.TemporaryDirectory(prefix="nda-convert-") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / f"input{suffix}"
        src.write_bytes(data)
        outdir = tmp_path / "out"
        outdir.mkdir()
        profile = tmp_path / "profile"

        cmd = [
            binary,
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(src),
        ]
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; binary from trusted settings
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            # The binary isn't installed / isn't on PATH — a soft capability failure, not a crash.
            raise ConversionUnavailable(
                f"LibreOffice binary {binary!r} not found; cannot convert to PDF"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ConversionTimeout(
                f"soffice conversion exceeded {timeout:.0f}s"
            ) from e

        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", "replace")[:400]
            log.warning(
                "convert.soffice_failed", returncode=proc.returncode, stderr=stderr
            )
            raise ConversionError(
                f"soffice exited {proc.returncode} converting {suffix} to PDF"
            )

        out_pdf = outdir / _pdf_name(src.name)
        if not out_pdf.exists():
            # LibreOffice sometimes names the output off the stem; take the sole produced .pdf if so.
            produced = sorted(outdir.glob("*.pdf"))
            if not produced:
                raise ConversionError("soffice produced no PDF output")
            out_pdf = produced[0]
        pdf_bytes = out_pdf.read_bytes()
        if not pdf_bytes:
            raise ConversionError("soffice produced an empty PDF")
        log.info(
            "convert.ok", suffix=suffix, in_bytes=len(data), out_bytes=len(pdf_bytes)
        )
        return pdf_bytes


def _get_settings() -> Settings:
    from ..config import get_settings

    return get_settings()


__all__ = [
    "convert_to_pdf",
    "ConversionError",
    "ConversionUnavailable",
    "ConversionTimeout",
    "CONVERTIBLE_SUFFIXES",
]
