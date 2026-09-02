"""``run_generation`` — the generate seam (PLAN §3.6, reference §3.4).

The in-house replacement for the n8n ``NDA: Tally Callback`` workflow — and, since the in-house ``/f``
form service was retired in favour of the external **Tally** form, this is the seam the Tally webhook
(:mod:`app.api.routes_tally`) calls once a submission arrives. Given the derived engine token table +
routing selectors + the originating conversation, it:

1. resolves the current ``tokenised`` template .docx for the (jurisdiction, counterparty, mutuality)
   combo and fills it (``app.support_task``: ``normalize_codes`` → ``resolve_template_docx`` →
   ``fill_docx`` with ``strip_unfilled``);
2. delivers the finished ``NDA.docx`` back into the ORIGINATING conversation (reconstructed from the
   ``origin_context``) through the wired channel-aware
   :class:`~app.bot.channels.replies.ReplyService`;
3. offers DocuSign — on Slack a "Send via DocuSign? → Yes" button carrying the typed value
   ``{v:1, kind:"send_docusign", ref}`` (the envelope intent registers the handler; this flow only
   emits the exact shape). The email reply-to-send offer is P3-envelope scope and is not built here.

Engine failures map to the ported friendly replies (reference §3.4 "Generate NDA Error"); a missing /
unloaded template degrades to a friendly, combo-named "not loaded yet", never a broken document.

Every collaborator (the DB ``session_factory``, the reply ``service`` / ``post_blocks``, the template
``resolve``/``fill``, ``settings``) is an injected parameter resolved lazily, so the whole flow is
unit-tested with a throwaway SQLite factory, a capturing reply service, and stubbed fill — zero
network, no LLM (PLAN house rules).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...telemetry import get_logger

if TYPE_CHECKING:
    from ...config import Settings
    from ..envelope import Envelope

log = get_logger("nda.bot.flow.generate_completion")

#: A zero-arg factory yielding a SQLAlchemy ``Session`` (a ``sessionmaker`` satisfies this).
SessionFactory = Callable[[], Any]

# --------------------------------------------------------------------------- #
# Preserved copy + contract identifiers (reference §3.4, §8)
# --------------------------------------------------------------------------- #
#: The delivered completed-NDA filename (reference §3.4 ``Generated NDA Reply`` / §8 default filenames).
GENERATED_NDA_FILENAME = "NDA.docx"
#: The ported file-reply caption (reference §3.4 ``Generated NDA Reply`` replyText).
GENERATED_NDA_CAPTION = "Here is your completed NDA (.docx)."

#: The post-generation DocuSign offer card (reference §3.4 "Send via DocuSign? Yes" — PLAN §3.9b).
DOCUSIGN_OFFER_SECTION = "Send this NDA to DocuSign for signature?"
DOCUSIGN_OFFER_BUTTON = "Yes"
DOCUSIGN_OFFER_FALLBACK = "Send this NDA to DocuSign for signature?"

#: The DocuSign-offer button ``action_id`` — the PRESERVED contract the envelope agent's interactivity
#: registration routes on (reference §8 ``send_docusign``). Kept verbatim so the button reaches the
#: right handler.
ACTION_SEND_DOCUSIGN = "send_docusign"
#: The interactivity KIND the typed button value carries (the envelope agent registers the handler).
KIND_SEND_DOCUSIGN = "send_docusign"
#: Typed button-value version pin (matches ``app.bot.interactivity.PAYLOAD_VERSION``).
PAYLOAD_VERSION = 1

# --------------------------------------------------------------------------- #
# Ported friendly error copy (reference §3.4 "Generate NDA Error" mapping)
# --------------------------------------------------------------------------- #
#: Zero-row-style guard (mirrors the ``template`` intent copy) — names the exact combo, never ships a
#: broken document (reference §9 gap 7).
TEMPLATE_NOT_LOADED_TEXT = (
    "I don't have the *{combo}* NDA template loaded yet, so I couldn't generate your "
    "document. Ask an admin to upload it, then try again."
)
NOT_AUTHORIZED_TEXT = (
    "You're not authorized to generate that NDA. Please ask the team for access."
)
AUTH_REJECTED_TEXT = (
    "I couldn't authenticate to the document engine to generate your NDA. "
    "Please let the team know."
)
TOO_LARGE_TEXT = "That NDA is too large for me to generate."
COULDNT_PROCESS_TEXT = (
    "I couldn't generate the NDA from that submission — please check the details and "
    "try again."
)

#: Human display labels for the combo name in the zero-row guard.
_CP_DISPLAY = {
    "Company": "Company",
    "ServiceProvider": "Service Provider",
    "Individual": "Individual",
}


# --------------------------------------------------------------------------- #
# Result + typed button value
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CompletionResult:
    """The outcome of one generation run — what happened, for the caller's logs/telemetry.

    ``ok`` is True on a delivered completion; ``reason`` is a machine-readable outcome tag
    (``delivered`` / ``no_delivery`` / ``engine_error:<code>``). ``delivered`` is True when the document
    was delivered; ``docusign_offered`` is True when the Slack DocuSign button was posted.
    """

    ok: bool
    reason: str
    delivered: bool = False
    docusign_offered: bool = False
    token_count: int = 0


def send_docusign_button_value(ref: str) -> str:
    """The typed ``send_docusign`` button VALUE — ``{v:1, kind:"send_docusign", ref}``.

    ``ref`` keys the ``bot_correlation`` row holding the generated document
    (:func:`app.bot.intents.envelope.stash_generated_document`), so the click enters the exact same
    modal → confirm → send chain as an attached document. Compact JSON so re-emits are stable.
    """
    return json.dumps(
        {"v": PAYLOAD_VERSION, "kind": KIND_SEND_DOCUSIGN, "ref": ref},
        separators=(",", ":"),
        sort_keys=True,
    )


# --------------------------------------------------------------------------- #
# The generation runner
# --------------------------------------------------------------------------- #
def run_generation(
    *,
    values: dict[str, str],
    jurisdiction: str,
    counterparty_type: str,
    mutuality: str,
    origin_context: dict[str, Any],
    ref: str = "generate",
    service: Any | None = None,
    post_blocks: Any | None = None,
    settings: Settings | None = None,
    resolve_template: Callable[..., tuple[bytes, Any]] | None = None,
    fill: Callable[..., bytes] | None = None,
    session_factory: SessionFactory | None = None,
) -> CompletionResult:
    """Generate + deliver the NDA for the given tokens/routing, and offer DocuSign (PLAN §3.6).

    ``values`` is the ``{token: value}`` table; ``jurisdiction``/``counterparty_type``/``mutuality`` are
    the routing selectors (lenient inputs — ``normalize_codes`` canonicalizes them). ``origin_context``
    is the ``{channel, slack_channel, slack_thread_ts, sender, …}`` dict reconstructing the requester's
    conversation. ``service`` is the channel-aware :class:`ReplyService`; ``post_blocks`` is the Slack
    sink's ``post_blocks`` for the DocuSign card — both fall back to the process-wide delivery.
    ``resolve_template`` / ``fill`` default to the real ``support_task`` path; tests inject stubs. Never
    raises — every failure degrades to a friendly reply and a typed result.
    """
    from app.api.errors import EngineError
    from app.support_task.generator import normalize_codes

    from ...config import get_settings

    settings = settings or get_settings()
    factory = session_factory or _default_session_factory()
    resolve_template = resolve_template or _default_resolve()
    fill = fill or _default_fill()
    service, post_blocks = _resolve_delivery(service, post_blocks)

    origin = _origin_envelope(origin_context or {}, settings, ref)

    try:
        jur, cp, mut = normalize_codes(jurisdiction, counterparty_type, mutuality)
    except EngineError as exc:
        log.warning("bot.flow.generate.bad_routing", ref=ref, error=repr(exc))
        _deliver_text(service, origin, _engine_error_text(exc, ""))
        return CompletionResult(
            ok=False, reason=f"engine_error:{getattr(exc, 'code', '') or ''}"
        )

    combo = _combo_label(jur, cp, mut)
    try:
        with factory() as db:
            template_bytes, _template = resolve_template(
                db, jur, cp, mut, variant="tokenised"
            )
        docx = fill(template_bytes, values, strip_unfilled=True)
    except EngineError as exc:
        log.warning("bot.flow.generate.engine_error", ref=ref, error=repr(exc))
        _deliver_text(service, origin, _engine_error_text(exc, combo))
        return CompletionResult(
            ok=False, reason=f"engine_error:{getattr(exc, 'code', '') or ''}"
        )

    if service is None or origin is None:
        # Nothing wired to deliver through (or an unrecoverable origin): the document was generated but
        # there is no requester conversation to land it in (e.g. a Tally submission with no channel).
        log.warning(
            "bot.flow.generate.no_delivery",
            ref=ref,
            has_service=service is not None,
            has_origin=origin is not None,
        )
        return CompletionResult(ok=False, reason="no_delivery")

    _deliver_file(service, origin, docx)
    docusign_offered = _offer_docusign(origin, post_blocks, factory, docx, ref)
    log.info(
        "bot.flow.generate.delivered",
        ref=ref,
        channel=origin.channel,
        docusign_offered=docusign_offered,
        token_count=len(values or {}),
    )
    return CompletionResult(
        ok=True,
        reason="delivered",
        delivered=True,
        docusign_offered=docusign_offered,
        token_count=len(values or {}),
    )


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _origin_envelope(
    origin_context: dict[str, Any], settings: Settings, ref: str
) -> Envelope | None:
    """Rebuild the ORIGIN reply envelope from the captured ``origin_context``.

    Slack: needs a ``slack_channel`` (threaded on ``slack_thread_ts``). Email: needs a ``sender`` address
    (threaded via ``email_message_id`` + subject). Returns ``None`` when the minimum context to reach the
    origin is absent (logged; the run still records its outcome)."""
    from ..envelope import Envelope

    channel = str(origin_context.get("channel") or "")
    event_key = f"generate:{ref}"
    from_email = str(
        origin_context.get("from_email") or getattr(settings, "nda_bot_from_email", "")
    )
    if channel == "slack":
        slack_channel = str(origin_context.get("slack_channel") or "")
        if not slack_channel:
            return None
        return Envelope(
            channel="slack",
            event_key=event_key,
            slack_channel=slack_channel,
            slack_thread_ts=str(origin_context.get("slack_thread_ts") or ""),
            verified_sender=True,
            from_email=from_email,
        )
    if channel == "email":
        sender = str(origin_context.get("sender") or "")
        if not sender:
            return None
        return Envelope(
            channel="email",
            event_key=event_key,
            sender_address=sender,
            email_message_id=str(origin_context.get("email_message_id") or ""),
            email_subject=str(origin_context.get("email_subject") or ""),
            from_email=from_email,
        )
    return None


def _deliver_file(service: Any, origin: Envelope, docx: bytes) -> Any | None:
    """Deliver the completed .docx through the channel-aware service (Slack upload / email attachment)."""
    from app.support_task.generator import DOCX_MIME

    from ..channels.protocol import OutboundAttachment, Reply

    try:
        return service.deliver(
            origin,
            Reply(
                text=GENERATED_NDA_CAPTION,
                attachments=(
                    OutboundAttachment(
                        filename=GENERATED_NDA_FILENAME,
                        content=docx,
                        content_type=DOCX_MIME,
                    ),
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — delivery is fail-soft (never crash the intake path)
        log.warning(
            "bot.flow.generate.deliver_file_failed",
            event_key=origin.event_key,
            error=repr(exc),
        )
        return None


def _deliver_text(service: Any, origin: Envelope | None, text: str) -> Any | None:
    """Deliver a plain (mrkdwn) reply (the friendly engine-error path). No-op when nothing is wired."""
    if service is None or origin is None:
        log.info("bot.flow.generate.text_deliver_skipped")
        return None
    from ..channels.protocol import Reply

    try:
        return service.deliver(origin, Reply(text=text))
    except Exception as exc:  # noqa: BLE001 — fail-soft
        log.warning(
            "bot.flow.generate.deliver_text_failed",
            event_key=origin.event_key,
            error=repr(exc),
        )
        return None


def _offer_docusign(
    origin: Envelope,
    post_blocks: Any,
    factory: SessionFactory,
    docx_bytes: bytes,
    ref: str,
) -> bool:
    """Post the Slack "Send via DocuSign? → Yes" card (reference §3.4). Email is P3-envelope scope.

    The generated document is FIRST stashed as envelope-chain state
    (:func:`app.bot.intents.envelope.stash_generated_document`) so the button's ``ref`` value enters
    the same modal → confirm → send chain as an attached document. Returns True when the card was
    posted; when ``post_blocks`` isn't wired (or the origin is email), the offer is skipped without
    failing the run — the document itself was already delivered.
    """
    if origin.channel != "slack" or post_blocks is None:
        return False
    try:
        from ..intents.envelope import stash_generated_document

        stash_ref = stash_generated_document(
            factory, origin, docx_bytes, GENERATED_NDA_FILENAME
        )
        post_blocks(
            origin,
            _docusign_offer_blocks(stash_ref),
            DOCUSIGN_OFFER_FALLBACK,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the offer is best-effort; the doc already delivered
        log.warning(
            "bot.flow.generate.docusign_offer_failed",
            event_key=origin.event_key,
            ref=ref,
            error=repr(exc),
        )
        return False


def _docusign_offer_blocks(ref: str) -> list[dict]:
    """The DocuSign offer card: a prompt section + a primary Yes button carrying the typed value."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": DOCUSIGN_OFFER_SECTION},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_SEND_DOCUSIGN,
                    "style": "primary",
                    "text": {
                        "type": "plain_text",
                        "text": DOCUSIGN_OFFER_BUTTON,
                        "emoji": True,
                    },
                    "value": send_docusign_button_value(ref),
                }
            ],
        },
    ]


