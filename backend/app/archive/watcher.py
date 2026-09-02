"""The Drive cache-folder watcher (PLAN §3.10, reference §3.11) — worker-scheduled auto-namer.

A behavioral port of the n8n "DocuSign NDA: Cache Folder Watcher (Auto-Name v2)" workflow. It runs on
the worker's schedule (``app.worker.scheduler``, capability-gated, ``watcher_interval_minutes`` — the
OLD 1-minute-vs-5 bug fixed, reference §3.11/§5) and, per pass:

1. resolves the Drive **cache** folder by name (``drive_cache_folder_name``);
2. lists the cache root + each ``Envelope_<id>`` subfolder, and every PDF within
   (``Build Folder List`` → ``List PDF Files in Folder``);
3. **skip filters (ported verbatim)** — a name containing ``certificate of completion`` or equal to
   ``summary.pdf`` is skipped (reference §3.11 ``Pair & Filter Files``);
4. **claims** each new file — an ``INSERT … ON CONFLICT (file_id) DO NOTHING`` dedup, here the portable
   insert-and-catch in :func:`app.archive.models.claim_cache_file` (Drive ``file_id`` is the PK), so a
   file is processed exactly once no matter how often the watcher polls;
5. downloads it, extracts text, and **classifies** issuer / recipient / mutuality on the cheap LLM alias
   (:mod:`app.archive.classify`) — a classify failure or an incomplete result is the ported
   ``namingFailed`` path (file kept under its original name);
6. renames to the ported ``<yyyyMMdd>_<issuer>_<mNDA|uNDA>_<recipient>.pdf`` (:mod:`app.archive.naming`);
7. **duplicate-checks** the destination archive folder (``drive_archive_folder_id``) by that name — an
   existing copy is ``duplicate_skipped`` + notified, never re-uploaded;
8. otherwise **files a copy** into the destination folder and records the ported status lifecycle
   (``renamed`` / ``saved_default_name``; ``failed`` on a download/upload error — the file is LEFT in the
   cache for the next pass, the ported "Never deletes from cache" invariant);
9. runs the **requester-DM seam** (STUBBED — see :func:`_dm_requester_seam`) and the **archive fan-out**
   (:func:`app.archive.hooks.on_archived`) the expiration extractor subscribes to.

Named fix (reference §3.11 / §9): the old workflow string-interpolated Drive ids / file names into
several SQL queries with hand-rolled sanitization. Here EVERY database access is parameterized
SQLAlchemy (:mod:`app.archive.models`).

Every collaborator (the :class:`~app.integrations.storage.base.ArchiveStorage` provider, the classifier,
the text extractor, the admin notifier, the requester-DM sender, the clock) is an injected parameter, so
the whole watcher matrix runs with fakes and **zero network / zero LLM** (PLAN house rules). The scheduler
wrapper resolves the production defaults. Drive/SharePoint specifics live behind the provider protocol
(:func:`~app.integrations.storage.factory.get_archive_storage`) — the watcher never sees an SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..integrations.storage.base import (
    FOLDER_MIME_TYPE,
    PDF_MIME_TYPE,
    ArchiveStorage,
    StorageEntry,
    StorageError,
    StorageUnavailable,
    StoredFile,
)
from ..integrations.storage.factory import get_archive_storage
from ..telemetry import get_logger
from .classify import CacheClassification, Classifier
from .hooks import ArchivedFile, on_archived
from .models import (
    STATUS_DUPLICATE_SKIPPED,
    STATUS_FAILED,
    STATUS_RENAMED,
    STATUS_SAVED_DEFAULT_NAME,
    claim_cache_file,
    mark_cache_status,
)
from .naming import cache_rename_filename, envelope_id_from_folder

if TYPE_CHECKING:
    from app.config import Settings

log = get_logger("nda.archive.watcher")

#: The ported skip substrings (reference §3.11 ``Pair & Filter Files``). Case-insensitive.
_SKIP_SUBSTRING = "certificate of completion"
_SKIP_EXACT = "summary.pdf"

#: Text extractor seam: ``(filename, data) -> str``. Defaults to the engine's ``routes_v1._extract_text``.
TextExtractor = Callable[[str | None, bytes], str]
#: Admin-channel notifier seam: ``(text) -> None`` (fail-soft). Defaults to the wired delivery + admin channel.
AdminNotifier = Callable[[str], None]
#: Requester-DM seam: ``(context, text) -> None`` (fail-soft). See :func:`_dm_requester_seam`.
RequesterDM = Callable[[dict[str, Any], str], None]


@dataclass
class WatchStats:
    """The outcome of one watcher pass — counters for the worker log + the tests' assertions."""

    disabled: bool = False
    scanned: int = 0
    skipped: int = 0
    claimed: int = 0
    renamed: int = 0
    saved_default_name: int = 0
    duplicate_skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "disabled": self.disabled,
            "scanned": self.scanned,
            "skipped": self.skipped,
            "claimed": self.claimed,
            "renamed": self.renamed,
            "saved_default_name": self.saved_default_name,
            "duplicate_skipped": self.duplicate_skipped,
            "failed": self.failed,
        }


