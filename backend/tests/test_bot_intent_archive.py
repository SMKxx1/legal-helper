"""The ``archive`` intent + ``arch_use_doc`` interactivity matrix (PLAN §3.10, reference §3.6/§3.7).

Drives :class:`app.bot.intents.archive.ArchiveIntent` / :class:`ArchiveInteractivity` directly with fakes
(an archiver = convert+upload, a Slack file fetcher, a thread scanner) + the throwaway
``bot_session_factory`` — zero network, zero soffice, zero Drive. Covers: BOTH channels, the PLAN §3.10
email-symmetry fix (email no-file ask) + the DMARC refusal (email action needs a verified sender), the
capability-off fail-soft reply, the convert/upload error mappings, thread-doc recovery + the arch_use_doc
confirm chain, and the ``archive_document`` convert-vs-passthrough decision.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.bot.envelope import AttachmentRef, Envelope
from app.bot.intents import IntentContext
from app.bot.intents.archive import (
    ARCH_CORRELATION_KIND,
    ARCHIVE_CONFIRM_TEXT,
    ARCHIVE_FAILED_TEXT,
    ARCHIVE_UNAVAILABLE_TEXT,
    CONVERT_FAILED_TEXT,
    DOC_GONE_TEXT,
    DOWNLOAD_FAILED_TEXT,
    EMAIL_NO_DOC_TEXT,
    EMAIL_UNVERIFIED_TEXT,
    EXPIRED_STATE_TEXT,
    SLACK_NO_DOC_TEXT,
    ArchiveDeps,
    ArchiveIntent,
    archive_document,
    register_archive,
)
from app.bot.interactivity import (
    InteractivityDeps,
    InteractivityRegistry,
    dispatch_interaction,
)
from app.bot.models import BotCorrelation
from app.bot.router import Classification
from app.bot.thread_docs import ThreadDoc
from app.config import Settings

pytest_plugins = ("conftest_bot",)

PDF = b"%PDF-1.4 signed nda\n%%EOF"


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
def _enabled_settings() -> Settings:
    """Settings that ENABLE the google_drive capability (OAuth trio + destination folder)."""
    return Settings(
        _env_file=None,
        google_oauth_client_id="cid",
        google_oauth_client_secret="secret",
        google_oauth_refresh_token="refresh",
        drive_archive_folder_id="DEST",
        drive_cache_folder_name="Cache",
        nda_bot_from_email="nda-bot@example.com",
    )


def _enabled_registry(settings: Settings | None = None):
    from app.capabilities import build_registry

    return build_registry(settings or _enabled_settings())


class _Archiver:
    """A fake archiver: records (data, filename) and returns a name, or raises a canned error."""

    def __init__(
        self, *, name: str = "NDA_signed.pdf", raises: Exception | None = None
    ) -> None:
        self.name = name
        self.raises = raises
        self.calls: list[tuple[bytes, str]] = []

    def __call__(self, data: bytes, filename: str) -> str:
        self.calls.append((data, filename))
        if self.raises is not None:
            raise self.raises
        return self.name


def _slack_att(name: str = "signed.pdf") -> AttachmentRef:
    return AttachmentRef(filename=name, source_ref="F1")


def _slack_env(
    *, attachments: tuple[AttachmentRef, ...] = (), thread_ts: str = "T1"
) -> Envelope:
    return Envelope(
        channel="slack",
        event_key="slack:A1",
        slack_channel="C1",
        slack_thread_ts=thread_ts,
        sender_id="U1",
        verified_sender=True,
        text="archive this",
        attachments=attachments,
    )


def _email_env(
    *, attachments: tuple[AttachmentRef, ...] = (), verified: bool = True
) -> Envelope:
    return Envelope(
        channel="email",
        event_key="email:A1",
        sender_address="lawyer@example.com",
        verified_sender=verified,
        text="archive this",
        attachments=attachments,
    )


_CLS = Classification(intent="archive")


def _intent(
    bot_session_factory: Any,
    *,
    archiver: Any = None,
    fetch: Any = None,
    scan: Any = None,
    settings: Settings | None = None,
    registry: Any = None,
) -> ArchiveIntent:
    settings = settings or _enabled_settings()
    return ArchiveIntent(
        session_factory=bot_session_factory,
        settings=settings,
        registry=registry or _enabled_registry(settings),
        slack_fetch=fetch or (lambda att: PDF),
        thread_scan=scan or (lambda ch, ts: None),
        archiver=archiver or _Archiver(),
    )


def _ctx(env: Envelope) -> IntentContext:
    return IntentContext(envelope=env, classification=_CLS)


# --------------------------------------------------------------------------- #
# Attachment path (both channels)
# --------------------------------------------------------------------------- #
def test_slack_attachment_archived(bot_session_factory):
    arch = _Archiver(name="NDA_signed.pdf")
    reply = _intent(bot_session_factory, archiver=arch)(
        _ctx(_slack_env(attachments=(_slack_att(),)))
    )
    assert reply.text == ARCHIVE_CONFIRM_TEXT
    assert arch.calls == [(PDF, "signed.pdf")]


def test_email_verified_attachment_archived(bot_session_factory):
    arch = _Archiver()
    # email attachments resolve from a spool path; fetch is via the injected slack_fetch only for slack —
    # for email the intent reads att.source_ref as a filesystem path. Use a real temp file.
    reply = _intent(
        bot_session_factory,
        archiver=arch,
        fetch=lambda a: PDF,  # unused on the email branch
    )(
        _ctx(
            _email_env(
                attachments=(
                    AttachmentRef(filename="signed.pdf", source_ref=_spool(PDF)),
                )
            )
        )
    )
    assert reply.text == ARCHIVE_CONFIRM_TEXT
    assert arch.calls and arch.calls[0][1] == "signed.pdf"


def test_email_unverified_refused(bot_session_factory):
    arch = _Archiver()
    reply = _intent(bot_session_factory, archiver=arch)(
        _ctx(_email_env(attachments=(_slack_att(),), verified=False))
    )
    assert reply.text == EMAIL_UNVERIFIED_TEXT
    assert arch.calls == []  # never touched Drive for an unverified email


def test_capability_disabled_is_friendly(bot_session_factory):
    disabled = Settings(_env_file=None)  # no Drive config → google_drive disabled
    from app.capabilities import build_registry

    arch = _Archiver()
    intent = ArchiveIntent(
        session_factory=bot_session_factory,
        settings=disabled,
        registry=build_registry(disabled),
        slack_fetch=lambda a: PDF,
        thread_scan=lambda c, t: None,
        archiver=arch,
    )
    reply = intent(_ctx(_slack_env(attachments=(_slack_att(),))))
    assert reply.text == ARCHIVE_UNAVAILABLE_TEXT
    assert arch.calls == []


def test_fetch_failure_is_friendly(bot_session_factory):
    def _boom(att):
        raise RuntimeError("slack download 404")

    reply = _intent(bot_session_factory, fetch=_boom)(
        _ctx(_slack_env(attachments=(_slack_att(),)))
    )
    assert reply.text == DOWNLOAD_FAILED_TEXT


@pytest.mark.parametrize(
    "exc_factory, expected",
    [
        ("drive_unavailable", ARCHIVE_UNAVAILABLE_TEXT),
        ("conversion_unavailable", ARCHIVE_UNAVAILABLE_TEXT),
        ("conversion_error", CONVERT_FAILED_TEXT),
        ("drive_error", ARCHIVE_FAILED_TEXT),
    ],
)
def test_archiver_errors_map_to_friendly(bot_session_factory, exc_factory, expected):
    from app.integrations.convert import ConversionError, ConversionUnavailable
    from app.integrations.storage.base import StorageTerminalError, StorageUnavailable

    exc = {
        "drive_unavailable": StorageUnavailable("off"),
        "conversion_unavailable": ConversionUnavailable("no soffice"),
        "conversion_error": ConversionError("bad exit"),
        "drive_error": StorageTerminalError("400", status_code=400),
    }[exc_factory]
    reply = _intent(bot_session_factory, archiver=_Archiver(raises=exc))(
        _ctx(_slack_env(attachments=(_slack_att(),)))
    )
    assert reply.text == expected


# --------------------------------------------------------------------------- #
# No-attachment path
# --------------------------------------------------------------------------- #
def test_slack_no_doc_no_thread_recovery(bot_session_factory):
    reply = _intent(bot_session_factory, scan=lambda c, t: None)(_ctx(_slack_env()))
    assert reply.text == SLACK_NO_DOC_TEXT


def test_email_no_attachment_asks(bot_session_factory):
    # The PLAN §3.10 email-symmetry fix: a friendly ask, never a silent drop.
    reply = _intent(bot_session_factory)(_ctx(_email_env()))
    assert reply.text == EMAIL_NO_DOC_TEXT


def test_slack_thread_recovery_offers_confirm(bot_session_factory):
    doc = ThreadDoc(file_id="TF1", file_name="signed.pdf", file_url="https://x/dl")
    reply = _intent(bot_session_factory, scan=lambda c, t: doc)(_ctx(_slack_env()))
    assert reply.slack_blocks is not None
    ref = _arch_ref(reply.slack_blocks)
    assert ref
    # A correlation row was stashed under the archive kind, holding the thread doc + origin.
    with bot_session_factory() as db:
        row = db.query(BotCorrelation).filter(BotCorrelation.key == ref).one()
        assert row.kind == ARCH_CORRELATION_KIND
        assert row.payload_json["slack_file_id"] == "TF1"
        assert row.payload_json["slack_channel"] == "C1"


# --------------------------------------------------------------------------- #
# arch_use_doc interactivity (the confirm click)
# --------------------------------------------------------------------------- #
class _CapturingService:
    def __init__(self) -> None:
        self.delivered: list[tuple[Envelope, Any]] = []

    def deliver(self, env: Envelope, reply: Any) -> None:
        self.delivered.append((env, reply))


def _dispatch_arch_use_doc(
    bot_session_factory, ref: str, *, archiver, settings, registry
):
    reg = InteractivityRegistry()
    register_archive(
        reg,
        deps=ArchiveDeps(
            slack_fetch=lambda att: PDF, archiver=archiver, drive_registry=registry
        ),
    )
    service = _CapturingService()
    deps = InteractivityDeps(
        session_factory=bot_session_factory, service=service, settings=settings
    )
    from app.bot.blockkit import arch_use_doc_value

    body = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "T1", "thread_ts": "T1"},
        "actions": [{"action_id": "arch_use_doc", "value": arch_use_doc_value(ref)}],
    }
    dispatch_interaction(body, registry=reg, deps=deps)
    return service


def test_arch_use_doc_files_and_confirms(bot_session_factory):
    settings = _enabled_settings()
    registry = _enabled_registry(settings)
    # Produce the offer (which stashes the correlation), then click it.
    doc = ThreadDoc(file_id="TF1", file_name="signed.pdf", file_url="https://x/dl")
    offer = _intent(
        bot_session_factory, scan=lambda c, t: doc, settings=settings, registry=registry
    )(_ctx(_slack_env()))
    ref = _arch_ref(offer.slack_blocks)
    arch = _Archiver(name="NDA_signed.pdf")
    service = _dispatch_arch_use_doc(
        bot_session_factory, ref, archiver=arch, settings=settings, registry=registry
    )
    assert [r.text for _, r in service.delivered] == [ARCHIVE_CONFIRM_TEXT]
    assert arch.calls == [(PDF, "signed.pdf")]


def test_arch_use_doc_stale_ref(bot_session_factory):
    settings = _enabled_settings()
    service = _dispatch_arch_use_doc(
        bot_session_factory,
        "no-such-ref",
        archiver=_Archiver(),
        settings=settings,
        registry=_enabled_registry(settings),
    )
    assert [r.text for _, r in service.delivered] == [EXPIRED_STATE_TEXT]


def test_arch_use_doc_capability_off(bot_session_factory):
    settings = _enabled_settings()
    registry = _enabled_registry(settings)
    doc = ThreadDoc(file_id="TF1", file_name="signed.pdf", file_url="https://x/dl")
    offer = _intent(
        bot_session_factory, scan=lambda c, t: doc, settings=settings, registry=registry
    )(_ctx(_slack_env()))
    ref = _arch_ref(offer.slack_blocks)
    # Now the capability is disabled at click time.
    disabled = Settings(_env_file=None)
    from app.capabilities import build_registry

    service = _dispatch_arch_use_doc(
        bot_session_factory,
        ref,
        archiver=_Archiver(),
        settings=settings,
        registry=build_registry(disabled),
    )
    assert [r.text for _, r in service.delivered] == [ARCHIVE_UNAVAILABLE_TEXT]


def test_arch_use_doc_doc_gone(bot_session_factory):
    settings = _enabled_settings()
    registry = _enabled_registry(settings)
    doc = ThreadDoc(file_id="TF1", file_name="signed.pdf", file_url="https://x/dl")
    offer = _intent(
        bot_session_factory, scan=lambda c, t: doc, settings=settings, registry=registry
    )(_ctx(_slack_env()))
    ref = _arch_ref(offer.slack_blocks)
    reg = InteractivityRegistry()

    def _boom(att):
        raise RuntimeError("file deleted")

    register_archive(
        reg,
        deps=ArchiveDeps(
            slack_fetch=_boom, archiver=_Archiver(), drive_registry=registry
        ),
    )
    service = _CapturingService()
    from app.bot.blockkit import arch_use_doc_value

    body = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "T1", "thread_ts": "T1"},
        "actions": [{"action_id": "arch_use_doc", "value": arch_use_doc_value(ref)}],
    }
    dispatch_interaction(
        body,
        registry=reg,
        deps=InteractivityDeps(
            session_factory=bot_session_factory, service=service, settings=settings
        ),
    )
    assert [r.text for _, r in service.delivered] == [DOC_GONE_TEXT]


# --------------------------------------------------------------------------- #
# archive_document — the convert-vs-passthrough decision (unit)
# --------------------------------------------------------------------------- #
class _FakeDrive:
    """A minimal :class:`ArchiveStorage` provider for the ``archive_document`` unit tests."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []

    def find_folder_by_name(self, name, *, parent_id=None):
        return "CACHE"

    def upload(self, *, name, content, content_type, folder_id):
        from app.integrations.storage.base import StoredFile

        self.uploads.append((folder_id, name, content))
        return StoredFile(id="U1", name=name)