# --------------------------------------------------------------------------- #
# Error mapping + small helpers
# --------------------------------------------------------------------------- #
def _engine_error_text(exc: Any, combo: str) -> str:
    """Map an ``EngineError`` to the ported friendly text (reference §3.4 "Generate NDA Error")."""
    status = getattr(exc, "status", 0) or 0
    code = getattr(exc, "code", "") or ""
    if code in ("template_not_found", "template_blob_missing") or status in (404, 409):
        return TEMPLATE_NOT_LOADED_TEXT.format(combo=combo or "requested")
    if code == "not_entitled" or status == 403:
        return NOT_AUTHORIZED_TEXT
    if code == "unauthorized" or status == 401:
        return AUTH_REJECTED_TEXT
    if code == "request_too_large" or status == 413:
        return TOO_LARGE_TEXT
    if code in ("bad_request", "unprocessable", "bad_template") or status in (400, 422):
        return COULDNT_PROCESS_TEXT
    from ..router import ERROR_REPLY_TEXT

    return ERROR_REPLY_TEXT


def _combo_label(jur: str, cp: str, mut: str) -> str:
    """Human-readable selector combo for the zero-row reply (``US / Company``, ``SG / Individual / Mutual``)."""
    parts = [jur, _CP_DISPLAY.get(cp, cp)]
    if cp == "Individual" and mut and mut != "NotApplicable":
        parts.append(mut)
    return " / ".join(p for p in parts if p)


