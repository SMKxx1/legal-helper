"""Nightly expiration sweep + scheduler wiring (PLAN §3.10 trigger b).

A cron job that walks the archive folder once a night and backfills / retries any signed NDA whose
expiration date isn't yet in Airtable — so it doubles as the BACKFILL for NDAs archived before this
feature existed and as the STRAGGLER catcher for archive-time extractions that failed (ERROR output, a
transient LLM/Airtable blip). Already-tracked NDAs (a non-empty date in Airtable) are skipped, so a
steady state costs one Airtable list + zero LLM calls.

Two cross-module seams, both DEFENSIVE (the archive agent builds them concurrently):

* **Archive source** — enumerating + downloading the archived PDFs is an archive-storage concern behind
  the provider protocol. The sweep depends on an injected :class:`ArchiveSource` (list + download) and
  resolves the production one via :func:`_resolve_archive_source`, which adapts the
  :class:`~app.integrations.storage.base.ArchiveStorage` provider (``list_folder(folder_id,
  mime_type=PDF)`` + ``download(id)``) over the destination archive folder (``drive_archive_folder_id``).
  Gated on the Google Drive config; if unavailable the sweep logs a greppable warning and no-ops
  (archive-time extraction still works).

* **Scheduler** — :func:`register_expiration_jobs` is called by the archive agent's
  ``app.worker.scheduler.run_worker`` (that module is theirs this wave — the one-line wiring is noted
  in the task open_items). It registers the cron at ``expiration_sweep_hour_utc``.

Capability gates (fail soft, PLAN §6): the sweep needs LLM inference (to extract), Google Drive (to
list/download), AND Airtable (to dedupe + persist). Any missing → the sweep logs and no-ops.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from ..integrations.airtable import (
    AirtableError,
    AirtableUnavailable,
    build_airtable_client,
)
from ..telemetry import get_logger
from .extractor import ExpirationUnavailable
from .service import process_pdf

log = get_logger("nda.expiration.jobs")


# --------------------------------------------------------------------------- #
# Archive-source seam (owned by the archive agent; injected in tests)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ArchivedFile:
    """One signed NDA in the archive folder, as the sweep needs it: a stable ref + a display name."""

    file_ref: str  # the Drive file id — the Airtable merge key
    display_name: str


@runtime_checkable
class ArchiveSource(Protocol):
    """Enumerate + fetch the archived PDFs. The archive agent's Drive-backed implementation satisfies
    this; tests inject a fake. ``list_pdfs`` already filters to signed-NDA PDFs (the watcher's skip
    filters — certificates, ``summary.pdf`` — are the archive agent's concern, not re-implemented here)."""

    def list_pdfs(self) -> Iterable[ArchivedFile]: ...

    def download(self, file_ref: str) -> bytes: ...


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SweepReport:
    """The tally of one sweep pass — surfaced in logs (and returned for tests/ops)."""

    scanned: int = 0
    skipped_tracked: int = 0
    written: int = 0
    no_date: int = 0
    failed: int = 0
    status: str = "ok"  # ok | llm_off | drive_off | airtable_off

    @property
    def processed(self) -> int:
        """Files an extraction pass was actually run on (scanned minus already-tracked skips)."""
        return self.written + self.no_date + self.failed


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def run_expiration_sweep(
    settings=None,
    *,
    source: ArchiveSource | None = None,
    registry=None,
    extract_transport: httpx.BaseTransport | None = None,
    airtable_transport: httpx.BaseTransport | None = None,
    drive_transport: httpx.BaseTransport | None = None,
) -> SweepReport:
    """Walk the archive folder once; extract + upsert any NDA missing an expiration date.

    Never raises (a worker job): every failure is caught and reflected in the returned
    :class:`SweepReport`. ``source`` overrides the archive seam (a fake in tests). The two
    ``*_transport`` seams inject ``httpx.MockTransport`` for the LLM + Airtable calls.
    """
    from ..config import get_settings

    settings = settings or get_settings()

    # --- capability gates (fail soft) ------------------------------------- #
    if not (settings.openrouter_api_key or "").strip():
        log.info("expiration.sweep.skipped", reason="llm_inference disabled")
        return SweepReport(status="llm_off")

    # Airtable is needed both to dedupe (list_tracked) and to persist; without it the sweep would
    # re-extract everything nightly with nowhere to write — so it stands down.
    try:
        airtable = build_airtable_client(
            settings, registry, transport=airtable_transport
        )
    except AirtableUnavailable as exc:
        log.info(
            "expiration.sweep.skipped", reason="airtable disabled", detail=str(exc)
        )
        return SweepReport(status="airtable_off")

    src = (
        source
        if source is not None
        else _resolve_archive_source(settings, registry, transport=drive_transport)
    )
    if src is None:
        log.warning(
            "expiration.sweep.skipped",
            reason="no archive source (app.archive drive seam unavailable)",
        )
        airtable.close()
        return SweepReport(status="drive_off")

    # --- dedupe set: file refs that already carry a date ------------------ #
    try:
        with airtable:
            tracked = {r.file_ref for r in airtable.list_tracked() if r.expiration_date}
    except AirtableError as exc:
        log.warning("expiration.sweep.list_failed", error=repr(exc))
        return SweepReport(status="airtable_off")

    scanned = skipped = written = no_date = failed = 0
    try:
        for f in src.list_pdfs():
            scanned += 1
            if f.file_ref in tracked:
                skipped += 1
                continue
            try:
                pdf_bytes = src.download(f.file_ref)
            except Exception as exc:  # noqa: BLE001 — one unreadable file must not abort the sweep
                failed += 1
                log.warning(
                    "expiration.sweep.download_failed",
                    file_ref=f.file_ref,
                    error=repr(exc),
                )
                continue
            try:
                outcome = process_pdf(
                    pdf_bytes,
                    file_ref=f.file_ref,
                    display_name=f.display_name,
                    settings=settings,
                    registry=registry,
                    extract_transport=extract_transport,
                    airtable_transport=airtable_transport,
                )
            except ExpirationUnavailable:
                # LLM turned off mid-sweep — nothing more to do this pass.
                log.info("expiration.sweep.llm_off_midway", scanned=scanned)
                break
            if outcome.status == "written":
                written += 1
            elif outcome.status == "no_date":
                no_date += 1
            else:  # airtable_off / airtable_error — the write failed
                failed += 1
    finally:
        # Close the Drive-backed source's httpx client (a real sweep opens one). Injected fakes may
        # not have close() — guard with getattr so tests pass a bare object.
        closer = getattr(src, "close", None)
        if callable(closer):
            closer()

    report = SweepReport(
        scanned=scanned,
        skipped_tracked=skipped,
        written=written,
        no_date=no_date,
        failed=failed,
    )
    log.info(
        "expiration.sweep.done",
        scanned=report.scanned,
        skipped_tracked=report.skipped_tracked,
        written=report.written,
        no_date=report.no_date,
        failed=report.failed,
    )
    return report


class _DriveArchiveSource:
    """Adapts an :class:`~app.integrations.storage.base.ArchiveStorage` provider to :class:`ArchiveSource`.

    Walks the DESTINATION archive folder (``drive_archive_folder_id`` — where the watcher files
    auto-named signed NDAs) and downloads by file id. ``file_ref`` is the provider file id, the same key
    the archive-time hook writes on, so the two never create competing Airtable rows.
    """

    def __init__(self, client, folder_id: str) -> None:
        self._client = client
        self._folder_id = folder_id

    def list_pdfs(self) -> Iterable[ArchivedFile]:
        from ..integrations.storage.base import PDF_MIME_TYPE

        for f in self._client.list_folder(self._folder_id, mime_type=PDF_MIME_TYPE):
            yield ArchivedFile(file_ref=f.id, display_name=f.name)

    def download(self, file_ref: str) -> bytes:
        return self._client.download(file_ref)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def _resolve_archive_source(
    settings, registry=None, *, transport: httpx.BaseTransport | None = None
) -> ArchiveSource | None:
    """Build a storage-backed :class:`ArchiveSource` over the archive folder, DEFENSIVELY (P4 gate).

    Adapts the :class:`~app.integrations.storage.base.ArchiveStorage` provider. Returns ``None`` — with a
    greppable warning, never a crash — when: the Google Drive config is absent (so no network is ever
    attempted), the capability is off, or the provider build fails. The ``transport`` seam lets a test
    drive the real adapter on ``httpx.MockTransport``.
    """
    # Config gate FIRST, so an unconfigured worker never constructs a client or touches the network.
    required = (
        "google_oauth_client_id",
        "google_oauth_client_secret",
        "google_oauth_refresh_token",
        "drive_archive_folder_id",
    )
    if not settings.is_configured(*required):
        log.info(
            "expiration.sweep.drive_unconfigured",
            missing=settings.missing_config(*required),
        )
        return None
    try:
        from app.integrations.storage.base import StorageUnavailable
        from app.integrations.storage.factory import get_archive_storage
    except Exception as exc:  # noqa: BLE001 — import defect: degrade rather than crash the worker
        log.warning(
            "expiration.sweep.no_storage_module",
            note="app.integrations.storage not importable — sweep inactive",
            error=repr(exc),
        )
        return None
    try:
        client = get_archive_storage(settings, registry, transport=transport)
    except StorageUnavailable as exc:
        log.info("expiration.sweep.drive_disabled", detail=str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 — a storage-auth/build failure must not crash the worker
        log.warning("expiration.sweep.drive_build_failed", error=repr(exc))
        return None
    return _DriveArchiveSource(client, settings.drive_archive_folder_id)


# --------------------------------------------------------------------------- #
# Scheduler registration (called by the archive agent's run_worker)
# --------------------------------------------------------------------------- #
def register_expiration_jobs(sched, settings) -> None:
    """Register the nightly expiration sweep on ``sched`` (an APScheduler instance).

    Called ONE line from ``app.worker.scheduler.run_worker`` (that module is the archive agent's this
    wave — the wiring is noted in task open_items). The cron fires at ``expiration_sweep_hour_utc``
    (UTC, minute 0). The job is registered UNCONDITIONALLY — the sweep gates on its capabilities
    internally and no-ops when any is off — so a later config change (adding the Airtable PAT / Drive
    creds) needs no re-registration. ``coalesce`` + ``max_instances=1`` keep a slow sweep from stacking.
    """
    hour = int(getattr(settings, "expiration_sweep_hour_utc", 2) or 0)
    sched.add_job(
        run_expiration_sweep,
        "cron",
        hour=hour,
        minute=0,
        id="expiration_sweep",
        coalesce=True,
        max_instances=1,
        kwargs={"settings": settings},
    )
    log.info("expiration.jobs.registered", sweep_hour_utc=hour)


__all__ = [
    "ArchivedFile",
    "ArchiveSource",
    "SweepReport",
    "run_expiration_sweep",
    "register_expiration_jobs",
]
