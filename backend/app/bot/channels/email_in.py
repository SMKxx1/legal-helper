"""IMAP email intake — raw RFC822 -> normalized :class:`Envelope` (PLAN §3.3).

Ports the n8n Router's email front door (reference §3.1 ``Normalize (Email)`` + ``Clean Email Text``)
and adds the two PLAN §3.3 hardening steps the old flow lacked:

* **Quoted-history cleaning** (ported verbatim in behavior, reference §3.1): a reply/forward is trimmed
  to just the new message — ``On … wrote:`` attributions, ``----- Original Message -----`` /
  ``---------- Forwarded message ----------`` separators, forwarded ``From:/Sent:/To:/Subject:`` header
  blocks, leading ``>`` quote lines, and a trailing ``Sent from my …`` signature are all stripped, and
  ``re:``/``fwd:``/``fw:`` subject prefixes are detected.
* **Sender authenticity** (PLAN §3.3, §6 — NEW): the ``Authentication-Results`` header is parsed for
  SPF/DKIM/DMARC pass + From-domain alignment => ``verified_sender``. With ``email_require_dmarc`` set
  (the default), mail that does not authenticate is marked UNTRUSTED (``verified_sender=False``): it is
  still processed for help/template, but the allowlist and every action-triggering intent refuse it
  (that refusal is the router's gate, keyed on this flag). With ``email_require_dmarc`` false (a
  trusted-relay dev setup) mail is trusted unconditionally.
* **has-content guard** — applied downstream in :func:`app.bot.dispatch.process_envelope` (both
  channels now), closing the old email-path gap (reference §9 gap 6).

The pure parser :func:`envelope_from_raw` does no network and no global state, so the whole
normalization + cleaning + auth matrix is unit-tested from fabricated ``bytes``. :func:`poll_once` is
the thin IMAP driver the worker schedules: it fetches ``UNSEEN`` without marking seen, normalizes each
message, hands it to the dispatch seam, then marks it seen only once it has been durably claimed —
at-least-once intake made exactly-once by the fail-closed dedup.
"""

from __future__ import annotations

import email
import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from email.message import EmailMessage, Message
from email.policy import default as _default_policy
from email.utils import parseaddr
from html import unescape as _html_unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...config import Settings
from ...telemetry import get_logger
from ..envelope import AttachmentRef, Envelope

if TYPE_CHECKING:
    from ..dispatch import Router

log = get_logger("nda.bot.email_in")

# --------------------------------------------------------------------------- #
# Quoted-history cleaning (ported from the n8n Router "Clean Email Text" node)
# --------------------------------------------------------------------------- #
#: Gmail/Apple-style attribution line: "On <date/anything> … wrote:" — may wrap across lines, so DOTALL
#: with a bounded lazy span finds the nearest "wrote:" at a line end.
_RE_ON_WROTE = re.compile(
    r"^On\b.{0,400}?\bwrote:[ \t]*$", re.IGNORECASE | re.MULTILINE | re.DOTALL
)
#: "-----Original Message-----" (any dash run / spacing).
_RE_ORIGINAL_MSG = re.compile(
    r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE
)
#: "---------- Forwarded message ----------" (Gmail) and "-----Forwarded Message-----" variants.
_RE_FORWARDED = re.compile(
    r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE
)
#: A trailing mobile signature: "Sent from my iPhone/Android/…" — cut it and everything after.
_RE_SENT_FROM = re.compile(r"^\s*Sent from my\b.*$", re.IGNORECASE | re.MULTILINE)
#: A forwarded/quoted header block start.
_RE_FROM_HEADER = re.compile(r"^\s*From:\s.+$", re.IGNORECASE | re.MULTILINE)
#: The companion header lines that confirm a "From:" is a forwarded-header block, not prose.
_RE_BLOCK_CONFIRM = re.compile(r"^\s*(Sent|Date|To|Subject|Cc):\s", re.IGNORECASE)
#: A quoted line (leading ">", optionally after whitespace).
_RE_QUOTE_LINE = re.compile(r"^\s*>")
#: re/fwd/fw subject prefixes (possibly stacked, e.g. "Re: Fwd:").
_RE_REPLY_PREFIX = re.compile(r"^\s*((?:re|fwd?|fw)\s*:\s*)+", re.IGNORECASE)


