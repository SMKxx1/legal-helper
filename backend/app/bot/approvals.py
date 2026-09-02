"""The real allowlist / approvals gate (PLAN §3.4, reference §3.5 checkAllowlist/saveRequest/Notify Admin).

Replaces the n8n ``checkAllowlist`` STUB that ALWAYS allowed (reference §6/§9 "Gaps": "allowlist ALWAYS
allows — gate effectively open"). This is the fail-CLOSED gate the ``review`` and ``envelope`` intents
run behind:

* :func:`gate_check` — the decision. Membership is checked on the envelope's **VERIFIED** identity only:
  a Slack ``sender_id`` always counts (Slack events are v0-HMAC signature-verified); an email
  ``sender_address`` counts ONLY when ``envelope.verified_sender`` (DMARC-aligned) — an un-aligned email
  can never match the allowlist, exactly the §3.3/§6 "unauthenticated mail is read-only-helpful" rule.
  An identity is ALLOWED when it is a ``nda_allowlist`` row for its plane OR it is an admin
  (``NDA_ADMIN_EMAIL`` is the one env-carried admin identity — for email; ``NDA_ADMIN_SLACK_CHANNEL`` is a
  *channel*, not an identity, so Slack admins are ordinary allowlist rows — no env bypass is invented).
  On a MISS the gate persists an idempotent :class:`~app.bot.models.NdaPendingRequest`, notifies the admin
  (Slack Block Kit with Approve/Deny buttons → ``NDA_ADMIN_SLACK_CHANNEL``, else a plain-email fallback →
  ``NDA_ADMIN_EMAIL``) and returns ``pending`` — the ported "Pending Approval" UX, now actually functional.

* :func:`approve_request` / :func:`deny_request` — the idempotent admin transitions
  (``pending → approved | denied``). **Approve also adds the principal to ``nda_allowlist``**; the user
  then retries (auto-resuming the original request is a deliberate later enhancement, not this wave).

* :class:`AllowlistGate` — the :class:`~app.bot.router.ApprovalGate` the router's pipeline calls. It opens
  a session, delegates to :func:`gate_check`, and **fails CLOSED** on any DB/session error — a gate that
  cannot verify membership refuses (``pending``), it never allows.

* :class:`AdminNotifier` — the admin-notification surface, wired from the SAME delivery the pipeline
  replies through (``ReplyService`` + the Slack sink's ``post_blocks``), so no new channel machinery.

Cross-agent contract (P2 wave B): ``gate_check(session, envelope, intent) -> GateDecision`` and
``approve_request(session, request_key, approver_id) -> bool`` /
``deny_request(session, request_key, approver_id) -> bool`` are the stable names agents 2/3 call.
Interactivity button values are the versioned JSON ``{"v": 1, "kind": "approval", "request_key", "action"}``
(PLAN §3.3; agent 3 owns parsing) — built here by :func:`approval_button_value`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..config import Settings, get_settings
from ..telemetry import get_logger
from .envelope import Envelope
from .models import NdaAllowlist, NdaPendingRequest
from .router import GATED_INTENTS, GateDecision

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

log = get_logger("nda.bot.approvals")

#: The Slack button ``value`` shape agent 3 parses (PLAN §3.3 — typed, versioned interactivity payloads).
APPROVAL_VALUE_VERSION = 1
APPROVAL_KIND = "approval"
#: Preserved action_ids for the admin Approve/Deny buttons (the interactivity handler matches on the
#: button ``value`` ``kind``, but stable action_ids keep the card self-describing).
ACTION_APPROVAL_APPROVE = "approval_approve"
ACTION_APPROVAL_DENY = "approval_deny"

#: The requester's *Request approval* confirm button (PLAN §3.4): only on this click is the admin pinged.
APPROVAL_REQUEST_KIND = "request_approval"
ACTION_REQUEST_APPROVAL = "request_approval"


def request_approval_button_value(request_key: str) -> str:
    """The typed *Request approval* button value: ``{"v":1,"kind":"request_approval","request_key"}``."""
    return json.dumps(
        {
            "v": APPROVAL_VALUE_VERSION,
            "kind": APPROVAL_REQUEST_KIND,
            "request_key": request_key,
        },
        separators=(",", ":"),
    )


#: A Slack Block Kit poster with the ported ``post_blocks`` signature ``(envelope, blocks, fallback)``.
PostBlocksFn = Callable[[Envelope, "list[dict[str, Any]]", str], Any]


# --------------------------------------------------------------------------- #
# Small time / identity helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(UTC)


def _verified_principal(envelope: Envelope) -> tuple[str, str] | None:
    """The ``(principal_type, principal_key)`` the allowlist is keyed on — VERIFIED identities only.

    Slack: ``sender_id`` always counts (the event was signature-verified upstream). Email: the
    ``sender_address`` counts ONLY when ``verified_sender`` (DMARC-aligned) — an un-aligned sender yields
    ``None`` and can therefore NEVER match the allowlist (§3.3/§6). Email keys are lower-cased so the
    stored allowlist row and the retry check compare consistently.
    """
    if envelope.channel == "slack":
        sid = (envelope.sender_id or "").strip()
        return ("slack", sid) if sid else None
    if envelope.channel == "email" and envelope.verified_sender:
        addr = (envelope.sender_address or "").strip().lower()
        return ("email", addr) if addr else None
    return None


def _requester_identity(envelope: Envelope) -> str:
    """The stable requester string recorded on the pending request (and later added to the allowlist on
    approve). Present even for an UNVERIFIED sender so the admin sees who asked — but an unverified email
    still cannot use the action after approval, because :func:`_verified_principal` gates the retry."""
    if envelope.channel == "slack":
        return (envelope.sender_id or "").strip() or "unknown"
    return (
        envelope.sender_address or envelope.sender_id or ""
    ).strip().lower() or "unknown"


def _request_key(requester: str, intent: str) -> str:
    """The idempotent pending-request handle (the ported ``'req_'||md5(sender||intent)`` shape). Stable
    per (requester, intent) so a re-ask collapses onto the same open row instead of spamming the admin."""
    digest = hashlib.md5(
        f"{requester}|{intent}".encode(), usedforsecurity=False
    ).hexdigest()
    return f"req_{digest}"


# --------------------------------------------------------------------------- #
# Admin notification (Slack Block Kit + Approve/Deny buttons; email fallback)
# --------------------------------------------------------------------------- #
def approval_button_value(request_key: str, action: str) -> str:
    """The versioned, typed button ``value`` JSON (PLAN §3.3): ``{"v":1,"kind":"approval",...}``.

    ``action`` is ``"approve"`` or ``"deny"``. Agent 3's interactivity handler parses this back to a dict
    and dispatches to :func:`approve_request` / :func:`deny_request` on the carried ``request_key``.
    """
    return json.dumps(
        {
            "v": APPROVAL_VALUE_VERSION,
            "kind": APPROVAL_KIND,
            "request_key": request_key,
            "action": action,
        },
        separators=(",", ":"),
    )


def admin_notice_text(requester: str, intent: str, request_key: str) -> str:
    """The ported ``Notify Admin`` message (reference §3.5) — the Slack fallback + the email body."""
    return (
        f"Approval requested: {requester} wants to run {intent}. Request {request_key}."
    )


def admin_notice_blocks(
    requester: str, intent: str, request_key: str
) -> list[dict[str, Any]]:
    """The admin Block Kit card: who/what/which-request + primary Approve / danger Deny buttons carrying
    the versioned ``{v:1, kind:"approval", request_key, action}`` values (PLAN §3.3, reference §3.5)."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Approval requested* — `{requester}` wants to run *{intent}*.\n"
                    f"Request `{request_key}`."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_APPROVAL_APPROVE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "value": approval_button_value(request_key, "approve"),
                },
                {
                    "type": "button",
                    "action_id": ACTION_APPROVAL_DENY,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                    "value": approval_button_value(request_key, "deny"),
                },
            ],
        },
    ]


