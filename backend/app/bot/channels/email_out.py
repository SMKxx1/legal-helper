"""Threaded SMTP reply delivery — the email :class:`ReplySink` (PLAN §3.3 step 5).

Ports the n8n ``NDA: Reply`` / ``NDA: Reply File`` email branch (reference §2.4, §2.5) to stdlib
``smtplib`` + ``email.message.EmailMessage``:

* **Threading** — ``In-Reply-To`` and ``References`` are set to the inbound Message-ID (normalized to
  ``<...>``), so the reply lands in the original mail thread (reference §2.4 ``normId``).
* **Subject** — the original subject verbatim when it already matches ``^re:`` (case-insensitive), else
  ``"Re: " + subject``; ``"Re: your NDA"`` when there is no subject (reference §2.4).
* **Body** — a multipart ``text/plain`` + ``text/html`` alternative. The HTML is rendered from the
  Slack-mrkdwn ``reply.text`` with ``&<>`` escaped FIRST (XSS mitigation) then the mrkdwn→HTML swaps,
  wrapped in the ported inline-styled ``<div>`` (reference §2.5); the plaintext is the mrkdwn-stripped
  form. A caller may override either via ``Reply.html`` / ``Reply.text_clean``.
* **Attachments** — each :class:`OutboundAttachment` is added with its own MIME type (the ported "only
  attach when bytes are present" rule).

The blocking ``smtplib`` send is isolated: ``deliver`` is synchronous and ``adeliver`` runs it in a
thread executor (``asyncio.to_thread``) so an async caller (worker/router) never blocks the event loop
on the network. Fail-soft (PLAN §6): a missing recipient or an SMTP error returns ``ok=False`` — it
never raises into the caller. (The ``email_out`` CAPABILITY already gates on the SMTP config, so a sink
is only constructed when delivery is wired; a runtime send failure degrades the one reply.)
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape as _html_escape

from ...config import Settings
from ...telemetry import get_logger
from ..envelope import Envelope
from .protocol import Reply, ReplyResult

log = get_logger("nda.bot.email_out")

#: Slack-mrkdwn emoji shortcodes the old ``Format Email HTML`` node swapped to unicode (reference §2.5).
_EMOJI = {":lock:": "\U0001f512", ":information_source:": "ℹ️"}

_RE_CODE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*([^*]+)\*")
_RE_ITALIC = re.compile(r"_([^_]+)_")
_RE_IS_REPLY = re.compile(r"^re:", re.IGNORECASE)

_HTML_WRAP_OPEN = (
    '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
    'font-size:14px;line-height:1.6;color:#1d1c1d;">'
)
_HTML_WRAP_CLOSE = "</div>"
_CODE_OPEN = '<code style="background:#f3f3f3;padding:1px 4px;border-radius:3px;">'


def _apply_emoji(text: str) -> str:
    for code, glyph in _EMOJI.items():
        text = text.replace(code, glyph)
    return text


def render_html(text: str) -> str:
    """Slack-mrkdwn ``text`` -> the HTML body (ported ``Format Email HTML``, reference §2.5).

    Order matters and is preserved from the original: HTML-escape ``&<>`` FIRST (so mrkdwn we inject
    is the only markup), then emoji shortcodes, then ``code`` / ``*bold*`` / ``_italic_`` / newline,
    then wrap in the inline-styled div.
    """
    body = _html_escape(text, quote=False)
    body = _apply_emoji(body)
    body = _RE_CODE.sub(lambda m: f"{_CODE_OPEN}{m.group(1)}</code>", body)
    body = _RE_BOLD.sub(r"<strong>\1</strong>", body)
    body = _RE_ITALIC.sub(r"<em>\1</em>", body)
    body = body.replace("\n", "<br>")
    return f"{_HTML_WRAP_OPEN}{body}{_HTML_WRAP_CLOSE}"


def render_text_clean(text: str) -> str:
    """Slack-mrkdwn ``text`` -> a plaintext body: strip ``*`` / ``` ``` ```, unwrap ``_italic_``, same
    emoji swaps (the ported ``replyTextClean`` path, reference §2.5)."""
    body = _apply_emoji(text)
    body = body.replace("`", "")
    body = _RE_BOLD.sub(r"\1", body)
    body = _RE_ITALIC.sub(r"\1", body)
    return body


def reply_subject(original: str | None) -> str:
    """The threaded reply subject (reference §2.4): verbatim if already ``Re:``-prefixed, else prefix
    ``"Re: "``; ``"Re: your NDA"`` when there is no original subject."""
    subject = (original or "").strip()
    if not subject:
        return "Re: your NDA"
    if _RE_IS_REPLY.match(subject):
        return subject
    return f"Re: {subject}"


