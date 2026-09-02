"""DocuSign envelope audit + requester mapping (PLAN §3.9, §3.10).

One table, ``nda_envelopes`` — the durable record of every DocuSign create-and-send ATTEMPT the bot
makes (successful or failed), plus the requester→channel mapping the P4 cache-folder watcher DMs from
(PLAN §3.10). It replaces the retired n8n ``nda_bot_envelope`` DAL row and the sibling "Main_Project"
workflow's ``nda_envelope_requesters`` table with one ordinary, in-process, transactional row.

Two jobs in one row:

* **Idempotency + audit** — ``idempotency_key`` (the ported ``sha1(docx_b64|recipients)[:40]``) is
  UNIQUE, so a redelivered Slack action / retried send collapses onto the FIRST attempt instead of
  firing a duplicate envelope. Failed attempts persist too (``status='failed'``, ``envelope_id`` NULL)
  so the audit trail is honest about what was tried.
* **Requester mapping (P4)** — ``envelope_id`` ↔ (``requested_by``, ``channel``, ``slack_channel``,
  ``slack_thread_ts``, ``email_message_id``) is what lets the watcher DM the human who requested a
  now-completed envelope (PLAN §3.10). ``envelope_id`` is indexed for that lookup.

Conventions match the bot-core tables (``app.bot.models``): 32-char hex-UUID PK, ``JSON_VARIANT`` for
the email lists (JSONB on Postgres, JSON on SQLite), a UTC-aware ``created_at`` with a ``server_default``
so a raw INSERT still stamps a time. Registered on ``Base.metadata`` through ``app.integrations`` (see
the package ``__init__`` — this wave ``app.models`` is owned by the forms agent, so registration is
wired via ``app.bot`` importing ``app.integrations``). Created by the Alembic migration
``0004_envelopes``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..db import JSON_VARIANT, Base


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class NdaEnvelope(Base):
    """A single DocuSign create-and-send attempt (PLAN §3.9) + its requester mapping (PLAN §3.10).

    ``idempotency_key`` is UNIQUE — the fail-closed dedup for outbound sends (a duplicate button
    click / redelivered event derives the SAME key from the SAME document+recipients and cannot be
    inserted twice). ``envelope_id`` is NULL on a failed attempt and set to DocuSign's returned id on
    success; it is indexed because the P4 watcher looks the requester up by envelope id.
    """

    __tablename__ = "nda_envelopes"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_nda_envelopes_idempotency_key"),
        # The P4 watcher's requester lookup: find the attempt for a completed envelope id.
        Index("ix_nda_envelopes_envelope_id", "envelope_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    #: DocuSign's returned envelope id — NULL on a failed attempt, set on a successful "sent".
    envelope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The ported idempotency key ``sha1(docx_b64 + '|' + JSON(recipients))[:40]`` — UNIQUE.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sent | failed
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # slack | email
    #: Requester-mapping context (PLAN §3.10) — where to DM/reply when the envelope completes.
    slack_channel: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    slack_thread_ts: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    email_message_id: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    #: The verified principal (Slack user id / email) that asked for this envelope — the DM target.
    requested_by: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    signer_emails: Mapped[list] = mapped_column(
        JSON_VARIANT, nullable=False, default=list
    )
    cc_emails: Mapped[list] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    #: The three-way routing choice sent to DocuSign: all_at_once | amp_first | cp_first.
    routing: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        index=True,
    )


class TallySubmission(Base):
    """One received Tally webhook submission — the fail-closed dedup ledger for the intake plane.

    Tally retries a webhook on any non-2xx, and may redeliver; ``submission_id`` (Tally's response id)
    is the PK, so :func:`claim_tally_submission` inserts-and-catches to process each real submission
    exactly once (a redelivery converges on the existing row and is a no-op).
    """

    __tablename__ = "tally_submissions"

    submission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    form_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    channel: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", server_default="received"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        index=True,
    )


def claim_tally_submission(
    db: Session, *, submission_id: str, form_id: str = "", channel: str = ""
) -> bool:
    """Claim a submission for processing. Returns True on the FIRST claim, False if already seen.

    Insert-and-catch-``IntegrityError`` (portable across SQLite/Postgres, no dialect ``ON CONFLICT``):
    a redelivered webhook deriving the same ``submission_id`` cannot insert twice, so the caller skips
    re-generation. A blank ``submission_id`` is never deduped (returns True) — better to re-deliver than
    to silently swallow a malformed payload."""
    if not submission_id:
        return True
    existing = db.get(TallySubmission, submission_id)
    if existing is not None:
        return False
    db.add(
        TallySubmission(submission_id=submission_id, form_id=form_id, channel=channel)
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def mark_tally_submission(db: Session, *, submission_id: str, status: str) -> None:
    """Record the processing outcome (``delivered`` / ``failed`` / ``no_delivery`` / …) on the claimed
    row. Best-effort: a missing row (blank id, never claimed) is a silent no-op."""
    if not submission_id:
        return
    row = db.get(TallySubmission, submission_id)
    if row is None:
        return
    row.status = status[:32]
    db.commit()


def save_envelope_attempt(
    db: Session,
    *,
    idempotency_key: str,
    status: str,
    channel: str,
    routing: str,
    requested_by: str = "",
    signer_emails: list[str] | None = None,
    cc_emails: list[str] | None = None,
    envelope_id: str | None = None,
    slack_channel: str = "",
    slack_thread_ts: str = "",
    email_message_id: str = "",
) -> NdaEnvelope:
    """Persist an envelope attempt, IDEMPOTENT on ``idempotency_key`` (first-writer-wins).

    Mirrors ``bot_dal.idempotency_store``'s portable insert-and-catch-``IntegrityError`` shape (works
    on SQLite dev/tests and Postgres prod without a dialect-specific ``ON CONFLICT``): a redelivered
    send that derives the same key returns the EXISTING row unchanged rather than recording a second
    attempt. A pre-check short-circuits the common case; the ``IntegrityError`` catch closes the
    concurrent-insert race so two callers converge on one row.
    """
    existing = db.execute(
        select(NdaEnvelope).where(NdaEnvelope.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = NdaEnvelope(
        idempotency_key=idempotency_key,
        envelope_id=envelope_id,
        status=status,
        channel=channel,
        routing=routing,
        requested_by=requested_by,
        signer_emails=list(signer_emails or []),
        cc_emails=list(cc_emails or []),
        slack_channel=slack_channel,
        slack_thread_ts=slack_thread_ts,
        email_message_id=email_message_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(NdaEnvelope).where(NdaEnvelope.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if winner is None:  # pragma: no cover — a conflict without a row is unreachable
            raise
        return winner
    return row