class _Deliverer(Protocol):
    """The subset of ``ReplyService`` the email fallback uses: deliver a text reply on a channel."""

    def deliver(self, envelope: Envelope, reply: Any) -> Any: ...


@dataclass
class AdminNotifier:
    """Announces an allowlist-miss approval request to the admin, over the pipeline's own delivery.

    ``post_blocks`` is the Slack sink's ``post_blocks`` (wired only when the ``slack`` channel is enabled);
    ``service`` is the channel-aware ``ReplyService`` (for the email fallback). Both optional — with
    NEITHER wired the notifier is a loud no-op (the request is still persisted; the admin can work the
    admin queue). Every send is fail-soft: a notification failure must never turn a pending decision into
    an allowed one.
    """

    service: _Deliverer | None = None
    post_blocks: PostBlocksFn | None = None

    def notify(
        self, *, settings: Settings, requester: str, intent: str, request_key: str
    ) -> bool:
        """Deliver the admin notification. Returns True if a notification was dispatched, else False
        (no admin channel wired / all sends failed). Never raises. The admin channel/email are resolved
        from the dashboard-managed store (with ``settings`` as the env fallback) via
        :func:`app.settings_store.admin_routing`."""
        from ..settings_store import admin_routing

        admin_channel, admin_email = admin_routing(settings_obj=settings)
        fallback = admin_notice_text(requester, intent, request_key)

        # Prefer the Slack Block Kit card (Approve/Deny buttons) when Slack is wired AND a channel is set.
        if self.post_blocks is not None and admin_channel:
            admin_env = Envelope(
                channel="slack",
                event_key=f"approval-notify:{request_key}",
                slack_channel=admin_channel,
            )
            blocks = admin_notice_blocks(requester, intent, request_key)
            try:
                self.post_blocks(admin_env, blocks, fallback)
                log.info(
                    "bot.approvals.notify.slack",
                    request_key=request_key,
                    channel=admin_channel,
                )
                return True
            except Exception as exc:  # noqa: BLE001 — notification is fail-soft; fall through to email
                log.warning(
                    "bot.approvals.notify.slack_failed",
                    request_key=request_key,
                    error=repr(exc),
                )

        # Plain-email fallback when Slack is not wired (or the Slack post failed).
        if self.service is not None and admin_email:
            admin_env = Envelope(
                channel="email",
                event_key=f"approval-notify:{request_key}",
                sender_address=admin_email,
            )
            try:
                from .channels.protocol import Reply

                self.service.deliver(admin_env, Reply(text=fallback))
                log.info(
                    "bot.approvals.notify.email",
                    request_key=request_key,
                    to=admin_email,
                )
                return True
            except Exception as exc:  # noqa: BLE001 — fail-soft
                log.warning(
                    "bot.approvals.notify.email_failed",
                    request_key=request_key,
                    error=repr(exc),
                )

        log.warning(
            "bot.approvals.notify.no_admin_channel",
            request_key=request_key,
            note="no NDA_ADMIN_SLACK_CHANNEL (with Slack wired) and no NDA_ADMIN_EMAIL — request persisted only",
        )
        return False


