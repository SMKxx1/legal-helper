"""Email intake normalization, quoted-history cleaning, and the sender-authenticity matrix.

No network: every case is a fabricated raw RFC822 message parsed by :func:`envelope_from_raw`, plus a
fake mailbox driving :func:`poll_once`. The cleaning golden cases mirror the n8n Router ``Clean Email
Text`` behaviors (reference §3.1); the ``verified_sender`` matrix exercises PLAN §3.3/§6.
"""

from __future__ import annotations

from email.message import EmailMessage

from app.bot.channels import email_in
from app.bot.channels.email_in import (
    clean_email_text,
    envelope_from_raw,
    is_reply_subject,
    strip_reply_prefixes,
)
from app.config import Settings


# --------------------------------------------------------------------------- #
# Fabricate raw RFC822 bytes
# --------------------------------------------------------------------------- #
def build_raw(
    *,
    sender: str = "Alice Partner <alice@partner.com>",
    to: str = "nda-bot@example.com",
    subject: str = "Please review this NDA",
    body: str = "Can you review the attached NDA?",
    html: str | None = None,
    message_id: str | None = "<msg-001@partner.com>",
    auth_results: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if subject is not None:
        msg["Subject"] = subject
    if message_id is not None:
        msg["Message-ID"] = message_id
    if auth_results is not None:
        msg["Authentication-Results"] = auth_results
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for filename, content, ctype in attachments or []:
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=filename,
        )
    return msg.as_bytes()


# --------------------------------------------------------------------------- #
# Core normalization
# --------------------------------------------------------------------------- #
def test_basic_normalization_maps_the_n8n_envelope_fields():
    env = envelope_from_raw(build_raw(), require_dmarc=False)
    assert env.channel == "email"
    assert env.event_key == "email:msg-001@partner.com"
    assert env.email_message_id == "msg-001@partner.com"
    assert env.sender_address == "alice@partner.com"
    assert "Alice Partner" in env.sender_id
    assert env.email_subject == "Please review this NDA"
    # text = subject + '\n\n' + cleaned body (the n8n shape; classifier sees the subject line).
    assert env.text.startswith("Please review this NDA")
    assert "Can you review the attached NDA?" in env.text
    assert env.has_content is True


def test_missing_message_id_gets_a_stable_nomsgid_dedup_key():
    raw = build_raw(message_id=None)
    env = envelope_from_raw(raw, require_dmarc=False)
    assert env.event_key.startswith("email:nomsgid-")
    # A synthesized fallback is NOT a real id -> no threading header source downstream.
    assert env.email_message_id == ""
    # Deterministic: the same bytes always produce the same key (dedup stays stable).
    assert envelope_from_raw(raw, require_dmarc=False).event_key == env.event_key


def test_html_only_body_is_stripped_to_text():
    # An html-only message (no text/plain part) so get_body falls through to the html branch.
    msg = EmailMessage()
    msg["From"] = "b@x.com"
    msg["Subject"] = "hello"
    msg["Message-ID"] = "<h@x.com>"
    msg.set_content("<p>Please <strong>review</strong> this.</p>", subtype="html")
    env = envelope_from_raw(msg.as_bytes(), require_dmarc=False)
    assert "Please review this." in env.text
    assert "<strong>" not in env.text


# --------------------------------------------------------------------------- #
# verified_sender matrix (PLAN §3.3 / §6)
# --------------------------------------------------------------------------- #
def test_dmarc_pass_aligned_is_verified():
    ar = "mx.example.com; dmarc=pass (p=REJECT) header.from=partner.com; spf=pass"
    env = envelope_from_raw(build_raw(auth_results=ar), require_dmarc=True)
    assert env.verified_sender is True


def test_dmarc_pass_but_from_domain_mismatch_is_untrusted():
    ar = "mx.example.com; dmarc=pass header.from=evil.example"
    env = envelope_from_raw(build_raw(auth_results=ar), require_dmarc=True)
    assert env.verified_sender is False


def test_spf_and_dkim_pass_with_aligned_dkim_domain_is_verified():
    ar = "mx.example.com; spf=pass smtp.mailfrom=partner.com; dkim=pass header.d=partner.com"
    env = envelope_from_raw(build_raw(auth_results=ar), require_dmarc=True)
    assert env.verified_sender is True


def test_spf_pass_alone_is_untrusted():
    ar = "mx.example.com; spf=pass smtp.mailfrom=partner.com; dkim=none; dmarc=none"
    env = envelope_from_raw(build_raw(auth_results=ar), require_dmarc=True)
    assert env.verified_sender is False


def test_dmarc_fail_is_untrusted():
    ar = "mx.example.com; dmarc=fail header.from=partner.com"
    env = envelope_from_raw(build_raw(auth_results=ar), require_dmarc=True)
    assert env.verified_sender is False


