"""The channel-agnostic reply contract (PLAN §3.3 step 5 — "one channel-aware reply service").

The n8n stack delivered replies through two shared sub-workflows — ``NDA: Reply`` (text) and
``NDA: Reply File`` (file) — each branching Slack-vs-email internally (reference §3.8/§3.9). Here that
becomes a typed :class:`ReplySink` protocol: one implementation per outbound channel
(``EmailReplySink`` in :mod:`app.bot.channels.email_out`, a Slack sink beside it), each consuming the
same immutable :class:`Reply`. An intent handler builds a ``Reply`` once and hands it to whichever sink
matches ``envelope.channel`` — it never learns SMTP threading or Slack Block Kit.

This module is intentionally dependency-free (stdlib + the envelope type) so BOTH the email agent and
the Slack agent can implement against it without importing each other. If it already exists when the
Slack agent arrives it implements the same ``ReplySink``; this is the minimal shared seam.

``deliver`` is synchronous — it is the lowest common denominator (stdlib ``smtplib`` and the Slack
``WebClient`` are both blocking). Async callers (the worker, the router) run it off the event loop with
``await asyncio.to_thread(sink.deliver, envelope, reply)``; ``EmailReplySink`` also offers a thin
``adeliver`` convenience that does exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..envelope import Envelope


@dataclass(frozen=True)
class OutboundAttachment:
    """A file to attach to a reply — the bytes, its name, and its MIME type.

    Unlike the inbound :class:`~app.bot.envelope.AttachmentRef` (metadata + a lazy handle), an outbound
    attachment carries the actual ``content`` because the sink is about to write it to the wire.
    """

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class Reply:
    """A channel-agnostic reply payload built by an intent handler.

    ``text`` is Slack-mrkdwn (``*bold*``, ``_italic_``, ``:lock:`` …) — the single source format the
    old system authored in. The Slack sink posts it verbatim; the email sink renders it to HTML
    (``render_html``) and a mrkdwn-stripped plaintext (``render_text_clean``), matching the ported
    ``Format Email HTML`` node (reference §2.5). ``html`` / ``text_clean`` let a caller override those
    renderings; leaving them ``None`` uses the ported defaults. ``attachments`` present => the sink
    takes its file-delivery path (Slack upload / email attachment).
    """

    text: str = ""
    html: str | None = None
    text_clean: str | None = None
    attachments: tuple[OutboundAttachment, ...] = ()


@dataclass(frozen=True)
class ReplyResult:
    """The outcome of a delivery attempt. Fail-soft: a sink returns ``ok=False`` (never raises) so a
    delivery failure degrades the turn, it does not crash the worker/router path."""

    ok: bool
    channel: str
    detail: str = ""
    error: str = ""
    meta: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class ReplySink(Protocol):
    """A channel-specific delivery surface. One per outbound channel (``email``, ``slack``).

    Implementations read the delivery context they need (recipient, thread, subject, threading id)
    from the inbound :class:`Envelope` — ``sender_address`` / ``email_message_id`` / ``email_subject``
    for email, ``slack_channel`` / ``slack_thread_ts`` for Slack — so the caller passes the same two
    objects to any sink.
    """

    #: The channel this sink delivers to — matched against ``Envelope.channel`` by the reply service.
    channel: str

    def deliver(self, envelope: Envelope, reply: Reply) -> ReplyResult:
        """Deliver ``reply`` in the context of the inbound ``envelope``. Never raises (fail-soft)."""
        ...