# --------------------------------------------------------------------------- #
# Persistence primitives (allowlist membership, pending upsert)
# --------------------------------------------------------------------------- #
def resolve_account(
    session: Session, principal_type: str, principal_key: str
) -> Any | None:
    """The web ``UserAccount`` a bot identity maps to, or ``None``. ``slack`` matches
    ``user_accounts.slack_user_id``; ``email`` matches ``user_accounts.email`` (case-insensitive). Only
    an ACTIVE account counts — a disabled/locked account never inherits its role into the bot gate."""
    from ..auth.models import UserAccount

    if not principal_key:
        return None
    if principal_type == "slack":
        stmt = select(UserAccount).where(
            UserAccount.slack_user_id == principal_key,
            UserAccount.status == "active",
        )
    elif principal_type == "email":
        stmt = select(UserAccount).where(
            func.lower(UserAccount.email) == principal_key.lower(),
            UserAccount.status == "active",
        )
    else:
        return None
    return session.execute(stmt).scalars().first()


def _principal_allowed(
    session: Session,
    principal: tuple[str, str],
    settings: Settings,  # noqa: ARG001 — kept for signature stability (admin routing moved to DB)
) -> bool:
    """True iff ``principal`` is exempt from the approval gate (may raise on a DB error — the caller
    fails CLOSED). Exempt iff EITHER:

    * it resolves to an ACTIVE web ``UserAccount`` whose ``role == "admin"`` (role-tied exemption), OR
    * an ``nda_allowlist`` row exists for it (``admin`` OR ``member`` — any row is exempt).
    """
    ptype, pkey = principal
    account = resolve_account(session, ptype, pkey)
    if account is not None and account.role == "admin":
        return True
    found = session.execute(
        select(NdaAllowlist.id)
        .where(NdaAllowlist.principal_type == ptype, NdaAllowlist.principal_key == pkey)
        .limit(1)
    ).first()
    return found is not None