def test_no_auth_header_is_untrusted_when_required():
    env = envelope_from_raw(build_raw(auth_results=None), require_dmarc=True)
    assert env.verified_sender is False


def test_require_dmarc_false_trusts_unconditionally():
    # Trusted-relay dev setup: no auth header, but the relay is trusted -> verified.
    env = envelope_from_raw(build_raw(auth_results=None), require_dmarc=False)
    assert env.verified_sender is True


def test_dkim_pass_with_mismatched_dkim_domain_is_untrusted():
    ar = "mx.example.com; spf=pass; dkim=pass header.d=spammer.example"
    env = envelope_from_raw(build_raw(auth_results=ar), require_dmarc=True)
    assert env.verified_sender is False


# --------------------------------------------------------------------------- #
# Quoted-history cleaning golden cases (reference §3.1)
# --------------------------------------------------------------------------- #
def test_clean_strips_gmail_attribution_and_quote():
    body = (
        "Thanks, that works for me.\n\n"
        "On Mon, Jul 1, 2026 at 3:00 PM Bob <bob@x.com> wrote:\n"
        "> Here is the original\n"
        "> message text\n"
    )
    assert clean_email_text(body) == "Thanks, that works for me."


def test_clean_strips_gmail_attribution_that_wraps_two_lines():
    body = (
        "Sounds good.\n\n"
        "On Mon, Jul 1, 2026 at 3:00 PM Bob Longname\n"
        "<bob@x.com> wrote:\n"
        "> quoted\n"
    )
    assert clean_email_text(body) == "Sounds good."


def test_clean_strips_original_message_separator():
    body = "My reply here.\n\n-----Original Message-----\nFrom: someone\nblah\n"
    assert clean_email_text(body) == "My reply here."


def test_clean_strips_forwarded_message_separator():
    body = "FYI below.\n\n---------- Forwarded message ----------\nFrom: a@b.com\ncontent\n"
    assert clean_email_text(body) == "FYI below."


def test_clean_strips_forwarded_header_block():
    body = (
        "See the note below.\n\n"
        "From: Alice <alice@x.com>\n"
        "Sent: Monday, July 1, 2026 3:00 PM\n"
        "To: Bob <bob@y.com>\n"
        "Subject: Original\n\n"
        "Original body here.\n"
    )
    assert clean_email_text(body) == "See the note below."


def test_clean_strips_leading_quote_lines_without_attribution():
    body = "Here is my answer.\n> quoted line 1\n> quoted line 2\n"
    assert clean_email_text(body) == "Here is my answer."


def test_clean_strips_trailing_mobile_signature():
    body = "Sounds good to me.\n\nSent from my iPhone\n"
    assert clean_email_text(body) == "Sounds good to me."


def test_lone_from_in_prose_is_not_treated_as_a_forwarded_block():
    body = "From now on please send NDAs to me directly. Thanks!"
    assert clean_email_text(body) == body.strip()


def test_reply_prefix_detection_and_stripping():
    assert is_reply_subject("Re: Contract") is True
    assert is_reply_subject("FWD: Contract") is True
    assert is_reply_subject("Fw: Contract") is True
    assert is_reply_subject("Contract") is False
    assert strip_reply_prefixes("Re: Fwd: Contract") == "Contract"


# --------------------------------------------------------------------------- #
# has-content guard inputs + attachments
# --------------------------------------------------------------------------- #
def test_empty_email_without_attachments_has_no_content():
    raw = build_raw(subject="", body="", message_id="<empty@x.com>")
    env = envelope_from_raw(raw, require_dmarc=False)
    assert env.text == ""
    assert env.attachments == ()
    assert env.has_content is False  # the dispatch has-content guard will drop this


def test_attachment_only_email_has_content():
    raw = build_raw(
        subject="",
        body="",
        attachments=[("nda.docx", b"PKcontent", "application/vnd.openxmlformats")],
    )
    env = envelope_from_raw(raw, require_dmarc=False)
    assert len(env.attachments) == 1
    assert env.has_content is True


def test_attachment_metadata_without_spooling():
    raw = build_raw(
        attachments=[("contract.pdf", b"%PDF-1.7 bytes", "application/pdf")],
    )
    env = envelope_from_raw(raw, require_dmarc=False)
    (att,) = env.attachments
    assert att.filename == "contract.pdf"
    assert att.content_type == "application/pdf"
    assert att.size == len(b"%PDF-1.7 bytes")
    assert att.source_ref == ""  # metadata-only path


def test_attachment_bytes_are_spooled_when_spool_dir_given(tmp_path):
    raw = build_raw(
        message_id="<spooled@x.com>",
        attachments=[("nda.docx", b"docx-bytes-here", "application/octet-stream")],
    )
    env = envelope_from_raw(raw, require_dmarc=False, spool_dir=tmp_path)
    (att,) = env.attachments
    assert att.source_ref  # a real file path
    from pathlib import Path

    spooled = Path(att.source_ref)
    assert spooled.exists()
    assert spooled.read_bytes() == b"docx-bytes-here"


