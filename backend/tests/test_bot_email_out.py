"""Threaded SMTP reply delivery — the email :class:`ReplySink`.

No network: a captured fake transport records the outbound :class:`EmailMessage` so we can assert the
threading headers, ``Re:`` subject rule, multipart text+HTML rendering (mrkdwn -> HTML with escaping),
and attachments — the ported ``NDA: Reply`` / ``Format Email HTML`` behaviors (reference §2.4/§2.5).
"""

from __future__ import annotations

from contextlib import contextmanager

from app.bot.channels.email_out import (
    EmailReplySink,
    normalize_message_id,
    render_html,
    render_text_clean,
    reply_subject,
)
from app.bot.channels.protocol import OutboundAttachment, Reply
from app.bot.envelope import Envelope
from app.config import Settings


class _FakeSMTP:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def send_message(self, msg) -> None:
        self._sink.append(msg)


def _sink_with_capture(**over):
    """An EmailReplySink whose transport captures every sent message into the returned list."""
    sent: list = []

    @contextmanager
    def factory(_settings):
        yield _FakeSMTP(sent)

    base = dict(
        smtp_host="smtp.test",
        smtp_user="bot@example.com",
        smtp_password="secret",
        nda_bot_from_email="nda-bot@example.com",
    )
    base.update(over)
    settings = Settings(_env_file=None, **base)
    return EmailReplySink(settings, transport_factory=factory), sent


def _inbound(**over) -> Envelope:
    base = dict(
        channel="email",
        event_key="email:orig@partner.com",
        sender_address="bob@partner.com",
        email_message_id="orig@partner.com",
        email_subject="NDA for Acme",
        text="please review",
    )
    base.update(over)
    return Envelope(**base)


# --------------------------------------------------------------------------- #
# Delivery + threading
# --------------------------------------------------------------------------- #
def test_deliver_sends_a_threaded_reply():
    sink, sent = _sink_with_capture()
    result = sink.deliver(_inbound(), Reply(text="*Done* — no issues"))
    assert result.ok is True
    assert result.channel == "email"
    assert len(sent) == 1
    msg = sent[0]
    assert msg["To"] == "bob@partner.com"
    assert msg["From"] == "NDA Bot <nda-bot@example.com>"
    assert msg["Subject"] == "Re: NDA for Acme"
    # Threading headers reference the inbound Message-ID, angle-wrapped.
    assert msg["In-Reply-To"] == "<orig@partner.com>"
    assert msg["References"] == "<orig@partner.com>"
    assert msg["Message-ID"]  # our own fresh id


def test_subject_verbatim_when_already_reply_prefixed():
    sink, sent = _sink_with_capture()
    sink.deliver(_inbound(email_subject="Re: NDA for Acme"), Reply(text="ok"))
    assert sent[0]["Subject"] == "Re: NDA for Acme"


def test_subject_fallback_when_no_subject():
    sink, sent = _sink_with_capture()
    sink.deliver(_inbound(email_subject=""), Reply(text="ok"))
    assert sent[0]["Subject"] == "Re: your NDA"


def test_no_threading_headers_when_no_inbound_message_id():
    sink, sent = _sink_with_capture()
    sink.deliver(_inbound(email_message_id=""), Reply(text="ok"))
    assert sent[0]["In-Reply-To"] is None
    assert sent[0]["References"] is None


# --------------------------------------------------------------------------- #
# Multipart body + mrkdwn rendering / escaping
# --------------------------------------------------------------------------- #
def _bodies(msg) -> tuple[str, str]:
    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    return text, html


def test_body_is_multipart_text_and_html_with_mrkdwn_rendered():
    sink, sent = _sink_with_capture()
    sink.deliver(_inbound(), Reply(text="*Approved* — see `clause 3` and _note_"))
    text, html = _bodies(sent[0])
    # HTML: mrkdwn -> tags, wrapped in the ported inline-styled div.
    assert "<strong>Approved</strong>" in html
    assert "<em>note</em>" in html
    assert "clause 3</code>" in html
    assert "font-family:-apple-system" in html
    # Plaintext: mrkdwn stripped.
    assert "Approved" in text
    assert "*" not in text
    assert "`" not in text