def test_archive_document_passes_pdf_through_without_converting():
    drive = _FakeDrive()
    called: list[Any] = []

    def _convert(d, fn):
        called.append((d, fn))
        return b"CONVERTED"

    name = archive_document(
        PDF, "signed.pdf", drive=drive, cache_folder_name="Cache", convert=_convert
    )
    assert name == "NDA_signed.pdf"
    assert called == []  # a PDF is uploaded as-is
    assert drive.uploads == [("CACHE", "NDA_signed.pdf", PDF)]


def test_archive_document_converts_non_pdf():
    drive = _FakeDrive()
    converted = b"%PDF-converted"

    def _convert(d, fn):
        assert fn == "signed.docx"
        return converted

    name = archive_document(
        b"PK docx",
        "signed.docx",
        drive=drive,
        cache_folder_name="Cache",
        convert=_convert,
    )
    assert name == "NDA_signed.pdf"
    assert drive.uploads == [("CACHE", "NDA_signed.pdf", converted)]


def test_archive_document_missing_cache_folder_raises():
    from app.integrations.storage.base import StorageTerminalError

    class _NoFolder(_FakeDrive):
        def find_folder_by_name(self, name, *, parent_id=None):
            return None

    with pytest.raises(StorageTerminalError):
        archive_document(
            PDF,
            "x.pdf",
            drive=_NoFolder(),
            cache_folder_name="Cache",
            convert=lambda d, f: d,
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _arch_ref(blocks) -> str:
    """Extract the arch_use_doc button's ref from a confirm card's blocks."""
    for block in blocks or ():
        if block.get("type") == "actions":
            for el in block.get("elements", []):
                if el.get("action_id") == "arch_use_doc":
                    return json.loads(el["value"])["ref"]
    return ""


@pytest.fixture(autouse=True)
def _spool_dir(tmp_path):
    global _SPOOL
    _SPOOL = tmp_path


_SPOOL = None


def _spool(data: bytes) -> str:
    """Write bytes to a temp file and return its path (an email attachment 'spool')."""
    import uuid

    path = _SPOOL / f"{uuid.uuid4().hex}.pdf"
    path.write_bytes(data)
    return str(path)