@dataclass
class _Ctx:
    """Resolved collaborators for one watcher pass (bundled so ``_process_file`` stays a pure function)."""

    settings: Settings
    session_factory: Any
    drive: ArchiveStorage
    classify: Classifier | None
    extract_text: TextExtractor
    notify_admin: AdminNotifier
    dm_requester: RequesterDM
    dest_folder_id: str
    today: str
    now: datetime


def should_skip(name: str) -> bool:
    """The ported skip filter (reference §3.11): a ``certificate of completion`` name, or ``summary.pdf``."""
    low = (name or "").strip().lower()
    return _SKIP_SUBSTRING in low or low == _SKIP_EXACT


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_watch_once(
    *,
    settings: Settings | None = None,
    session_factory: Any | None = None,
    registry: Any | None = None,
    drive: ArchiveStorage | None = None,
    classify: Classifier | None = None,
    extract_text: TextExtractor | None = None,
    notify_admin: AdminNotifier | None = None,
    dm_requester: RequesterDM | None = None,
    now: datetime | None = None,
) -> WatchStats:
    """Run ONE watcher pass over the cache folder (PLAN §3.10, reference §3.11). Returns :class:`WatchStats`.

    Capability-gated (PLAN §6): a disabled/unhealthy GOOGLE_DRIVE capability (or an unconfigured Drive
    client) makes this a clean no-op (``disabled=True``), never an error. Every collaborator is optional
    and resolved to its production default when omitted; tests inject fakes for all of them so the pass
    runs with zero network + zero LLM.
    """
    settings = settings or _get_settings()
    stats = WatchStats()

    if drive is None:
        registry = registry or _build_registry(settings)
        from ..capabilities import GOOGLE_DRIVE, CapabilityState

        if registry.state(GOOGLE_DRIVE) is not CapabilityState.ENABLED:
            log.info(
                "archive.watcher.disabled", reason=registry.get(GOOGLE_DRIVE).reason
            )
            stats.disabled = True
            return stats
        try:
            drive = get_archive_storage(settings, registry)
        except StorageUnavailable as exc:
            log.info("archive.watcher.disabled", reason=str(exc))
            stats.disabled = True
            return stats

    dest_folder_id = (settings.drive_archive_folder_id or "").strip()
    if not dest_folder_id:
        # The capability requires it, so this only trips if a caller injected a drive but left it blank.
        log.warning("archive.watcher.no_destination")
        stats.disabled = True
        return stats

    now = now or datetime.now(UTC)
    ctx = _Ctx(
        settings=settings,
        session_factory=session_factory or _default_session_factory(),
        drive=drive,
        classify=classify if classify is not None else _default_classifier(settings),
        extract_text=extract_text or _default_extract_text(),
        notify_admin=notify_admin or _default_notify_admin(settings),
        dm_requester=dm_requester or _default_dm_requester(settings),
        dest_folder_id=dest_folder_id,
        today=now.strftime("%Y%m%d"),
        now=now,
    )

    try:
        folders = _list_folders(ctx)
    except StorageError as exc:
        log.warning("archive.watcher.scan_failed", error=repr(exc))
        return stats

    for folder_id, folder_name in folders:
        try:
            pdfs = ctx.drive.list_folder(folder_id, mime_type=PDF_MIME_TYPE)
        except StorageError as exc:
            log.warning(
                "archive.watcher.list_failed",
                folder=folder_name or "<root>",
                error=repr(exc),
            )
            continue
        for pdf in pdfs:
            _handle_candidate(ctx, pdf, folder_name, stats)

    log.info("archive.watcher.pass_complete", **stats.as_dict())
    return stats