def normalize_message_id(message_id: str) -> str:
    """Wrap a bare Message-ID in ``<...>`` (the ported ``normId``, reference §2.4). Blank stays blank."""
    mid = (message_id or "").strip()
    if not mid:
        return ""
    if not mid.startswith("<"):
        mid = "<" + mid
    if not mid.endswith(">"):
        mid = mid + ">"
    return mid


def build_message(settings: Settings, envelope: Envelope, reply: Reply) -> EmailMessage:
    """Assemble the threaded multipart reply for ``envelope`` (does not send).

    From = the bot's configured address; To = the inbound sender; Subject = the ported ``Re:`` rule;
    ``In-Reply-To`` / ``References`` = the inbound Message-ID (only when one exists — a synthesized
    fallback key is NOT a real id and must not become a threading header). Body = text + HTML
    alternative; attachments follow.
    """
    msg = EmailMessage()
    from_addr = (
        settings.nda_bot_from_email or envelope.from_email or settings.smtp_user
    ).strip()
    msg["From"] = formataddr(("NDA Bot", from_addr))
    msg["To"] = envelope.sender_address
    msg["Subject"] = reply_subject(envelope.email_subject)

    threaded = normalize_message_id(envelope.email_message_id)
    if threaded:
        msg["In-Reply-To"] = threaded
        msg["References"] = threaded
    # A fresh, valid Message-ID for our own outbound mail (domain from the bot's From address).
    domain = from_addr.rpartition("@")[2] or "example.com"
    msg["Message-ID"] = make_msgid(domain=domain)

    text_body = (
        reply.text_clean
        if reply.text_clean is not None
        else render_text_clean(reply.text)
    )
    html_body = reply.html if reply.html is not None else render_html(reply.text)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    for att in reply.attachments:
        maintype, _, subtype = (
            att.content_type or "application/octet-stream"
        ).partition("/")
        msg.add_attachment(
            att.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename or "document",
        )
    return msg


@contextmanager
def _smtp_transport(settings: Settings) -> Iterator[smtplib.SMTP]:
    """Open an authenticated SMTP connection per the ported transport rules (reference §2.4):
    ``smtp_secure`` => implicit TLS (``SMTP_SSL``, typically 465); otherwise STARTTLS on submission
    (typically 587). Always ``quit()`` on exit."""
    context = ssl.create_default_context()
    smtp: smtplib.SMTP
    if settings.smtp_secure:
        smtp = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=30, context=context
        )
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    try:
        if not settings.smtp_secure:
            smtp.starttls(context=context)
        smtp.login(settings.smtp_user, settings.smtp_password)
        yield smtp
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001 — teardown must not mask a real send error
            pass


class EmailReplySink:
    """The email implementation of :class:`~app.bot.channels.protocol.ReplySink` (reference §2.4/§2.5).

    ``transport_factory`` is injected so tests can capture the outbound message with zero network; it
    defaults to the real STARTTLS/implicit-TLS ``smtplib`` transport above.
    """

    channel = "email"

    def __init__(
        self,
        settings: Settings,
        *,
        transport_factory: (
            Callable[[Settings], AbstractContextManager[smtplib.SMTP]] | None
        ) = None,
    ) -> None:
        self._settings = settings
        self._transport_factory = transport_factory or _smtp_transport

    def deliver(self, envelope: Envelope, reply: Reply) -> ReplyResult:
        """Send ``reply`` as a threaded email to the inbound sender. Fail-soft — returns ``ok=False``
        (never raises) on missing config, no recipient, or an SMTP error."""
        s = self._settings
        if not (s.smtp_host and s.smtp_user and s.smtp_password):
            log.warning("email_out.not_configured")
            return ReplyResult(ok=False, channel="email", error="smtp_not_configured")
        if not envelope.sender_address:
            log.warning("email_out.no_recipient", event_key=envelope.event_key)
            return ReplyResult(ok=False, channel="email", error="no_recipient")

        msg = build_message(s, envelope, reply)
        try:
            with self._transport_factory(s) as smtp:
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 — delivery failure degrades the turn, never crashes
            log.exception("email_out.send_failed", to=envelope.sender_address)
            return ReplyResult(ok=False, channel="email", error=type(exc).__name__)

        log.info(
            "email_out.sent",
            to=envelope.sender_address,
            subject=msg["Subject"],
            attachments=len(reply.attachments),
        )
        return ReplyResult(
            ok=True,
            channel="email",
            detail=msg["Message-ID"] or "",
            meta={"to": envelope.sender_address, "subject": msg["Subject"] or ""},
        )

    async def adeliver(self, envelope: Envelope, reply: Reply) -> ReplyResult:
        """Async wrapper: run the blocking ``deliver`` in a thread so the event loop is never blocked
        on the SMTP round-trip (PLAN §3.3 "threaded SMTP")."""
        return await asyncio.to_thread(self.deliver, envelope, reply)