def _upsert_pending(
    session: Session,
    *,
    requester: str,
    channel: str,
    intent: str,
    request_key: str,
    now: datetime,
    origin: dict[str, str | None],
    document_blob_id: str | None,
    document_filename: str | None,
    review_depth: str | None,
) -> tuple[bool, str]:
    """Idempotently record the pending request as ``awaiting_confirmation`` (PLAN §3.4 — the requester
    must confirm before the admin is pinged). Returns ``(created, current_status)``: ``created`` True
    when a NEW row was inserted. A re-ask collapses onto the existing row (the UNIQUE ``request_key`` is
    the guard — never a second admin ping) but HEALS a missing stash (a re-ask that finally carried the
    document backfills ``document_blob_id`` so the approved review can auto-run)."""
    existing = session.execute(
        select(NdaPendingRequest).where(NdaPendingRequest.request_key == request_key)
    ).scalar_one_or_none()
    if existing is not None:
        existing.created_at = now  # refresh recency for the admin queue ordering
        if document_blob_id and not existing.document_blob_id:
            existing.document_blob_id = document_blob_id
            existing.document_filename = document_filename
            existing.review_depth = review_depth
            for col, val in origin.items():
                if val and not getattr(existing, col, None):
                    setattr(existing, col, val)
        session.commit()
        return (False, existing.status)

    row = NdaPendingRequest(
        requester=requester,
        channel=channel,
        intent=intent,
        request_key=request_key,
        status="awaiting_confirmation",
        created_at=now,
        document_blob_id=document_blob_id,
        document_filename=document_filename,
        review_depth=review_depth,
        slack_channel=origin.get("slack_channel"),
        slack_thread_ts=origin.get("slack_thread_ts"),
        email_message_id=origin.get("email_message_id"),
        email_subject=origin.get("email_subject"),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent miss raced us onto the same request_key — collapse onto the existing row.
        session.rollback()
        existing = session.execute(
            select(NdaPendingRequest).where(
                NdaPendingRequest.request_key == request_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.created_at = now
            session.commit()
            return (False, existing.status)
        return (False, "awaiting_confirmation")
    return (True, "awaiting_confirmation")


def _ensure_allowlist(
    session: Session, *, principal_type: str, principal_key: str, added_by: str | None
) -> None:
    """Add ``(principal_type, principal_key)`` to ``nda_allowlist`` if absent (idempotent — the UNIQUE
    constraint makes re-approval a no-op). Flushed on the enclosing transaction's commit."""
    found = session.execute(
        select(NdaAllowlist.id)
        .where(
            NdaAllowlist.principal_type == principal_type,
            NdaAllowlist.principal_key == principal_key,
        )
        .limit(1)
    ).first()
    if found is not None:
        return
    session.add(
        NdaAllowlist(
            principal_type=principal_type,
            principal_key=principal_key,
            role="member",
            added_by=added_by,
        )
    )


def _admin_display_names(session: Session, settings: Settings) -> list[str]:  # noqa: ARG001
    """Display names of the admins who can approve — labels of ``admin``-role allowlist rows + the
    name/email of ACTIVE admin ``user_accounts``. Best-effort (partial/empty on error), deduped."""
    from ..auth.models import UserAccount

    names: list[str] = []
    try:
        for label, key in session.execute(
            select(NdaAllowlist.label, NdaAllowlist.principal_key).where(
                NdaAllowlist.role == "admin"
            )
        ).all():
            names.append((label or key or "").strip())
        for name, email in session.execute(
            select(UserAccount.name, UserAccount.email).where(
                UserAccount.role == "admin", UserAccount.status == "active"
            )
        ).all():
            names.append((name or email or "").strip())
    except Exception as exc:  # noqa: BLE001 — cosmetic; a lookup error just yields fewer names
        log.warning("bot.approvals.admin_names_failed", error=repr(exc))
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _store_blob(session: Session, data: bytes, filename: str) -> str:  # noqa: ARG001
    """Content-address ``data`` into ``document_blob`` (reuse an existing sha256 row); return the id."""
    import uuid

    from app.models_v2 import DocumentBlob

    sha = hashlib.sha256(data).hexdigest()
    existing = session.execute(
        select(DocumentBlob.id).where(DocumentBlob.sha256 == sha).limit(1)
    ).first()
    if existing is not None:
        return str(existing[0])
    blob = DocumentBlob(
        id=uuid.uuid4().hex,
        sha256=sha,
        bytes=data,
        byte_size=len(data),
        mime_type="application/octet-stream",
    )
    session.add(blob)
    session.flush()
    return blob.id


def _stash_review_document(
    session: Session, envelope: Envelope, settings: Settings
) -> tuple[str | None, str | None, str | None]:
    """Fetch the submitted review doc bytes + store them as a ``document_blob``; return
    ``(blob_id, depth, filename)``. Best-effort: ``(None, None, None)`` when there is no attachment or
    the fetch fails (the request is still created; an approve then just allowlists and the user
    re-submits)."""
    try:
        from .intents.review import ReviewIntent, _pick_attachment

        att = _pick_attachment(envelope.attachments)
        if att is None:
            return (None, None, None)
        data = ReviewIntent(settings=settings)._load_bytes(envelope, att)
        if not data:
            return (None, None, None)
        filename = att.filename or "document"
        return (_store_blob(session, data, filename), "quick", filename)
    except Exception as exc:  # noqa: BLE001 — a stash failure never blocks the gate decision
        log.warning(
            "bot.approvals.stash_failed",
            event_key=envelope.event_key,
            error=repr(exc),
        )
        return (None, None, None)


def _origin_columns(envelope: Envelope) -> dict[str, str | None]:
    """The thread-context columns persisted on the pending row so the approved review lands back in the
    ORIGIN conversation (slack DM/channel/thread, or the email reply)."""
    return {
        "slack_channel": envelope.slack_channel or None,
        "slack_thread_ts": envelope.slack_thread_ts or None,
        "email_message_id": envelope.email_message_id or None,
        "email_subject": envelope.email_subject or None,
    }


# --------------------------------------------------------------------------- #
# The gate decision (the cross-agent ``gate_check`` entry)
# --------------------------------------------------------------------------- #
def gate_check(
    session: Session,
    envelope: Envelope,
    intent: str,
    *,
    settings: Settings | None = None,
    notifier: AdminNotifier | None = None,  # noqa: ARG001 — admin ping moved to advance_and_notify
) -> GateDecision:
    """Decide whether ``intent`` may run for ``envelope`` (PLAN §3.4). FAILS CLOSED — any error refuses.

    Allowed iff the envelope's VERIFIED identity (:func:`_verified_principal`) is an admin or a
    ``nda_allowlist`` row. On a miss: persist an idempotent :class:`NdaPendingRequest`, notify the admin
    (on the first creation only — a re-ask does not re-spam), and return ``pending`` with the stable
    ``request_key`` the user's "Pending Approval" reply quotes. A DB read error, or a failure to persist
    the pending row, still returns ``pending`` — the gate NEVER returns ``allowed`` on an error path.
    """
    settings = settings or get_settings()
    ek = envelope.event_key
    principal = _verified_principal(envelope)

    # 1) Membership check — fail CLOSED on any error (a gate that cannot verify must refuse, never allow).
    try:
        if principal is not None and _principal_allowed(session, principal, settings):
            log.info(
                "bot.approvals.allowed",
                event_key=ek,
                intent=intent,
                principal_type=principal[0],
            )
            return GateDecision(status="allowed")
    except Exception as exc:  # noqa: BLE001 — fail closed: an unverifiable membership refuses
        log.error(
            "bot.approvals.check_failed",
            event_key=ek,
            intent=intent,
            error=repr(exc),
            note="allowlist read failed — failing CLOSED (pending, never allowed)",
        )
        return GateDecision(
            status="pending", reason="allowlist check failed; failing closed"
        )

    # 2) Miss -> awaiting_confirmation. Stash the review document (so an approve can auto-run WITHOUT
    #    re-asking) + persist an idempotent row. The admin is NOT pinged here — the requester must first
    #    confirm (Slack button) / the email path auto-advances in the router. FAILS CLOSED throughout.
    requester = _requester_identity(envelope)
    request_key = _request_key(requester, intent)
    document_blob_id: str | None = None
    document_filename: str | None = None
    review_depth: str | None = None
    if intent == "review":
        document_blob_id, review_depth, document_filename = _stash_review_document(
            session, envelope, settings
        )
    try:
        created, status = _upsert_pending(
            session,
            requester=requester,
            channel=envelope.channel,
            intent=intent,
            request_key=request_key,
            now=_now(),
            origin=_origin_columns(envelope),
            document_blob_id=document_blob_id,
            document_filename=document_filename,
            review_depth=review_depth,
        )
    except Exception as exc:  # noqa: BLE001 — even a persist failure fails CLOSED (never allowed)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        log.error(
            "bot.approvals.persist_failed",
            event_key=ek,
            intent=intent,
            request_key=request_key,
            error=repr(exc),
        )
        return GateDecision(
            status="pending",
            request_key=request_key,
            reason="pending persist failed; failing closed",
        )

    log.info(
        "bot.approvals.pending",
        event_key=ek,
        intent=intent,
        request_key=request_key,
        created=created,
        status=status,
        verified=principal is not None,
        has_doc=bool(document_blob_id),
    )
    # A row that is still awaiting the requester's confirmation → needs_confirmation (carry the admin
    # names for the prompt). A row already past that (pending/approved/denied) → the plain pending reply.
    if status == "awaiting_confirmation":
        return GateDecision(
            status="needs_confirmation",
            request_key=request_key,
            reason="not on allowlist; awaiting requester confirmation",
            admin_names=tuple(_admin_display_names(session, settings)),
        )
    return GateDecision(
        status="pending",
        request_key=request_key,
        reason="already awaiting an admin decision",
    )


def advance_and_notify(
    session: Session,
    request_key: str,
    *,
    notifier: AdminNotifier,
    settings: Settings,
    requester_id: str | None = None,
) -> str:
    """Transition ``awaiting_confirmation → pending`` (the requester confirmed / the email path
    auto-advances) and notify the admin ONCE, on the transition. FAIL-CLOSED authorization: when
    ``requester_id`` is given it MUST equal the row's requester (the confirm click is the requester's
    OWN) — a mismatch is refused and NOTHING is notified. Idempotent. Returns one of:

    * ``"notified"`` — transitioned and the admin was pinged;
    * ``"already"``  — already ``pending``/``approved`` (idempotent no-op, no re-ping);
    * ``"forbidden"``— ``requester_id`` did not match the row's requester (refused);
    * ``"missing"``  — no such request;
    * ``"denied"``   — the request is terminally denied (not reopened here).
    """
    row = session.execute(
        select(NdaPendingRequest).where(NdaPendingRequest.request_key == request_key)
    ).scalar_one_or_none()
    if row is None:
        log.warning("bot.approvals.advance.no_request", request_key=request_key)
        return "missing"
    if requester_id is not None and (row.requester or "") != requester_id:
        log.warning(
            "bot.approvals.advance.forbidden",
            request_key=request_key,
            note="clicker is not the original requester — refusing (fail-closed)",
        )
        return "forbidden"
    if row.status == "denied":
        return "denied"
    if row.status != "awaiting_confirmation":
        return "already"  # pending / approved — do not re-ping the admin

    row.status = "pending"
    row.created_at = _now()
    requester, intent = row.requester, row.intent
    session.commit()
    try:
        notifier.notify(
            settings=settings,
            requester=requester,
            intent=intent,
            request_key=request_key,
        )
    except Exception as exc:  # noqa: BLE001 — a notify failure never un-does the pending transition
        log.warning(
            "bot.approvals.advance.notify_error",
            request_key=request_key,
            error=repr(exc),
        )
    log.info("bot.approvals.advanced", request_key=request_key, requester=requester)
    return "notified"


# --------------------------------------------------------------------------- #
# Admin transitions (idempotent) — the cross-agent approve/deny entries
# --------------------------------------------------------------------------- #
def approve_request(
    session: Session,
    request_key: str,
    approver_id: str,
    *,
    service: Any | None = None,
    post_blocks: PostBlocksFn | None = None,
    settings: Settings | None = None,
) -> bool:
    """Approve the pending request ``request_key`` (idempotent). Transitions ``pending → approved`` AND
    adds the requester to ``nda_allowlist`` so the retry passes the gate. Returns True on success (or a
    repeat approve of an already-approved request — idempotent); False if there is no such request or it
    was already ``denied`` (a terminal state is not reopened here).

    When a reply ``service`` is supplied AND this is a ``review`` request that stashed a document (PLAN
    §3.4 D), the review is AUTO-RUN on the stashed bytes and delivered back to the ORIGIN conversation
    (DM/channel/thread or email reply) — on the FIRST approve only. That auto-run is fully fail-soft: any
    failure is logged and never un-does the approval (the requester is on the allowlist and can re-submit).
    """
    row = session.execute(
        select(NdaPendingRequest).where(NdaPendingRequest.request_key == request_key)
    ).scalar_one_or_none()
    if row is None:
        log.warning(
            "bot.approvals.approve.no_request",
            request_key=request_key,
            approver=approver_id,
        )
        return False
    if row.status == "denied":
        log.info(
            "bot.approvals.approve.conflict_denied",
            request_key=request_key,
            approver=approver_id,
        )
        return False

    # Add to the allowlist for BOTH a fresh approve and a repeat approve (idempotent via the pre-check),
    # so a re-approve heals a missing allowlist row without duplicating it.
    _ensure_allowlist(
        session,
        principal_type=row.channel,
        principal_key=row.requester,
        added_by=approver_id,
    )
    first_approve = row.status != "approved"
    if first_approve:
        row.status = "approved"
        row.decided_by = approver_id
        row.decided_at = _now()
    session.commit()
    log.info(
        "bot.approvals.approved",
        request_key=request_key,
        approver=approver_id,
        principal_type=row.channel,
        principal_key=row.requester,
        first_approve=first_approve,
    )
    # D: auto-run the stashed review + deliver to the origin — first approve only, fully fail-soft.
    if first_approve and service is not None:
        _maybe_resume_review(
            session, row, service=service, post_blocks=post_blocks, settings=settings
        )
    return True


def _origin_envelope_from_row(row: NdaPendingRequest) -> Envelope | None:
    """Reconstruct the ORIGIN reply :class:`Envelope` from a pending row's stored context, so an approved
    review lands back where it was asked. Slack: the stored ``slack_channel`` (a DM id or a channel id) +
    ``slack_thread_ts``. Email: the requester address + ``email_message_id`` / ``email_subject``. ``None``
    when the minimum context to reach the origin is missing (logged by the caller)."""
    ek = f"approval-resume:{row.request_key}"
    if row.channel == "slack":
        chan = (row.slack_channel or "").strip() or (row.requester or "").strip()
        if not chan:
            return None
        return Envelope(
            channel="slack",
            event_key=ek,
            slack_channel=chan,
            slack_thread_ts=(row.slack_thread_ts or ""),
            sender_id=(row.requester or ""),
            verified_sender=True,
        )
    if row.channel == "email":
        addr = (row.requester or "").strip()
        if not addr:
            return None
        return Envelope(
            channel="email",
            event_key=ek,
            sender_address=addr,
            email_message_id=(row.email_message_id or ""),
            email_subject=(row.email_subject or ""),
            verified_sender=True,
        )
    return None


def _build_review_intent(settings: Settings) -> Any:
    """Construct the review runner for the auto-resume (a testing seam — tests patch this to inject a
    stub with fake extract/run/serialize/save collaborators, so no engine/network is touched)."""
    from .intents.review import ReviewIntent

    return ReviewIntent(settings=settings)


def _maybe_resume_review(
    session: Session,
    row: NdaPendingRequest,
    *,
    service: Any | None,
    post_blocks: PostBlocksFn | None,
    settings: Settings | None,
) -> None:
    """Auto-run the stashed review + deliver it to the reconstructed origin (PLAN §3.4 D). Fail-soft:
    any error is logged and swallowed so it can NEVER un-do the approval that already committed."""
    if row.intent != "review" or not row.document_blob_id:
        return
    try:
        from app.models_v2 import DocumentBlob

        from .router import _deliver

        settings = settings or get_settings()
        blob = session.get(DocumentBlob, row.document_blob_id)
        if blob is None or not blob.bytes:
            log.warning("bot.approvals.resume.no_blob", request_key=row.request_key)
            return
        origin = _origin_envelope_from_row(row)
        if origin is None:
            log.warning("bot.approvals.resume.no_origin", request_key=row.request_key)
            return
        reply = _build_review_intent(settings).review_bytes(
            filename=row.document_filename or "document",
            data=bytes(blob.bytes),
            origin_envelope=origin,
        )
        _deliver(origin, reply, service, post_blocks)
        log.info(
            "bot.approvals.resume.delivered",
            request_key=row.request_key,
            channel=origin.channel,
        )
    except Exception as exc:  # noqa: BLE001 — auto-run is best-effort; never un-does the approval
        log.warning(
            "bot.approvals.resume.failed",
            request_key=row.request_key,
            error=repr(exc),
        )


def deny_request(session: Session, request_key: str, approver_id: str) -> bool:
    """Deny the pending request ``request_key`` (idempotent). Transitions ``pending → denied``; does NOT
    touch the allowlist. Returns True on success (or a repeat deny); False if there is no such request or
    it was already ``approved`` (a terminal state is not reversed here)."""
    row = session.execute(
        select(NdaPendingRequest).where(NdaPendingRequest.request_key == request_key)
    ).scalar_one_or_none()
    if row is None:
        log.warning(
            "bot.approvals.deny.no_request",
            request_key=request_key,
            approver=approver_id,
        )
        return False
    if row.status == "approved":
        log.info(
            "bot.approvals.deny.conflict_approved",
            request_key=request_key,
            approver=approver_id,
        )
        return False
    if row.status != "denied":
        row.status = "denied"
        row.decided_by = approver_id
        row.decided_at = _now()
    session.commit()
    log.info("bot.approvals.denied", request_key=request_key, approver=approver_id)
    return True


# --------------------------------------------------------------------------- #
# The router-facing gate (the ApprovalGate protocol)
# --------------------------------------------------------------------------- #
def _default_session_factory() -> sessionmaker:
    from app.db import SessionLocal

    return SessionLocal


class AllowlistGate:
    """The production :class:`~app.bot.router.ApprovalGate` (PLAN §3.4) — fail CLOSED.

    Non-gated intents (everything but ``review`` / ``envelope``) are allowed WITHOUT touching the DB
    (template/generate/help/archive stay open per the reference). A gated intent opens a session and
    delegates to :func:`gate_check`; ANY session/DB error refuses (``pending``) — the gate can never
    return ``allowed`` when it could not actually verify membership.

    ``session_factory`` / ``settings`` / ``notifier`` are injectable (tests pass an in-memory factory + a
    capturing notifier — zero network); they default to the process ``SessionLocal``, ``get_settings()``,
    and a notifier wired to whatever delivery the pipeline passed in.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker | None = None,
        settings: Settings | None = None,
        notifier: AdminNotifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._notifier = notifier

    def check(self, envelope: Envelope, classification: Any) -> GateDecision:
        intent = classification.intent
        if intent not in GATED_INTENTS:
            return GateDecision(status="allowed")
        settings = self._settings or get_settings()
        factory = self._session_factory or _default_session_factory()
        try:
            with factory() as session:
                return gate_check(
                    session,
                    envelope,
                    intent,
                    settings=settings,
                    notifier=self._notifier,
                )
        except Exception as exc:  # noqa: BLE001 — fail CLOSED: a session/DB error refuses, never allows
            log.error(
                "bot.approvals.gate_session_failed",
                event_key=envelope.event_key,
                intent=intent,
                error=repr(exc),
                note="gate session failed — failing CLOSED (pending, never allowed)",
            )
            return GateDecision(
                status="pending", reason="gate session error; failing closed"
            )

    def advance(self, request_key: str, *, requester_id: str | None = None) -> str:
        """Transition ``awaiting_confirmation → pending`` + notify the admin, over this gate's own
        session + notifier (the router's email auto-advance calls this). Fail-soft: any session error
        yields ``"error"`` (nothing notified) so the turn never crashes. See :func:`advance_and_notify`
        for the return codes."""
        settings = self._settings or get_settings()
        factory = self._session_factory or _default_session_factory()
        notifier = self._notifier or AdminNotifier()
        try:
            with factory() as session:
                return advance_and_notify(
                    session,
                    request_key,
                    notifier=notifier,
                    settings=settings,
                    requester_id=requester_id,
                )
        except Exception as exc:  # noqa: BLE001 — advancing is fail-soft (the row stays awaiting)
            log.error(
                "bot.approvals.advance_session_failed",
                request_key=request_key,
                error=repr(exc),
            )
            return "error"


__all__ = [
    "APPROVAL_KIND",
    "APPROVAL_VALUE_VERSION",
    "ACTION_APPROVAL_APPROVE",
    "ACTION_APPROVAL_DENY",
    "AdminNotifier",
    "AllowlistGate",
    "admin_notice_blocks",
    "admin_notice_text",
    "approval_button_value",
    "approve_request",
    "deny_request",
    "gate_check",
]
