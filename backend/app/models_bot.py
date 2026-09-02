"""Flow-step idempotency table (survivor of the retired n8n "NDA Bot" integration).

The original module carried four tables the n8n NDA-Bot workflow read/wrote DIRECTLY (via the Postgres
node): ``nda_bot_request`` (correlation ↔ pending request), ``nda_bot_event`` (inbound-event dedup),
``nda_bot_envelope`` (DocuSign envelope audit + dedup), and ``nda_idempotency_key`` (flow-step
idempotency). With the n8n doorway retired (the bot is now in-process; the ``/v1/support_task/bot/*``
DAL plane is gone), the first three tables are dropped — their only consumers were the removed bot DAL
endpoints. ``nda_idempotency_key`` survives: the KEPT ``/v1/support_task/generate-nda`` endpoint uses it
for idempotent replay (a retried POST returns the FIRST filled ``.docx`` byte-for-byte).

Conventions:
- ``org_id`` follows the denormalized multi-tenant pattern (PL-7 child tables): a NOT NULL, indexed
  column defaulting to the bootstrap org — a plain column, not a FK.
- ``created_at`` carries a ``server_default`` so a raw INSERT that omits it still stamps a time.

Registered on ``Base.metadata`` by an import in ``app.models`` (so create_all + alembic autogenerate see
it). See the squashed Alembic baseline ``0001_baseline``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .schemas import DEFAULT_ORG_ID


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _org_id() -> Mapped[str]:
    """Denormalized tenant column: NOT NULL, indexed, defaults to the bootstrap org (PL-7 pattern)."""
    return mapped_column(
        String(32), nullable=False, server_default=DEFAULT_ORG_ID, index=True
    )


class NdaIdempotencyKey(Base):
    """Flow-step idempotency for idempotent replay (2026-07 hardening).

    A caller retries HTTP calls on timeout, and a shared service principal spans every flow — so dedup
    is scoped ``(org_id, principal_id, purpose, key)`` and the caller generates a uuid per flow-step.
    Two purposes today:

      * ``generate_nda`` — the filled .docx is stored (``response_body``) so a retried POST returns
        the FIRST result byte-for-byte instead of re-generating against a possibly-updated template.
      * ``review``       — maps the caller's key to the ``review_id`` of the first completed run,
        folded into the /v1/reviews dedup ahead of the content-sha tiers.

    Insert-and-catch-``IntegrityError`` on the unique triple (the portable ON CONFLICT pattern). Rows
    are transient by design: a worker sweep deletes them after ``IDEMPOTENCY_RETENTION_H`` (a retry
    storm is minutes-long, not days-long).
    """

    __tablename__ = "nda_idempotency_key"
    __table_args__ = (
        # org_id is part of the identity: service-key principal ids (svc:<slug>) are not
        # unique across orgs, and one org's key must never collide with (or read) another's.
        UniqueConstraint(
            "org_id",
            "principal_id",
            "purpose",
            "key",
            name="uq_nda_idempotency_principal_purpose_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    org_id: Mapped[str] = _org_id()
    principal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    # purpose="review": the first run's review id (payload re-read from engine_reviews on replay).
    review_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # purpose="generate_nda": the first response's bytes + download filename.
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        index=True,  # the retention sweep scans by age
    )