def _list_folders(ctx: _Ctx) -> list[tuple[str, str]]:
    """Resolve the cache folder + its ``Envelope_<id>`` subfolders (reference §3.11 ``Build Folder List``).

    Returns ``(folder_id, folder_name)`` pairs; the cache ROOT is appended with an empty folder name (the
    ported convention). An unresolvable cache folder yields ``[]`` (logged, a clean no-op)."""
    cache_id = ctx.drive.find_folder_by_name(ctx.settings.drive_cache_folder_name)
    if cache_id is None:
        log.warning(
            "archive.watcher.cache_folder_missing",
            name=ctx.settings.drive_cache_folder_name,
        )
        return []
    folders: list[tuple[str, str]] = [(cache_id, "")]  # cache root, folderName=''
    for sub in ctx.drive.list_folder(cache_id, mime_type=FOLDER_MIME_TYPE):
        folders.append((sub.id, sub.name))
    return folders


def _handle_candidate(
    ctx: _Ctx, pdf: StorageEntry, folder_name: str, stats: WatchStats
) -> None:
    """Skip-filter, claim, and (on a won claim) process one candidate PDF."""
    stats.scanned += 1
    if should_skip(pdf.name):
        stats.skipped += 1
        log.debug("archive.watcher.skipped", name=pdf.name)
        return
    with ctx.session_factory() as db:
        won = claim_cache_file(
            db, file_id=pdf.id, file_name=pdf.name, envelope_folder=folder_name
        )
    if not won:
        return  # already seen (dedup) — the fail-closed claim
    stats.claimed += 1
    _process_file(ctx, pdf, folder_name, stats)


# --------------------------------------------------------------------------- #
# Per-file processing
# --------------------------------------------------------------------------- #
def _process_file(
    ctx: _Ctx, pdf: StorageEntry, folder_name: str, stats: WatchStats
) -> None:
    """Download → classify → name → duplicate-check → file → record → DM → fan-out for one claimed file."""
    try:
        pdf_bytes = ctx.drive.download(pdf.id)
    except StorageError as exc:
        _mark_failed(ctx, pdf.id, f"download failed ({type(exc).__name__})", stats)
        return

    classification = _classify(ctx, pdf_bytes)
    naming_failed = classification is None or not classification.is_complete
    if naming_failed:
        target_name = pdf.name
        status = STATUS_SAVED_DEFAULT_NAME
    else:
        assert classification is not None  # narrowed by naming_failed
        target_name = cache_rename_filename(
            effective_date=classification.effective_date,
            issuer=classification.issuer,
            nda_type=classification.nda_type,
            recipient=classification.recipient,
            today=ctx.today,
        )
        status = STATUS_RENAMED

    # Destination duplicate check (reference §3.11 ``Check Existing in Main Folder``).
    try:
        existing = ctx.drive.exists_in_folder(ctx.dest_folder_id, target_name)
    except StorageError as exc:
        _mark_failed(
            ctx, pdf.id, f"duplicate check failed ({type(exc).__name__})", stats
        )
        return
    if existing:
        _record(ctx, pdf.id, STATUS_DUPLICATE_SKIPPED, renamed_to=target_name)
        stats.duplicate_skipped += 1
        log.info("archive.watcher.duplicate_skipped", name=target_name)
        _notify(
            ctx, f":page_facing_up: Skipped *{target_name}* — already in the archive."
        )
        return

    # File a copy into the destination archive folder (reference §3.11 ``Upload Copy to Main Folder``).
    try:
        uploaded = ctx.drive.upload(
            name=target_name,
            content=pdf_bytes,
            content_type=PDF_MIME_TYPE,
            folder_id=ctx.dest_folder_id,
        )
    except StorageError as exc:
        _mark_failed(ctx, pdf.id, f"upload failed ({type(exc).__name__})", stats)
        return

    _record(ctx, pdf.id, status, renamed_to=target_name)
    if naming_failed:
        stats.saved_default_name += 1
        log.info("archive.watcher.saved_default_name", name=target_name)
        _notify(
            ctx,
            f":inbox_tray: Filed *{target_name}* (couldn't auto-name — kept original).",
        )
    else:
        stats.renamed += 1
        log.info("archive.watcher.renamed", name=target_name)
        _notify(ctx, f":inbox_tray: Filed a signed NDA as *{target_name}*.")

    # Requester DM (STUBBED seam, PLAN §3.10) + the expiration fan-out (both best-effort).
    _dm_requester_seam(ctx, folder_name, target_name)
    _fan_out(ctx, pdf, folder_name, target_name, uploaded, pdf_bytes, classification)