def _forwarded_header_block_offset(text: str) -> int | None:
    """The char offset of a forwarded ``From:`` header block (a ``From:`` line immediately followed,
    within a few lines, by ``Sent:/Date:/To:/Subject:/Cc:``), or ``None``. A lone ``From:`` in prose
    is NOT a cut point — the confirming header line is what distinguishes a forwarded block."""
    for m in _RE_FROM_HEADER.finditer(text):
        rest = text[m.end() :].split("\n")[:5]
        if any(_RE_BLOCK_CONFIRM.match(line) for line in rest):
            return m.start()
    return None


def clean_email_text(body: str) -> str:
    """Strip quoted history from an email body, leaving only the newly-typed message (reference §3.1).

    Cuts at the EARLIEST of the quoted-history markers (``On … wrote:``, ``Original Message`` /
    ``Forwarded message`` separators, a forwarded header block), then drops any leftover leading ``>``
    quote lines and a trailing ``Sent from my …`` signature, and collapses the whitespace.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")

    cut = len(text)
    for rx in (_RE_ON_WROTE, _RE_ORIGINAL_MSG, _RE_FORWARDED, _RE_SENT_FROM):
        m = rx.search(text)
        if m is not None and m.start() < cut:
            cut = m.start()
    fwd = _forwarded_header_block_offset(text)
    if fwd is not None and fwd < cut:
        cut = fwd
    text = text[:cut]

    # Drop any remaining quoted (">") lines — top-posted quotes without an attribution line.
    lines = [ln for ln in text.split("\n") if not _RE_QUOTE_LINE.match(ln)]
    text = "\n".join(lines)

    # Collapse 3+ blank lines to one and trim.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def is_reply_subject(subject: str) -> bool:
    """True when ``subject`` carries a ``re:``/``fwd:``/``fw:`` prefix (reply/forward detection)."""
    return bool(_RE_REPLY_PREFIX.match(subject or ""))


def strip_reply_prefixes(subject: str) -> str:
    """Remove leading ``re:``/``fwd:``/``fw:`` prefixes (possibly stacked) from a subject."""
    return _RE_REPLY_PREFIX.sub("", subject or "").strip()


# --------------------------------------------------------------------------- #
# Sender authenticity (Authentication-Results parsing — PLAN §3.3, §6)
# --------------------------------------------------------------------------- #
def sender_is_verified(msg: Message, from_domain: str, *, require_dmarc: bool) -> bool:
    """Decide ``verified_sender`` from the ``Authentication-Results`` header(s) (PLAN §3.3).

    When ``require_dmarc`` is False the sender is trusted unconditionally (a trusted-relay dev setup).
    Otherwise the message is verified iff it authenticated with From-domain alignment:

    * ``dmarc=pass`` (DMARC pass inherently requires SPF- or DKIM-alignment with the From domain) — and,
      when the result records ``header.from=``, it must match the actual From domain; or
    * both ``spf=pass`` and ``dkim=pass``, with the DKIM ``header.d=`` domain aligned to the From
      domain (when a ``header.d=`` is present to check).

    Anything else — no header, a ``fail``/``none`` result, or a domain mismatch — is UNTRUSTED.
    """
    if not require_dmarc:
        return True
    headers = msg.get_all("Authentication-Results") or []
    blob = " ".join(str(h) for h in headers).lower()
    if not blob:
        return False

    from_domain = (from_domain or "").lower().strip()
    dmarc_pass = re.search(r"\bdmarc=pass\b", blob) is not None
    dkim_pass = re.search(r"\bdkim=pass\b", blob) is not None
    spf_pass = re.search(r"\bspf=pass\b", blob) is not None

    if dmarc_pass:
        m = re.search(r"dmarc=pass[^;]*?header\.from=([^\s;]+)", blob)
        if m and from_domain:
            claimed = m.group(1).strip("\"'").lower()
            if not _domains_align(claimed, from_domain):
                return False
        return True

    if dkim_pass and spf_pass:
        m = re.search(r"dkim=pass[^;]*?header\.d=([^\s;]+)", blob)
        if m and from_domain:
            return _domains_align(m.group(1).strip("\"'").lower(), from_domain)
        return True

    return False


def _domains_align(a: str, b: str) -> bool:
    """Organizational-domain alignment (exact, or one is a subdomain of the other)."""
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# --------------------------------------------------------------------------- #
# HTML-body fallback (only used when there is no text/plain part)
# --------------------------------------------------------------------------- #
_RE_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)\b.*?>.*?</\1>")
_RE_BR = re.compile(r"(?i)<br\s*/?>")
_RE_BLOCK_END = re.compile(r"(?i)</(p|div|tr|li|h[1-6])>")
_RE_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    text = _RE_SCRIPT_STYLE.sub(" ", html)
    text = _RE_BR.sub("\n", text)
    text = _RE_BLOCK_END.sub("\n", text)
    text = _RE_TAG.sub(" ", text)
    text = _html_unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- #
# Normalization: raw RFC822 -> Envelope
# --------------------------------------------------------------------------- #
def _strip_brackets(message_id: str | None) -> str:
    return (message_id or "").strip().strip("<>").strip()


def _extract_body(msg: EmailMessage) -> str:
    """The message body as plaintext: prefer ``text/plain``; else strip a ``text/html`` part; else ''."""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return str(part.get_content())
        hpart = msg.get_body(preferencelist=("html",))
        if hpart is not None:
            return _html_to_text(str(hpart.get_content()))
    except Exception:  # noqa: BLE001 — a malformed part must never crash intake; degrade to no body
        log.warning("email_in.body_extract_failed")
    return ""


_RE_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _spool_attachments(
    msg: EmailMessage, spool_dir: Path, key: str
) -> list[AttachmentRef]:
    """Write each attachment's bytes under ``spool_dir`` (restart-safe) and return refs whose
    ``source_ref`` is the absolute file path a downstream handler reads. Metadata (filename, MIME,
    size) is captured either way."""
    spool_dir.mkdir(parents=True, exist_ok=True)
    safe_key = _RE_UNSAFE_FILENAME.sub("_", key)[:64]
    refs: list[AttachmentRef] = []
    for i, part in enumerate(msg.iter_attachments()):
        filename = part.get_filename() or f"attachment-{i}"
        payload = part.get_payload(decode=True) or b""
        safe_name = _RE_UNSAFE_FILENAME.sub("_", filename) or f"attachment-{i}"
        path = spool_dir / f"{safe_key}-{i}-{safe_name}"
        try:
            path.write_bytes(payload if isinstance(payload, bytes) else bytes(payload))
            source_ref = str(path)
        except Exception:  # noqa: BLE001 — spooling is best-effort; keep the metadata ref regardless
            log.warning("email_in.spool_failed", filename=filename)
            source_ref = ""
        refs.append(
            AttachmentRef(
                filename=filename,
                content_type=part.get_content_type(),
                size=len(payload),
                source_ref=source_ref,
            )
        )
    return refs


def _attachment_metadata(msg: EmailMessage) -> list[AttachmentRef]:
    """Attachment refs WITHOUT persisting bytes (metadata only; ``source_ref`` blank)."""
    refs: list[AttachmentRef] = []
    for i, part in enumerate(msg.iter_attachments()):
        payload = part.get_payload(decode=True) or b""
        refs.append(
            AttachmentRef(
                filename=part.get_filename() or f"attachment-{i}",
                content_type=part.get_content_type(),
                size=len(payload),
            )
        )
    return refs


def envelope_from_raw(
    raw: bytes,
    *,
    require_dmarc: bool = True,
    bot_from_email: str = "",
    spool_dir: Path | None = None,
) -> Envelope:
    """Parse raw RFC822 ``bytes`` into a normalized :class:`Envelope` (reference §3.1).

    * ``event_key`` = ``email:<message-id>``; when the message carries no ``Message-ID`` a deterministic
      ``nomsgid-<sha256>`` fallback keeps the dedup key non-empty and stable (a hardening over the n8n
      ``'email:'+messageId`` which produced ``'email:'`` for id-less mail).
    * ``text`` = the subject plus the quoted-history-cleaned body (subject first, matching the n8n
      ``subject + '\\n\\n' + body`` shape, so the classifier sees the subject line).
    * ``verified_sender`` comes from :func:`sender_is_verified` under ``require_dmarc``.
    * When ``spool_dir`` is given, attachment bytes are written there and each ref's ``source_ref`` is
      the file path; without it, refs are metadata-only (the unit-test path).
    """
    # policy=default makes ``message_from_bytes`` return an EmailMessage (decoded headers, get_body,
    # iter_attachments) — the modern API the parsing below relies on.
    msg = email.message_from_bytes(raw, policy=_default_policy)

    raw_from = str(msg["From"] or "")
    _display, addr = parseaddr(raw_from)
    sender_address = addr.strip().lower()
    from_domain = sender_address.rpartition("@")[2]

    subject = str(msg["Subject"] or "").strip()
    body = clean_email_text(_extract_body(msg))
    text = "\n\n".join(part for part in (subject, body) if part)

    real_mid = _strip_brackets(str(msg["Message-ID"]) if msg["Message-ID"] else "")
    key_id = real_mid or f"nomsgid-{hashlib.sha256(raw).hexdigest()[:32]}"
    event_key = f"email:{key_id}"

    if spool_dir is not None:
        attachments = _spool_attachments(msg, spool_dir, key_id)
    else:
        attachments = _attachment_metadata(msg)

    verified = sender_is_verified(msg, from_domain, require_dmarc=require_dmarc)

    return Envelope(
        channel="email",
        event_key=event_key,
        text=text,
        sender_id=raw_from.strip(),
        sender_address=sender_address,
        verified_sender=verified,
        email_message_id=real_mid,
        email_subject=subject,
        from_email=bot_from_email,
        attachments=tuple(attachments),
    )


# --------------------------------------------------------------------------- #
# IMAP poll driver (the thin worker-scheduled loop)
# --------------------------------------------------------------------------- #
def _raw_of(msg: Any) -> bytes:
    """Extract raw RFC822 bytes from an imap-tools ``MailMessage`` (``.obj.as_bytes()``) or a test
    double exposing ``.raw``."""
    raw = getattr(msg, "raw", None)
    if isinstance(raw, bytes):
        return raw
    obj = getattr(msg, "obj", None)
    if obj is not None and hasattr(obj, "as_bytes"):
        return bytes(obj.as_bytes())
    raise TypeError("mail message exposes neither .raw bytes nor .obj.as_bytes()")


def _default_mailbox_factory(
    settings: Settings,
) -> Callable[[], AbstractContextManager[Any]]:
    """Build the real imap-tools ``MailBox`` context manager (lazy import so tests never need IMAP)."""

    def _factory() -> AbstractContextManager[Any]:
        from imap_tools import MailBox

        return MailBox(settings.imap_host, port=settings.imap_port).login(
            settings.imap_user,
            settings.imap_password,
            initial_folder=settings.imap_folder,
        )

    return _factory


def poll_once(
    settings: Settings,
    *,
    mailbox_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    on_envelope: Callable[[Envelope], str] | None = None,
    router: Router | None = None,
) -> int:
    """Fetch every ``UNSEEN`` message once, normalize + guard + claim + route it, then mark it seen.
    Returns the number of messages handed to the handler.

    Channel-side orchestration mirrors the Slack handler (PLAN §3.3): the has-content guard and the
    fail-closed dedup CLAIM live here (the channel owns the ``bot_inbox`` row); the shared
    ``dispatch.process_envelope`` seam does only the routing.

    Fault isolation (PLAN house rules): messages are fetched WITHOUT ``mark_seen`` and flagged seen only
    AFTER the handler has durably claimed the event — so a crash before the claim leaves the message
    ``UNSEEN`` for the next poll (at-least-once), while the fail-closed dedup makes reprocessing a no-op
    (exactly-once effect). A parse/handler error on one message is logged and skipped; it never aborts
    the batch.
    """
    if not settings.imap_host:
        return 0
    factory = mailbox_factory or _default_mailbox_factory(settings)
    handle = on_envelope or _default_handler(router)

    handled = 0
    with factory() as mailbox:
        for msg in _fetch_unseen(mailbox):
            uid = getattr(msg, "uid", None)
            try:
                envelope = envelope_from_raw(
                    _raw_of(msg),
                    require_dmarc=settings.email_require_dmarc,
                    bot_from_email=settings.nda_bot_from_email,
                    spool_dir=settings.uploads_path,
                )
            except Exception:  # noqa: BLE001 — one unparseable message must not stall the mailbox
                log.exception("email_in.parse_failed", uid=uid)
                continue
            # Has-content guard on the email path (PLAN §3.3 — the deliberate fix; the old flow ran this
            # only on Slack). An empty, attachment-less message carries no work: mark it seen (consumed)
            # and move on without claiming/routing.
            if not envelope.has_content:
                log.info("email_in.drop_empty", uid=uid, event_key=envelope.event_key)
                _mark_seen(mailbox, uid)
                continue
            try:
                outcome = handle(envelope)
            except Exception:  # noqa: BLE001 — the handler is fail-soft, but never abort the batch
                log.exception("email_in.dispatch_failed", event_key=envelope.event_key)
                # Do NOT mark seen: leave it UNSEEN so the next poll retries (dedup makes it safe).
                continue
            _mark_seen(mailbox, uid)
            handled += 1
            log.info(
                "email_in.handled",
                uid=uid,
                event_key=envelope.event_key,
                outcome=outcome,
            )
    return handled


def _default_handler(router: Router | None) -> Callable[[Envelope], str]:
    """The per-message orchestration the poll loop runs: fail-closed dedup CLAIM -> route -> finalize
    (the same ``bot_inbox`` lifecycle the Slack handler runs inline). Returns a short outcome string for
    the poll log."""

    def _handle(envelope: Envelope) -> str:
        from ..dispatch import (
            STATUS_DONE,
            STATUS_FAILED,
            claim,
            finalize,
            process_envelope,
        )

        inbox_id = claim(envelope)
        if inbox_id is None:
            log.info("email_in.duplicate", event_key=envelope.event_key)
            return "duplicate"
        try:
            process_envelope(envelope, router=router)
        except Exception as exc:  # noqa: BLE001 — record failure on the row for the sweep to retry
            log.exception("email_in.route_failed", event_key=envelope.event_key)
            finalize(
                inbox_id,
                STATUS_FAILED,
                error=f"{type(exc).__name__}: routing failed (see server logs)",
            )
            return "failed"
        finalize(inbox_id, STATUS_DONE)
        return "done"

    return _handle


def _fetch_unseen(mailbox: Any) -> Iterator[Any]:
    """Fetch UNSEEN without marking seen — imap-tools when available, else a duck-typed fetch."""
    try:
        from imap_tools import AND

        return iter(mailbox.fetch(AND(seen=False), mark_seen=False, bulk=True))
    except (
        ImportError
    ):  # pragma: no cover — imap-tools is a pinned dep; the test double skips this
        return iter(mailbox.fetch(mark_seen=False))


def _mark_seen(mailbox: Any, uid: Any) -> None:
    if uid is None:
        return
    try:
        from imap_tools import MailMessageFlags

        mailbox.flag(uid, MailMessageFlags.SEEN, True)
    except ImportError:  # pragma: no cover — the test double exposes flag() directly
        mailbox.flag(uid, "\\Seen", True)
    except Exception:  # noqa: BLE001 — a flag failure is non-fatal (dedup guards a re-fetch)
        log.warning("email_in.flag_seen_failed", uid=uid)