def test_envelope_round_trips_through_model_dump_for_persistence(tmp_path):
    from app.bot.envelope import Envelope

    raw = build_raw(attachments=[("a.pdf", b"x", "application/pdf")])
    env = envelope_from_raw(raw, require_dmarc=False, spool_dir=tmp_path)
    restored = Envelope.model_validate(env.model_dump(mode="json"))
    assert restored == env


# --------------------------------------------------------------------------- #
# poll_once — the IMAP driver (fake mailbox, no network)
# --------------------------------------------------------------------------- #
class _FakeMsg:
    def __init__(self, uid: str, raw: bytes) -> None:
        self.uid = uid
        self.raw = raw


class _FakeMailbox:
    def __init__(self, messages: list[_FakeMsg]) -> None:
        self._messages = messages
        self.flagged: list[tuple[str, bool]] = []

    def fetch(self, criteria=None, *, mark_seen: bool = True, bulk: bool = False):
        assert mark_seen is False  # poll_once must fetch WITHOUT marking seen
        return list(self._messages)

    def flag(self, uid, flag, value):  # imap-tools signature: flag(uid, flags, value)
        self.flagged.append((uid, value))

    def __enter__(self) -> _FakeMailbox:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _settings(tmp_path, **over) -> Settings:
    base = dict(
        imap_host="imap.test",
        imap_user="bot",
        imap_password="secret",
        email_require_dmarc=False,
        nda_bot_from_email="nda-bot@example.com",
        data_dir=str(tmp_path),
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def test_poll_once_normalizes_dispatches_and_marks_seen(tmp_path):
    mailbox = _FakeMailbox(
        [
            _FakeMsg("101", build_raw(message_id="<a@x.com>", subject="Review one")),
            _FakeMsg("102", build_raw(message_id="<b@x.com>", subject="Review two")),
        ]
    )
    seen: list = []
    handled = email_in.poll_once(
        _settings(tmp_path),
        mailbox_factory=lambda: mailbox,
        on_envelope=lambda env: seen.append(env) or "done",
    )
    assert handled == 2
    assert [e.event_key for e in seen] == ["email:a@x.com", "email:b@x.com"]
    # Both marked seen ONLY after dispatch ran.
    assert mailbox.flagged == [("101", True), ("102", True)]


def test_poll_once_skips_and_does_not_flag_a_message_whose_dispatch_raises(tmp_path):
    mailbox = _FakeMailbox([_FakeMsg("200", build_raw(message_id="<c@x.com>"))])

    def _boom(_env):
        raise RuntimeError("dispatch exploded")

    handled = email_in.poll_once(
        _settings(tmp_path), mailbox_factory=lambda: mailbox, on_envelope=_boom
    )
    assert handled == 0
    # NOT flagged seen -> the next poll retries (dedup makes the retry safe).
    assert mailbox.flagged == []


def test_poll_once_is_a_noop_when_imap_unconfigured(tmp_path):
    n = email_in.poll_once(_settings(tmp_path, imap_host=""))
    assert n == 0


def test_poll_once_has_content_guard_drops_empty_email(tmp_path):
    # The email-path has-content guard (PLAN §3.3 fix): an empty, attachment-less message is dropped —
    # marked seen (consumed) but never handed to the handler.
    empty = build_raw(subject="", body="", message_id="<empty@x.com>")
    mailbox = _FakeMailbox([_FakeMsg("300", empty)])
    calls: list = []
    handled = email_in.poll_once(
        _settings(tmp_path),
        mailbox_factory=lambda: mailbox,
        on_envelope=lambda env: calls.append(env) or "done",
    )
    assert handled == 0
    assert calls == []  # handler never invoked for an empty message
    assert mailbox.flagged == [("300", True)]  # still marked seen (consumed)


def test_poll_once_default_handler_claims_and_dispatches(tmp_path, monkeypatch):
    # With no injected handler, the poll loop's default orchestration claims (dedup) + routes each
    # message through the shared dispatch seam. Point dispatch at an in-memory SQLite + a fake router.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.bot.dispatch as dispatch
    import app.models  # noqa: F401 - register tables
    from app.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'inbox.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dispatch, "_default_session_factory", lambda: factory)

    routed: list = []
    mailbox = _FakeMailbox([_FakeMsg("400", build_raw(message_id="<d@x.com>"))])
    handled = email_in.poll_once(
        _settings(tmp_path),
        mailbox_factory=lambda: mailbox,
        router=lambda env: routed.append(env.event_key),
    )
    assert handled == 1
    assert routed == ["email:d@x.com"]
    with factory() as s:
        from app.bot.models import BotInbox

        row = s.query(BotInbox).filter_by(event_key="email:d@x.com").one()
        assert row.status == "done"