def test_html_escaping_prevents_injection():
    sink, sent = _sink_with_capture()
    sink.deliver(_inbound(), Reply(text="watch <script>alert(1)</script> & stuff"))
    _text, html = _bodies(sent[0])
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "&amp; stuff" in html


def test_caller_supplied_html_and_text_override_defaults():
    sink, sent = _sink_with_capture()
    sink.deliver(
        _inbound(),
        Reply(text="ignored", html="<div>custom</div>", text_clean="custom text"),
    )
    text, html = _bodies(sent[0])
    assert "custom text" in text
    assert "<div>custom</div>" in html


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
def test_attachment_is_delivered():
    sink, sent = _sink_with_capture()
    sink.deliver(
        _inbound(),
        Reply(
            text="Here is your NDA.",
            attachments=(
                OutboundAttachment(
                    "NDA.docx", b"docx-bytes", "application/vnd.openxml"
                ),
            ),
        ),
    )
    atts = list(sent[0].iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "NDA.docx"
    assert atts[0].get_payload(decode=True) == b"docx-bytes"


# --------------------------------------------------------------------------- #
# Fail-soft
# --------------------------------------------------------------------------- #
def test_deliver_fails_soft_when_no_recipient():
    sink, sent = _sink_with_capture()
    result = sink.deliver(_inbound(sender_address=""), Reply(text="ok"))
    assert result.ok is False
    assert result.error == "no_recipient"
    assert sent == []


def test_deliver_fails_soft_when_smtp_unconfigured():
    sink, sent = _sink_with_capture(smtp_host="")
    result = sink.deliver(_inbound(), Reply(text="ok"))
    assert result.ok is False
    assert result.error == "smtp_not_configured"
    assert sent == []


def test_deliver_fails_soft_when_transport_raises():
    @contextmanager
    def boom_factory(_settings):
        raise OSError("connection refused")
        yield  # pragma: no cover

    settings = Settings(
        _env_file=None,
        smtp_host="smtp.test",
        smtp_user="u",
        smtp_password="p",
    )
    sink = EmailReplySink(settings, transport_factory=boom_factory)
    result = sink.deliver(_inbound(), Reply(text="ok"))
    assert result.ok is False
    assert result.error == "OSError"


async def test_adeliver_runs_in_thread_and_sends():
    sink, sent = _sink_with_capture()
    result = await sink.adeliver(_inbound(), Reply(text="hi"))
    assert result.ok is True
    assert len(sent) == 1


# --------------------------------------------------------------------------- #
# Pure renderers (ported §2.5 / §2.4)
# --------------------------------------------------------------------------- #
def test_render_html_emoji_and_markup():
    html = render_html(":lock: keep this `secret` and *safe*")
    assert "\U0001f512" in html  # :lock: -> 🔒
    assert "<code" in html
    assert "<strong>safe</strong>" in html


def test_render_text_clean_strips_markup():
    assert (
        render_text_clean("*bold* and `code` and _italic_")
        == "bold and code and italic"
    )


def test_reply_subject_rules():
    assert reply_subject("Contract") == "Re: Contract"
    assert reply_subject("re: Contract") == "re: Contract"
    assert reply_subject("RE: Contract") == "RE: Contract"
    assert reply_subject("") == "Re: your NDA"
    assert reply_subject(None) == "Re: your NDA"


def test_normalize_message_id_wraps_brackets():
    assert normalize_message_id("abc@x.com") == "<abc@x.com>"
    assert normalize_message_id("<abc@x.com>") == "<abc@x.com>"
    assert normalize_message_id("") == ""