def _resolve_delivery(service: Any, post_blocks: Any) -> tuple[Any, Any]:
    """Resolve the reply service + block poster: an explicit ``service`` wins; else the process-wide
    delivery wired by :func:`app.bot.router.configure_delivery` (the same source the router/interactivity
    deliver through). Returns ``(None, post_blocks)`` when nothing is wired."""
    if service is not None:
        return service, post_blocks
    try:
        from ..router import _DELIVERY
    except Exception:  # noqa: BLE001 — router import failure must not crash generation
        return None, post_blocks
    if _DELIVERY is not None:
        svc, pb = _DELIVERY
        return svc, (post_blocks if post_blocks is not None else pb)
    return None, post_blocks


def _default_session_factory() -> SessionFactory:
    from app.db import SessionLocal

    return SessionLocal


def _default_resolve() -> Callable[..., tuple[bytes, Any]]:
    from app.support_task import resolve_template_docx

    return resolve_template_docx


def _default_fill() -> Callable[..., bytes]:
    from app.support_task import fill_docx

    return fill_docx


__all__ = [
    "CompletionResult",
    "run_generation",
    "send_docusign_button_value",
    "GENERATED_NDA_FILENAME",
    "GENERATED_NDA_CAPTION",
    "ACTION_SEND_DOCUSIGN",
    "KIND_SEND_DOCUSIGN",
    "DOCUSIGN_OFFER_SECTION",
    "DOCUSIGN_OFFER_BUTTON",
    "DOCUSIGN_OFFER_FALLBACK",
    "TEMPLATE_NOT_LOADED_TEXT",
    "COULDNT_PROCESS_TEXT",
    "TOO_LARGE_TEXT",
]