def _classify(ctx: _Ctx, pdf_bytes: bytes) -> CacheClassification | None:
    """Extract text + classify one signed NDA; ``None`` on any failure (→ the ported ``namingFailed``)."""
    if ctx.classify is None:
        return None
    try:
        text = ctx.extract_text("signed.pdf", pdf_bytes)
    except Exception as exc:  # noqa: BLE001 — an unreadable PDF is namingFailed, not a crash
        log.info("archive.watcher.extract_failed", error=repr(exc))
        return None
    if not (text or "").strip():
        return None
    try:
        return ctx.classify(text)
    except Exception as exc:  # noqa: BLE001 — a classify/provider failure is namingFailed
        log.warning("archive.watcher.classify_failed", error=repr(exc))
        return None


def _record(
    ctx: _Ctx, file_id: str, status: str, *, renamed_to: str | None = None
) -> None:
    """Transition the claimed row's status (parameterized). Best-effort — a record failure is logged."""
    try:
        with ctx.session_factory() as db:
            mark_cache_status(db, file_id, status, renamed_to=renamed_to, now=ctx.now)
    except Exception as exc:  # noqa: BLE001 — a status write must never crash the pass
        log.warning("archive.watcher.record_failed", file_id=file_id, error=repr(exc))


def _mark_failed(ctx: _Ctx, file_id: str, reason: str, stats: WatchStats) -> None:
    """Record a ``failed`` row + alert; the file is LEFT in the cache for the next pass (reference §3.11)."""
    try:
        with ctx.session_factory() as db:
            mark_cache_status(db, file_id, STATUS_FAILED, error=reason, now=ctx.now)
    except Exception as exc:  # noqa: BLE001 — a status write must never crash the pass
        log.warning(
            "archive.watcher.fail_record_failed", file_id=file_id, error=repr(exc)
        )
    stats.failed += 1
    log.warning("archive.watcher.failed", file_id=file_id, reason=reason)
    _notify(
        ctx,
        f":warning: Couldn't file a signed NDA ({reason}). It's still in the cache.",
    )


def _notify(ctx: _Ctx, text: str) -> None:
    """Post an admin-channel notice (fail-soft — a notify failure never affects the archive)."""
    try:
        ctx.notify_admin(text)
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        log.warning("archive.watcher.notify_failed", error=repr(exc))


def _fan_out(
    ctx: _Ctx,
    pdf: StorageEntry,
    folder_name: str,
    target_name: str,
    uploaded: StoredFile,
    pdf_bytes: bytes,
    classification: CacheClassification | None,
) -> None:
    """Hand the filed NDA to the archive fan-out (:func:`app.archive.hooks.on_archived`) — the seam the
    expiration extractor subscribes to (PLAN §3.10). Fail-soft (``on_archived`` swallows subscriber errors)."""
    payload = {
        "issuer": classification.issuer if classification else "",
        "recipient": classification.recipient if classification else "",
        "nda_type": classification.nda_type if classification else "",
        "effective_date": classification.effective_date if classification else "",
        "counterparty_name": classification.counterparty_name if classification else "",
    }
    on_archived(
        ArchivedFile(
            file_name=target_name,
            file_id=uploaded.id,  # the DESTINATION file id (the filed copy)
            folder_id=ctx.dest_folder_id,  # the destination archive folder id
            pdf_bytes=pdf_bytes,
            source_file_id=pdf.id,  # the originating cache file id
            envelope_folder=folder_name,
            classification=payload,
        )
    )


