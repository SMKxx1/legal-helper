"""Archive-time trigger — extract + upsert when a signed NDA is archived (PLAN §3.10 trigger a).

The archive agent's watcher (and the archive intent) fan every successfully-filed signed NDA out to the
post-archive registry it owns (``app.archive.hooks.register_on_archived`` / its ``on_archived``
emitter). This module subscribes :func:`extract_on_archived` there, so a freshly-archived NDA gets its
expiration date the moment it lands (the nightly sweep then only mops up stragglers).

The seam contract (owned by the archive agent, integrated here)::

    app.archive.hooks.register_on_archived(hook)     # idempotent per callable
    hook(archived_file: app.archive.hooks.ArchivedFile) -> None

where ``ArchivedFile`` already carries the ``pdf_bytes`` the watcher downloaded for classification, the
final filed ``file_name``, and the destination ``file_id`` — so extraction runs without re-fetching
Drive. :func:`extract_on_archived` reads those, keys the Airtable row on the destination ``file_id``
(falling back to the cache ``source_file_id`` / the name), and returns an
:class:`~app.expiration.service.ExpirationOutcome`. It never raises — a hook defect must not disturb the
archive that already succeeded (the archive agent's fan-out also swallows, belt-and-braces).

:func:`register_archive_hook` imports the archive registry DEFENSIVELY (the two agents build
concurrently): if it isn't importable it logs a greppable warning and returns ``False`` WITHOUT raising,
so worker boot never breaks and the nightly sweep still covers archive-time drops. The one-line worker
wiring (call this at ``run_worker`` boot, and in ``create_app`` for the in-process archive-intent path)
is noted in the task open_items.
"""

from __future__ import annotations

from typing import Any

from ..telemetry import get_logger
from .service import ExpirationOutcome, process_pdf

log = get_logger("nda.expiration.hooks")


def extract_on_archived(archived: Any) -> ExpirationOutcome:
    """Post-archive subscriber: run extract→upsert for a just-filed NDA. Never raises.

    ``archived`` is an ``app.archive.hooks.ArchivedFile`` (duck-typed here so this module carries no
    load-time import of the archive package). The Airtable merge key is the destination Drive
    ``file_id`` when present — the same id the sweep keys on, so the archive-time write and a later
    sweep converge on ONE row — falling back to the cache ``source_file_id`` and then the file name.
    """
    file_ref = (
        getattr(archived, "file_id", "")
        or getattr(archived, "source_file_id", "")
        or getattr(archived, "file_name", "")
    )
    display_name = getattr(archived, "file_name", "") or file_ref
    pdf_bytes = getattr(archived, "pdf_bytes", b"") or b""
    try:
        outcome = process_pdf(pdf_bytes, file_ref=file_ref, display_name=display_name)
        log.info(
            "expiration.on_archived",
            file_ref=file_ref,
            status=outcome.status,
            date=outcome.date,
        )
        return outcome
    except Exception as exc:  # noqa: BLE001 — must never break the archive that triggered it
        log.warning("expiration.on_archived.failed", file_ref=file_ref, error=repr(exc))
        return ExpirationOutcome(
            file_ref=file_ref,
            date=None,
            upserted=False,
            status="hook_error",
            detail=repr(exc),
        )


def _archive_subscriber(archived: Any) -> None:
    """The exact ``Callable[[ArchivedFile], None]`` the archive registry expects — a stable,
    module-level object (so re-registration de-dupes) that runs :func:`extract_on_archived` and
    discards its outcome (the outcome is for direct callers/tests; the fan-out ignores return values)."""
    extract_on_archived(archived)


def register_archive_hook(settings=None) -> bool:
    """Subscribe the archive-time extractor to the archive agent's post-archive registry.

    DEFENSIVE (PLAN P4 gate): if ``app.archive.hooks.register_on_archived`` isn't importable — the
    archive agent builds it concurrently — this logs a greppable warning and returns ``False`` WITHOUT
    raising, so the worker still boots and the nightly sweep still runs. Idempotent (the registry
    de-dupes the same callable — :func:`_archive_subscriber`), so calling it from both ``run_worker``
    and ``create_app`` subscribes once. Returns ``True`` when subscribed.
    """
    try:
        from app.archive.hooks import register_on_archived
    except Exception as exc:  # noqa: BLE001 — module not built yet / import defect: degrade, don't crash
        log.warning(
            "expiration.register_archive_hook.unavailable",
            note="app.archive.hooks.register_on_archived not importable — archive-time "
            "extraction not wired (nightly sweep still covers it); see task open_items",
            error=repr(exc),
        )
        return False

    register_on_archived(_archive_subscriber)
    log.info("expiration.register_archive_hook.ok")
    return True


__all__ = ["extract_on_archived", "register_archive_hook"]
