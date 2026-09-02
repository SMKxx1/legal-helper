"""The ``archive`` intent + its Drive interactivity chain (PLAN §3.10, reference §3.6/§3.7).

A behavioral port of the n8n ``NDA: Archive`` sub-workflow and the ``arch_use_doc`` branch of
``NDA: Interactivity`` — with the PLAN §3.10 **email-symmetry fix**: the old no-file recovery path was
Slack-only, so email archive requests with no attachment fell through. Here BOTH channels archive.

Flow (reference §3.6):

* **Attachment present** — fetch the bytes, PDF-normalize a non-PDF via ``soffice``
  (:func:`app.integrations.convert.convert_to_pdf`), name it ``NDA_<basename>.pdf``
  (:func:`app.archive.naming.archive_filename`), and upload it into the Drive **cache** folder (resolved
  by name). The cache-folder watcher then auto-names + files it — hence the ported confirmation copy
  ("Saved to the *Signed NDAs Cache* — it will be auto-renamed and filed … within a few minutes").
* **No attachment, Slack** — thread-doc recovery (:mod:`app.bot.thread_docs`) → the ported
  *Yes, archive it* (``arch_use_doc``) / *No, attach a file* (``decline_doc``) confirm chain. The decline
  reuses the shared envelope ``decline_doc`` handler (identical reply).
* **No attachment, email** — the ported "attach the file" ask (the email-symmetry fix: a friendly reply,
  never a silent drop).

**Security (PLAN §3.3/§6, gates fail closed):** archive is an action-triggering intent, so an
EMAIL-initiated archive requires a DMARC-aligned (``verified_sender``) sender — an unverified email is
refused (read-only-helpful). Slack senders are signature-verified upstream. The GOOGLE_DRIVE capability
is checked first (capabilities fail soft): with Drive unconfigured, archive degrades to a friendly reply.

The intent uploads to the CACHE folder (resolved by ``drive_cache_folder_name``), NOT directly to
``drive_archive_folder_id`` — the watcher owns the auto-name → destination-folder step, exactly as in the
n8n pipeline (see the task open_items note on the "upload to drive_archive_folder_id" wording).

Every collaborator (the DB session factory, the Slack file fetcher, the thread scanner, the archiver =
convert+upload) is an injected constructor dep, so the whole matrix runs with fakes + **zero network**.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field
from sqlalchemy import select

from ...telemetry import get_logger
from ..blockkit import (
    ACTION_ARCH_USE_DOC,
    ARCH_THREAD_DOC_FALLBACK_TEXT,
    KIND_ARCH_USE_DOC,
    arch_confirm_blocks,
)
from ..channels.protocol import Reply
from ..envelope import AttachmentRef, Envelope
from ..interactivity import (
    ButtonPayload,
    Interaction,
    InteractivityDeps,
    InteractivityRegistry,
)
from ..models import BotCorrelation
from ..thread_docs import ThreadDoc, ThreadScanner
from . import IntentContext, IntentReply
from .review import HttpSlackFileFetcher, SlackFileFetcher

if TYPE_CHECKING:
    from app.capabilities import CapabilityRegistry
    from app.config import Settings

log = get_logger("nda.bot.intent.archive")

# --------------------------------------------------------------------------- #
# Ported copy + constants
# --------------------------------------------------------------------------- #
DEFAULT_DOC_NAME = "document.pdf"
#: Doc-like suffixes used to PICK the archive document off the inbound attachments (reference §3.6).
_DOC_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".rtf", ".odt"})
_PDF_MAGIC = b"%PDF"

#: bot_correlation kind for the archive thread-doc confirm state (PLAN §3.10).
ARCH_CORRELATION_KIND = "arch_confirm"
ARCH_CORRELATION_TTL_HOURS = 24.0

#: The ported confirmation (reference §3.6 ``Archive Reply``) — the file goes to the CACHE; the watcher
#: auto-names + files it into the destination folder within a few minutes.
ARCHIVE_CONFIRM_TEXT = (
    ":inbox_tray: Saved to the *Signed NDAs Cache* — I'll auto-name it and file it into "
    "*Signed Company NDAs* within a few minutes."
)
#: GOOGLE_DRIVE capability disabled/unhealthy — capabilities fail soft (PLAN §6).
ARCHIVE_UNAVAILABLE_TEXT = (
    "Archiving isn't set up right now, so I can't file that NDA. Let the team know and "
    "try again later."
)
#: Email archive from an unverified sender (PLAN §3.3/§6 — action-triggering intents need DMARC).
EMAIL_UNVERIFIED_TEXT = (
    "For security I can only archive documents from a verified sender, and this email didn't "
    "pass sender authentication (SPF/DKIM/DMARC). Please archive it from Slack instead."
)
#: The attachment couldn't be fetched (Slack download failed / email spool gone).
DOWNLOAD_FAILED_TEXT = (
    "I couldn't open that document. Please re-attach the signed NDA and try again."
)
#: PDF normalization failed (soffice conversion error / timeout).
CONVERT_FAILED_TEXT = "I couldn't convert that document to PDF to archive it. Please attach a PDF and try again."
#: The upload to Drive failed (an outage-shaped or rejected Drive call).
ARCHIVE_FAILED_TEXT = (
    "I couldn't file that NDA to the archive just now. Please try again in a moment."
)
#: Slack, no attachment and no recoverable thread doc (reference §3.6 "No-Doc Reject").
SLACK_NO_DOC_TEXT = (
    "I couldn't find a document to archive. Attach the signed NDA (`.pdf` or `.docx`) and "
    "send your request again."
)
#: Email, no attachment (the PLAN §3.10 email-symmetry fix — a friendly ask, not a silent drop).
EMAIL_NO_DOC_TEXT = (
    "To archive a signed NDA, attach it (`.pdf` or `.docx`) to your email and send your "
    "request again."
)
#: A recovered thread doc is no longer reachable at archive time.
DOC_GONE_TEXT = "I couldn't retrieve that document any more. Please re-attach the NDA and try again."
#: The confirm state expired or was already consumed (stale button).
EXPIRED_STATE_TEXT = (
    "Sorry — that archive request has expired. Please start again by mentioning me with your "
    "archive request."
)


# --------------------------------------------------------------------------- #
# The archiver seam (convert + upload) — pure of channel concerns
# --------------------------------------------------------------------------- #
#: ``archive_bytes(data, filename) -> final_name``: PDF-normalize + upload to the cache folder, returning
#: the uploaded name. Raises ``convert.Conversion*`` / ``storage.Storage*`` on failure (mapped to friendly
#: replies by the caller). Injected in tests (a fake that records the call), built lazily in production.
Archiver = Callable[[bytes, str], str]


def _looks_like_pdf(filename: str, data: bytes) -> bool:
    """The ported PDF check (reference §3.6): a ``.pdf`` name OR ``%PDF`` magic bytes."""
    return (filename or "").lower().endswith(".pdf") or data[:4] == _PDF_MAGIC


def archive_document(
    data: bytes,
    filename: str,
    *,
    drive: Any,
    cache_folder_name: str,
    convert: Callable[[bytes, str], bytes],
) -> str:
    """PDF-normalize ``data`` and upload it into the archive cache folder as ``NDA_<basename>.pdf``.

    Ports the n8n ``NDA: Archive`` upload half (reference §3.6): a non-PDF is converted via ``convert``
    (soffice); the canonical name is :func:`app.archive.naming.archive_filename`; the cache folder is
    resolved by name and the file uploaded there. ``drive`` is any
    :class:`~app.integrations.storage.base.ArchiveStorage` provider. Returns the uploaded name. Raises the
    convert/storage errors for the caller to map — a missing cache folder is a terminal storage error.
    """
    from app.archive.naming import archive_filename
    from app.integrations.storage.base import PDF_MIME_TYPE, StorageTerminalError

    pdf_bytes = data if _looks_like_pdf(filename, data) else convert(data, filename)
    name = archive_filename(filename)
    cache_id = drive.find_folder_by_name(cache_folder_name)
    if cache_id is None:
        raise StorageTerminalError(f"cache folder {cache_folder_name!r} not found")
    drive.upload(
        name=name, content=pdf_bytes, content_type=PDF_MIME_TYPE, folder_id=cache_id
    )
    log.info("bot.archive.uploaded", name=name, bytes=len(pdf_bytes))
    return name


def build_archiver(settings: Settings, registry: CapabilityRegistry) -> Archiver:
    """Build the production archiver: gate on GOOGLE_DRIVE, build the storage provider, convert+upload.

    Raises ``storage.StorageUnavailable`` (capability off) / ``convert.ConversionUnavailable`` (soffice
    missing) — both mapped to the friendly "archiving isn't set up" reply — or a storage/convert error on a
    live failure. The provider's httpx client is closed after each archive (a fresh short-lived token per
    burst is fine at archive cadence).
    """
    from app.integrations.convert import convert_to_pdf
    from app.integrations.storage.factory import get_archive_storage

    def _archive(data: bytes, filename: str) -> str:
        # Raises StorageUnavailable if the capability is off.
        drive = get_archive_storage(settings, registry)
        try:
            return archive_document(
                data,
                filename,
                drive=drive,
                cache_folder_name=settings.drive_cache_folder_name,
                convert=lambda d, fn: convert_to_pdf(d, filename=fn, settings=settings),
            )
        finally:
            close = getattr(drive, "close", None)
            if callable(close):
                close()

    return _archive


def _map_archive_error(exc: Exception) -> str:
    """Map a convert/storage failure onto the ported friendly reply text."""
    from app.integrations.convert import ConversionError, ConversionUnavailable
    from app.integrations.storage.base import StorageError, StorageUnavailable

    if isinstance(exc, (ConversionUnavailable, StorageUnavailable)):
        return ARCHIVE_UNAVAILABLE_TEXT
    if isinstance(exc, ConversionError):  # incl. ConversionTimeout
        return CONVERT_FAILED_TEXT
    if isinstance(exc, StorageError):
        return ARCHIVE_FAILED_TEXT
    return ARCHIVE_FAILED_TEXT


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _pick_doc_attachment(
    attachments: tuple[AttachmentRef, ...],
) -> AttachmentRef | None:
    """The first ``.pdf``/``.docx``/… attachment (reference §3.6 "Attach File (archive)"), else the first
    of any kind, else ``None``."""
    if not attachments:
        return None
    for att in attachments:
        name = (att.filename or "").lower()
        if any(name.endswith(suffix) for suffix in _DOC_SUFFIXES):
            return att
    return attachments[0]


def _requester(env: Envelope) -> str:
    return env.sender_id or env.sender_address or ""


def _now() -> datetime:
    return datetime.now(UTC)


def _expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    return exp < _now()


def _store_correlation(session_factory: Any, payload: dict[str, Any]) -> str:
    """Persist archive thread-doc state, returning a fresh opaque ``ref`` (the button value's secret)."""
    from ...auth.security import new_token

    key = new_token(18)
    with session_factory() as session:
        session.add(
            BotCorrelation(
                key=key,
                kind=ARCH_CORRELATION_KIND,
                payload_json=payload,
                expires_at=_now() + timedelta(hours=ARCH_CORRELATION_TTL_HOURS),
            )
        )
        session.commit()
    return key


def _load_correlation(session_factory: Any, ref: str) -> dict[str, Any] | None:
    if not ref or session_factory is None:
        return None
    try:
        with session_factory() as session:
            row = session.execute(
                select(BotCorrelation).where(BotCorrelation.key == ref)
            ).scalar_one_or_none()
            if row is None or _expired(row.expires_at):
                return None
            return dict(row.payload_json or {})
    except Exception as exc:  # noqa: BLE001 — a state read must never crash the ack path
        log.warning("bot.archive.correlation_read_failed", ref=ref, error=repr(exc))
        return None


def _doc_bytes_from_state(
    state: dict[str, Any], *, slack_fetch: SlackFileFetcher
) -> bytes:
    """Resolve the recovered thread document to bytes: inline base64 (if inlined), else a Slack fetch of
    the stored file ref (pulled only on the ``arch_use_doc`` click)."""
    b64 = state.get("doc_b64")
    if b64:
        return base64.b64decode(b64)
    ref = str(state.get("slack_file_id") or state.get("file_url") or "")
    if ref:
        att = AttachmentRef(
            filename=str(state.get("file_name") or DEFAULT_DOC_NAME), source_ref=ref
        )
        return slack_fetch(att)
    raise ValueError("no document in archive correlation state")


# =========================================================================== #
# The archive INTENT handler (IntentRegistry)
# =========================================================================== #
class ArchiveIntent:
    """The ``archive`` intent handler (reference §3.6). Callable ``(ctx) -> IntentReply``.

    Checks GOOGLE_DRIVE first (fail soft), refuses an unverified email (fail closed), then either
    archives an attachment (convert → cache upload → ported confirmation) or offers the Slack thread-doc
    confirm / the email attach-ask. Every collaborator is an injected dep so the matrix runs with fakes.
    """

    def __init__(
        self,
        *,
        session_factory: Any | None = None,
        settings: Settings | None = None,
        registry: CapabilityRegistry | None = None,
        slack_fetch: SlackFileFetcher | None = None,
        thread_scan: ThreadScanner | None = None,
        archiver: Archiver | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._registry = registry
        self._slack_fetch = slack_fetch
        self._thread_scan = thread_scan
        self._archiver = archiver

    # -- lazy production defaults ------------------------------------------
    def _get_settings(self) -> Settings:
        if self._settings is not None:
            return self._settings
        from app.config import get_settings

        return get_settings()

    def _get_registry(self) -> CapabilityRegistry:
        if self._registry is not None:
            return self._registry
        from app.capabilities import build_registry

        return build_registry(self._get_settings())

    def _get_session_factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        from app.db import SessionLocal

        return SessionLocal

    def _get_slack_fetch(self) -> SlackFileFetcher:
        if self._slack_fetch is not None:
            return self._slack_fetch
        return HttpSlackFileFetcher(self._get_settings().slack_bot_token)

    def _get_thread_scan(self) -> ThreadScanner:
        if self._thread_scan is not None:
            return self._thread_scan
        from ..thread_docs import HttpSlackThreadScanner

        return HttpSlackThreadScanner(self._get_settings().slack_bot_token)

    def _get_archiver(self) -> Archiver:
        if self._archiver is not None:
            return self._archiver
        return build_archiver(self._get_settings(), self._get_registry())

    def _drive_enabled(self) -> bool:
        from app.capabilities import GOOGLE_DRIVE, CapabilityState

        return self._get_registry().state(GOOGLE_DRIVE) is CapabilityState.ENABLED

    # -- entry point -------------------------------------------------------
    def __call__(self, ctx: IntentContext) -> IntentReply:
        env = ctx.envelope
        if not self._drive_enabled():
            log.info("bot.archive.capability_off", event_key=env.event_key)
            return IntentReply(text=ARCHIVE_UNAVAILABLE_TEXT)

        # Email archive is an action → DMARC-aligned sender required (PLAN §3.3/§6, fail closed).
        if env.channel == "email" and not env.verified_sender:
            log.info("bot.archive.email_unverified", event_key=env.event_key)
            return IntentReply(text=EMAIL_UNVERIFIED_TEXT)

        att = _pick_doc_attachment(env.attachments)
        if att is not None:
            try:
                data = self._load_bytes(env, att)
            except Exception as exc:  # noqa: BLE001 — a fetch failure is a friendly reply, not a crash
                log.warning(
                    "bot.archive.fetch_failed",
                    event_key=env.event_key,
                    filename=att.filename,
                    error=repr(exc),
                )
                return IntentReply(text=DOWNLOAD_FAILED_TEXT)
            return self._archive_and_reply(env, data, att.filename or DEFAULT_DOC_NAME)

        # No attachment → recover (Slack) / ask (email).
        if env.channel == "slack":
            thread_doc = self._recover_thread_doc(env)
            if thread_doc is None:
                log.info("bot.archive.no_doc", event_key=env.event_key)
                return IntentReply(text=SLACK_NO_DOC_TEXT)
            return self._offer_thread_doc(env, thread_doc)
        log.info("bot.archive.email_no_doc", event_key=env.event_key)
        return IntentReply(text=EMAIL_NO_DOC_TEXT)

    # -- attachment path ---------------------------------------------------
    def _archive_and_reply(
        self, env: Envelope, data: bytes, filename: str
    ) -> IntentReply:
        try:
            name = self._get_archiver()(data, filename)
        except Exception as exc:  # noqa: BLE001 — convert/Drive failures degrade to friendly replies
            text = _map_archive_error(exc)
            log.warning(
                "bot.archive.failed",
                event_key=env.event_key,
                filename=filename,
                error=repr(exc),
                reply=text,
            )
            return IntentReply(text=text)
        log.info("bot.archive.done", event_key=env.event_key, name=name)
        return IntentReply(text=ARCHIVE_CONFIRM_TEXT)

    # -- no-attachment (Slack) → thread-doc recovery -----------------------
    def _recover_thread_doc(self, env: Envelope) -> ThreadDoc | None:
        if not env.slack_thread_ts:
            return None
        try:
            return self._get_thread_scan()(env.slack_channel, env.slack_thread_ts)
        except Exception as exc:  # noqa: BLE001 — recovery is best-effort
            log.warning(
                "bot.archive.thread_scan_failed",
                event_key=env.event_key,
                error=repr(exc),
            )
            return None

    def _offer_thread_doc(self, env: Envelope, thread_doc: ThreadDoc) -> IntentReply:
        payload = {
            "file_name": thread_doc.file_name,
            "slack_file_id": thread_doc.file_id,
            "file_url": thread_doc.file_url,
            "channel": env.channel,
            "slack_channel": env.slack_channel,
            "slack_thread_ts": env.slack_thread_ts,
            "requested_by": _requester(env),
        }
        ref = _store_correlation(self._get_session_factory(), payload)
        log.info(
            "bot.archive.thread_doc_offer",
            event_key=env.event_key,
            ref=ref,
            file_name=thread_doc.file_name,
        )
        return IntentReply(
            slack_blocks=tuple(arch_confirm_blocks(thread_doc.file_name, ref)),
            fallback_text=ARCH_THREAD_DOC_FALLBACK_TEXT,
        )

    def _load_bytes(self, env: Envelope, att: AttachmentRef) -> bytes:
        """Resolve attachment bytes from its channel handle (email spool path / Slack file)."""
        if env.channel == "email":
            from pathlib import Path

            ref = (att.source_ref or "").strip()
            if not ref:
                raise ValueError("email attachment was not spooled (no source_ref)")
            return Path(ref).read_bytes()
        return self._get_slack_fetch()(att)


# =========================================================================== #
# Typed button-value payload (validated before the handler runs)
# =========================================================================== #
class ArchUseDocPayload(ButtonPayload):
    """The *Yes, archive it* thread-doc confirm (reference §3.6/§3.7 ``arch_use_doc``). ``ref`` keys the
    ``bot_correlation`` row holding the recovered document + its origin thread."""

    kind: Literal["arch_use_doc"] = "arch_use_doc"
    ref: str = Field(min_length=1)


# =========================================================================== #
# Archive interactivity handler + registration
# =========================================================================== #
@dataclass(frozen=True)
class ArchiveDeps:
    """Archive-specific collaborators bound at :func:`register_archive` time (the default registry binds
    NONE; each is resolved lazily from the per-dispatch :class:`InteractivityDeps` + settings). Tests bind
    fakes (a Slack fetcher, an archiver, a capability registry) for a zero-network chain."""

    slack_fetch: SlackFileFetcher | None = None
    archiver: Archiver | None = None
    drive_registry: CapabilityRegistry | None = None


class ArchiveInteractivity:
    """The ``arch_use_doc`` kind handler (reference §3.7). Registered onto the shared
    :class:`InteractivityRegistry`. The ``decline_doc`` button reuses the envelope module's shared handler
    (identical "attach a file" reply), so only ``arch_use_doc`` is registered here.
    """

    def __init__(self, deps: ArchiveDeps | None = None) -> None:
        self._deps = deps or ArchiveDeps()

    def register(self, registry: InteractivityRegistry) -> None:
        registry.register_action(ACTION_ARCH_USE_DOC, KIND_ARCH_USE_DOC)
        registry.register_kind(
            KIND_ARCH_USE_DOC, self._handle_arch_use_doc, value_model=ArchUseDocPayload
        )

    # -- the arch_use_doc click -------------------------------------------
    def _handle_arch_use_doc(
        self, interaction: Interaction, deps: InteractivityDeps
    ) -> None:
        payload = interaction.payload
        if not isinstance(payload, ArchUseDocPayload):
            return
        env = _interaction_envelope(interaction, deps)
        if not self._drive_enabled(deps):
            _deliver_text(env, ARCHIVE_UNAVAILABLE_TEXT, deps)
            return
        state = _load_correlation(deps.session_factory, payload.ref)
        if state is None:
            _deliver_text(env, EXPIRED_STATE_TEXT, deps)
            return
        try:
            data = _doc_bytes_from_state(state, slack_fetch=self._get_slack_fetch(deps))
        except Exception as exc:  # noqa: BLE001 — a re-fetch failure is friendly, not a crash
            log.warning(
                "bot.archive.use_doc.fetch_failed", ref=payload.ref, error=repr(exc)
            )
            _deliver_text(env, DOC_GONE_TEXT, deps)
            return
        file_name = str(state.get("file_name") or DEFAULT_DOC_NAME)
        try:
            name = self._get_archiver(deps)(data, file_name)
        except Exception as exc:  # noqa: BLE001 — convert/Drive failures degrade to friendly replies
            log.warning("bot.archive.use_doc.failed", ref=payload.ref, error=repr(exc))
            _deliver_text(env, _map_archive_error(exc), deps)
            return
        log.info("bot.archive.use_doc.done", ref=payload.ref, name=name)
        _deliver_text(env, ARCHIVE_CONFIRM_TEXT, deps)

    # -- collaborator resolution ------------------------------------------
    def _get_registry(self, deps: InteractivityDeps) -> CapabilityRegistry:
        if self._deps.drive_registry is not None:
            return self._deps.drive_registry
        from app.capabilities import build_registry

        return build_registry(deps.settings or _get_settings())

    def _drive_enabled(self, deps: InteractivityDeps) -> bool:
        from app.capabilities import GOOGLE_DRIVE, CapabilityState

        return self._get_registry(deps).state(GOOGLE_DRIVE) is CapabilityState.ENABLED

    def _get_slack_fetch(self, deps: InteractivityDeps) -> SlackFileFetcher:
        if self._deps.slack_fetch is not None:
            return self._deps.slack_fetch
        settings = deps.settings or _get_settings()
        return HttpSlackFileFetcher(settings.slack_bot_token)

    def _get_archiver(self, deps: InteractivityDeps) -> Archiver:
        if self._deps.archiver is not None:
            return self._deps.archiver
        settings = deps.settings or _get_settings()
        return build_archiver(settings, self._get_registry(deps))


# --------------------------------------------------------------------------- #
# Interactivity helpers (envelope + delivery) — small local copies
# --------------------------------------------------------------------------- #
def _get_settings() -> Settings:
    from app.config import get_settings

    return get_settings()


def _interaction_envelope(
    interaction: Interaction, deps: InteractivityDeps
) -> Envelope | None:
    """A Slack reply envelope addressed at the interaction's OWN thread (the confirm card's message)."""
    channel = interaction.channel_id
    if not channel:
        return None
    thread = interaction.thread_ts or interaction.message_ts
    from_email = deps.settings.nda_bot_from_email if deps.settings else ""
    return Envelope(
        channel="slack",
        event_key=f"slack:int:arch:{channel}:{thread or 'root'}",
        slack_channel=channel,
        slack_thread_ts=thread or "",
        verified_sender=True,
        from_email=from_email,
    )


def _deliver_text(env: Envelope | None, text: str, deps: InteractivityDeps) -> None:
    if env is None or deps.service is None:
        return
    try:
        deps.service.deliver(env, Reply(text=text))
    except Exception as exc:  # noqa: BLE001 — delivery is fail-soft
        log.warning("bot.archive.deliver_failed", error=repr(exc))


def register_archive(
    registry: InteractivityRegistry, *, deps: ArchiveDeps | None = None
) -> None:
    """Register the archive interactivity kind onto ``registry`` (called from
    ``default_interactivity_registry``; tests pass ``deps`` to bind fakes)."""
    ArchiveInteractivity(deps).register(registry)


__all__ = [
    "ArchiveIntent",
    "ArchiveInteractivity",
    "ArchiveDeps",
    "ArchUseDocPayload",
    "Archiver",
    "archive_document",
    "build_archiver",
    "register_archive",
    "ARCH_CORRELATION_KIND",
    "ARCHIVE_CONFIRM_TEXT",
    "ARCHIVE_UNAVAILABLE_TEXT",
    "EMAIL_UNVERIFIED_TEXT",
    "EMAIL_NO_DOC_TEXT",
    "SLACK_NO_DOC_TEXT",
    "CONVERT_FAILED_TEXT",
    "DOWNLOAD_FAILED_TEXT",
    "DOC_GONE_TEXT",
    "EXPIRED_STATE_TEXT",
]