def _dm_requester_seam(ctx: _Ctx, folder_name: str, target_name: str) -> None:
    """Notify the human who requested a now-filed envelope (the ported DM-to-requester, reference §3.11/§7).

    Mechanism (VERIFIED 2026-07-04 against the live n8n workflows — there is no "Main_Project"; the send +
    mapping now live in ``NDA: Envelope Review``): the envelope intent writes ``nda_envelopes`` keyed by the
    DocuSign ``envelope_id`` with ``requested_by`` = the requester's Slack user id (+ channel/thread). DocuSign's
    native archive-to-Drive integration drops each completed envelope into a cache subfolder named by that same
    Envelope GUID, so we derive the id from the subfolder name (:func:`envelope_id_from_folder`, optional
    ``Envelope_`` prefix) and look the requester up by it. A cache-root file (empty folder name) or an unmatched
    id simply skips the DM — the archive still stands. (The live n8n watcher never actually DMed; this is the
    ported-but-newly-implemented step, keyed exactly as the writer above records it.)
    """
    envelope_id = envelope_id_from_folder(folder_name)
    if not envelope_id:
        return
    from ..integrations.models import NdaEnvelope

    try:
        with ctx.session_factory() as db:
            row = db.execute(
                select(NdaEnvelope).where(NdaEnvelope.envelope_id == envelope_id)
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — a requester lookup must never crash the pass
        log.warning("archive.watcher.requester_lookup_failed", error=repr(exc))
        return
    if row is None or not row.requested_by:
        log.info("archive.watcher.no_requester", envelope_id=envelope_id)
        return
    context = {
        "kind": row.channel or "slack",
        "target": row.requested_by,
        "slack_channel": row.slack_channel or "",
        "slack_thread_ts": row.slack_thread_ts or "",
        "email_message_id": row.email_message_id or "",
    }
    text = f":white_check_mark: Your signed NDA has been filed to the archive as *{target_name}*."
    try:
        ctx.dm_requester(context, text)
    except Exception as exc:  # noqa: BLE001 — the DM is best-effort
        log.warning(
            "archive.watcher.dm_failed", envelope_id=envelope_id, error=repr(exc)
        )


# --------------------------------------------------------------------------- #
# Production default resolution (lazy — kept off the import path)
# --------------------------------------------------------------------------- #
def _get_settings() -> Settings:
    from app.config import get_settings

    return get_settings()


def _build_registry(settings: Settings) -> Any:
    from app.capabilities import build_registry

    return build_registry(settings)


def _default_session_factory() -> Any:
    from app.db import SessionLocal

    return SessionLocal


def _default_classifier(settings: Settings) -> Classifier | None:
    from .classify import default_classifier

    return default_classifier(settings)


def _default_extract_text() -> TextExtractor:
    from app.api.routes_v1 import _extract_text

    return _extract_text


def _wired_service() -> Any | None:
    """The process-wide :class:`ReplyService` the bot pipeline delivers through (or ``None`` if unwired)."""
    try:
        from app.bot import router

        return router._DELIVERY[0] if router._DELIVERY else None
    except Exception:  # noqa: BLE001 — a missing router is just "no delivery wired"
        return None


def _default_notify_admin(settings: Settings) -> AdminNotifier:
    """Post watcher notices into the admin Slack channel via the wired reply service (no-op if unwired)."""
    from app.settings_store import admin_routing

    channel = admin_routing(settings_obj=settings)[0]

    def _notify(text: str) -> None:
        service = _wired_service()
        if service is None or not channel:
            log.info("archive.watcher.notify_skipped", has_service=service is not None)
            return
        from app.bot.channels.protocol import Reply
        from app.bot.envelope import Envelope

        env = Envelope(
            channel="slack",
            event_key=f"slack:watcher:notify:{channel}",
            slack_channel=channel,
            verified_sender=True,
            from_email=settings.nda_bot_from_email,
        )
        service.deliver(env, Reply(text=text))

    return _notify


def _default_dm_requester(settings: Settings) -> RequesterDM:
    """DM the requester of a filed envelope via the wired reply service (Slack DM / threaded email)."""

    def _dm(context: dict[str, Any], text: str) -> None:
        service = _wired_service()
        target = str(context.get("target") or "")
        if service is None or not target:
            log.info("archive.watcher.dm_skipped", has_service=service is not None)
            return
        from app.bot.channels.protocol import Reply
        from app.bot.envelope import Envelope

        if context.get("kind") == "email":
            env = Envelope(
                channel="email",
                event_key=f"slack:watcher:dm:{target}",
                sender_address=target,
                email_message_id=str(context.get("email_message_id") or ""),
                email_subject="Your NDA has been filed",
                from_email=settings.nda_bot_from_email,
            )
        else:
            env = Envelope(
                channel="slack",
                event_key=f"slack:watcher:dm:{target}",
                slack_channel=target,  # a Slack user id is a valid DM channel target
                slack_thread_ts=str(context.get("slack_thread_ts") or ""),
                verified_sender=True,
                from_email=settings.nda_bot_from_email,
            )
        service.deliver(env, Reply(text=text))

    return _dm


__all__ = [
    "run_watch_once",
    "should_skip",
    "WatchStats",
]
