"""Bot-core persistence primitives (PLAN §3.3–§3.5) — the durable substrate the in-process bot runs on.

Four tables, each a GATE or a state store the retired n8n stubs never had (reference §9 "Gaps": the
old allowlist always allowed, dedup was fail-open, correlation was pipe-packed into a URL):

* ``bot_inbox``           — fail-closed dedup + durability. The UNIQUE insert on ``event_key`` IS the
  dedup (PLAN §3.3): a duplicate event can't be inserted, so it can't be reprocessed — closing the
  old ``dedupSeen onError=continueRegularOutput`` hole where an engine outage let duplicates through.
  The row also durably records intake so a crash mid-processing is recoverable (status + attempts).
* ``nda_allowlist``       — the REAL allowlist (PLAN §3.4), replacing the always-allow stub. Verified
  principals (Slack user id / DMARC-aligned email) that may run gated intents (review, envelope).
* ``nda_pending_requests``— the pending-approval flow (PLAN §3.4): an allowlist miss persists here,
  the admin is notified, and an approve/deny decision resumes or rejects the request.
* ``bot_correlation``     — confirmation/form state (PLAN §3.6, §3.9) keyed by a short token, with an
  expiry the worker sweeps. Replaces the n8n button-``value``-JSON and STUB Tally correlation query.

Conventions match the ported data layer (``app.models``/``app.models_v2``): 32-char hex-UUID PKs
(cross-dialect), ``JSON_VARIANT`` for semi-structured payloads (JSONB on Postgres, JSON on SQLite),
UTC-aware timestamps with a ``server_default`` so a raw INSERT still stamps a time. Registered on
``Base.metadata`` by an import in ``app.models`` (so ``create_all`` + Alembic autogenerate see them);
created by the Alembic migration ``0002_bot_core``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import JSON_VARIANT, Base


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class BotInbox(Base):
    """Inbound-event dedup + durable intake record (PLAN §3.3).

    The dedup is the UNIQUE constraint on ``event_key``: intake inserts a row per event and lets a
    duplicate ``IntegrityError`` mean "already seen" (the portable insert-and-catch pattern, same as
    ``NdaIdempotencyKey``). Because the insert is transactional, dedup FAILS CLOSED — there is no
    "assume-new on error" path. ``status`` + ``attempts`` + ``error`` make processing durable and
    crash-recoverable: the worker sweep (``BOT_INBOX_SWEEP_SECONDS``) re-drives rows stuck in
    ``processing`` past a timeout and retries ``failed`` ones.
    """

    __tablename__ = "bot_inbox"
    __table_args__ = (
        # The sweep/claimer scan: rows in a given status, oldest first.
        Index("ix_bot_inbox_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    #: The dedup key ('slack:'+event_id / 'email:'+message-id). UNIQUE — the fail-closed dedup.
    event_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # slack | email
    #: The normalized Envelope (model_dump) — persisted so processing survives a restart.
    payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )  # pending | processing | done | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        onupdate=_now,
    )


class NdaAllowlist(Base):
    """A verified principal permitted to run gated intents (PLAN §3.4) — the real allowlist.

    Keyed by ``(principal_type, principal_key)``: ``principal_type`` is the identity plane
    (``slack`` user id, or ``email`` address — only ever added for a DMARC-aligned identity), and the
    pair is UNIQUE so membership checks are exact and re-adding is idempotent. Cutover seeds the
    current active users here so day-one behavior is unchanged (PLAN §3.4).
    """

    __tablename__ = "nda_allowlist"
    __table_args__ = (
        UniqueConstraint(
            "principal_type", "principal_key", name="uq_nda_allowlist_principal"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    principal_type: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # slack | email
    principal_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # role gates BOTH exemption (any row is exempt from the approval gate) and approval power: an
    # ``admin`` entry may also approve/deny others' requests; a ``member`` entry is exempt only.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="member", server_default="member"
    )  # admin | member
    # Optional display name shown in the "request approval from <names>" confirm card.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    added_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
    )


class NdaPendingRequest(Base):
    """A pending-approval request raised when an unverified/absent principal hits a gated intent.

    Flow (PLAN §3.4): allowlist miss -> persist here (``status='pending'``) + notify admin -> admin
    approves/denies -> the request auto-resumes or is rejected. ``request_key`` is the idempotent
    handle (the n8n ``'req_'||md5(sender||intent)`` shape) and is UNIQUE, so a user re-asking collapses
    onto the same open request instead of spamming the admin channel.
    """

    __tablename__ = "nda_pending_requests"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_nda_pending_request_key"),
        # Admin queue: open requests first, oldest first.
        Index("ix_nda_pending_requests_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    requester: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # slack | email
    intent: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # review | envelope | ...
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )  # awaiting_confirmation | pending | approved | denied
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # The stashed document (content-addressed DocumentBlob) + review depth, so an approved review can
    # auto-run WITHOUT re-asking the requester for the file. Nullable: envelope requests carry no doc.
    document_blob_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("document_blob.id", ondelete="SET NULL"), nullable=True
    )
    # The stashed document's original filename — its suffix picks the parser when the approved review
    # auto-runs (``_extract_text`` defaults to .txt without it, which would mis-parse a .docx/.pdf).
    document_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    review_depth: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        index=True,
    )
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Thread-context (P2 open item, added in 0003_forms) so an approval notification threads back into
    # the ORIGIN conversation instead of a fresh message. Purely additive + nullable: a Slack-origin
    # request carries slack_channel/slack_thread_ts; an email-origin one carries email_message_id (for
    # In-Reply-To/References threading) + email_subject (the "Re:" subject).
    slack_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slack_thread_ts: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    email_subject: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class BotCorrelation(Base):
    """Short-lived confirmation / form state keyed by an opaque token (PLAN §3.6, §3.9).

    Replaces the n8n Slack-button ``value`` JSON and the STUB Tally correlation query with a real
    server-side store: a handler stashes ``{kind, payload}`` under a random ``key``, hands the key to
    the user (button value / form-link id), and resolves it on the callback. ``expires_at`` bounds the
    lifetime; the worker sweep reaps expired rows. ``key`` is UNIQUE (an id PK keeps the row shape
    consistent with the rest of the schema; the token is the natural lookup key).
    """

    __tablename__ = "bot_correlation"
    __table_args__ = (
        UniqueConstraint("key", name="uq_bot_correlation_key"),
        # The reaper scans by expiry.
        Index("ix_bot_correlation_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # confirmation | form | ...
    payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
    )
