"""Password-reset email delivery (PLAN §6 — "reset email wired to SMTP").

The ported reset flow (``app.api.routes_auth.reset_request``) created a single-use, time-boxed token
and only *logged* it (a documented P0 stub). This module turns that stub into real delivery, reusing
the ported SMTP transport in :mod:`app.bot.channels.email_out` (STARTTLS / implicit-TLS, argon-free —
just the wire) so there is exactly ONE smtplib configuration path in the codebase.

Why a DEDICATED builder instead of the NDA reply sink (:class:`~app.bot.channels.email_out.EmailReplySink`):

* A reset email is a FRESH message, not a reply — it must carry NO ``In-Reply-To`` / ``References``
  threading and a plain declarative subject, whereas the reply sink deliberately munges every subject
  to ``"Re: …"`` (reference §2.4). A security email that arrives as ``"Re: your NDA"`` reads as
  phishing; users must recognise it instantly.
* The body reflects **no user-controlled content** (PLAN §6): the only variable is the ``reset_link``,
  and that carries a server-minted token — nothing the requester typed (their ``user_id``, a display
  name, an arbitrary subject) is ever echoed, so the message can't be turned into an injection or
  spoof vector.

Delivery is **fail-soft** (PLAN §6 capabilities): :meth:`ResetEmailSender.send` never raises and never
touches the request's response — a missing recipient, absent SMTP config, or an SMTP error just returns
a status string. The *capability gate* itself lives at the call site (the route reads the ``email_out``
capability off ``app.state`` and skips scheduling entirely when it is off — "the ported safe no-op");
this sender additionally re-checks the raw SMTP config as defence in depth.

``transport_factory`` is injected so tests capture the outbound :class:`~email.message.EmailMessage`
with ZERO network (the "fake sink"); it defaults to the ported ``email_out`` transport.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from contextlib import AbstractContextManager
from email.message import EmailMessage
from email.utils import formataddr
from html import escape as _html_escape

# Reuse the ported SMTP transport verbatim (STARTTLS/implicit-TLS + login + quit). It is a
# module-private helper, but ``app/bot`` is frozen for this wave so it cannot be re-exported under a
# public name; importing it here is the intended "wire delivery through email_out.py (the SMTP
# sender)". A caller/test overrides it via ``transport_factory`` and never reaches the real network.
from app.bot.channels.email_out import _smtp_transport
from app.config import Settings
from app.telemetry import get_logger

log = get_logger("nda.auth.reset_email")

#: Fixed subject — declarative, recognisable, and free of any requester-supplied text.
RESET_SUBJECT = "Amperesand NDA Assistant — password reset"

#: Path of the (wave-B) reset-completion page the link points at. Kept here as a module constant
#: because there is no config field for it yet (see the config gap reported for the reset base URL).
RESET_PATH = "/reset-password"

#: Fixed body copy (no user-controlled interpolation). ``{link}`` is the ONLY substitution and is a
#: server-minted URL — see the module docstring.
_TEXT_BODY = (
    "We received a request to reset the password for your Amperesand NDA Assistant "
    "account.\n\n"
    "Open the link below within the next hour to choose a new password:\n\n"
    "{link}\n\n"
    "If you did not request this, you can safely ignore this email — your password "
    "will not change.\n"
)

_HTML_BODY = (
    '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
    'font-size:14px;line-height:1.6;color:#1d1c1d;">'
    "<p>We received a request to reset the password for your Amperesand NDA "
    "Assistant account.</p>"
    "<p>Open the link below within the next hour to choose a new password:</p>"
    '<p><a href="{href}">Reset your password</a></p>'
    "<p>If you did not request this, you can safely ignore this email — your "
    "password will not change.</p>"
    "</div>"
)

#: Type of the injectable SMTP transport (matches ``email_out._smtp_transport``).
TransportFactory = Callable[[Settings], AbstractContextManager[smtplib.SMTP]]


def build_reset_link(settings: Settings, token: str) -> str:
    """The absolute URL a recipient clicks to complete their reset.

    Uses ``settings.form_base_url`` as the public site origin when present (the only public base URL
    the frozen config exposes today — see the reported config gap for a dedicated web/reset base
    URL); falls back to a bare path when it is unset so the token still travels, just without a host.
    ``token`` is URL-safe (``secrets.token_urlsafe``) so it needs no escaping.
    """
    base = (getattr(settings, "form_base_url", "") or "").strip().rstrip("/")
    if base:
        return f"{base}{RESET_PATH}?token={token}"
    return f"{RESET_PATH}?token={token}"


def build_reset_message(
    settings: Settings, *, to_email: str, reset_link: str
) -> EmailMessage:
    """Assemble the fresh (non-threaded) reset email. Does not send.

    From = the bot's configured address; To = the account's email; Subject = the fixed
    :data:`RESET_SUBJECT`; body = fixed plaintext + HTML alternative with ONLY ``reset_link``
    interpolated (HTML-escaped in the ``href``). No threading headers are set (this is not a reply).
    """
    msg = EmailMessage()
    from_addr = (
        getattr(settings, "nda_bot_from_email", "") or settings.smtp_user or ""
    ).strip()
    msg["From"] = formataddr(("NDA Assistant", from_addr))
    msg["To"] = to_email
    msg["Subject"] = RESET_SUBJECT
    msg.set_content(_TEXT_BODY.format(link=reset_link))
    msg.add_alternative(
        _HTML_BODY.format(href=_html_escape(reset_link, quote=True)), subtype="html"
    )
    return msg


class ResetEmailSender:
    """Sends the password-reset email through the ported SMTP transport. Fail-soft (never raises).

    ``transport_factory`` defaults to ``email_out._smtp_transport``; tests inject a fake so the
    outbound message is captured with no network.
    """

    def __init__(self, *, transport_factory: TransportFactory | None = None) -> None:
        self._transport_factory = transport_factory or _smtp_transport

    def send(self, settings: Settings, *, to_email: str, reset_link: str) -> str:
        """Deliver the reset email. Returns a status string; NEVER raises.

        ``no_recipient`` (no email on the account), ``not_configured`` (SMTP creds absent),
        ``send_failed`` (SMTP error), or ``sent``. The capability gate is the caller's job; this
        re-checks the raw config as defence in depth so a direct call can't send half-configured.
        """
        if not to_email:
            log.info("auth.reset_email.no_recipient")
            return "no_recipient"
        if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
            log.warning("auth.reset_email.not_configured")
            return "not_configured"
        msg = build_reset_message(settings, to_email=to_email, reset_link=reset_link)
        try:
            with self._transport_factory(settings) as smtp:
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 — delivery failure degrades, never crashes
            log.warning("auth.reset_email.send_failed", error=type(exc).__name__)
            return "send_failed"
        log.info("auth.reset_email.sent")
        return "sent"
