"""Data-access for the KEPT generate-nda / review idempotency helpers.

This module originally backed the ``/v1/support_task/bot`` endpoints that REPLACED the n8n workflow's
raw Postgres ``DAL: <op>`` nodes (dedup / consume-request / allowlist / save-envelope). Those bot-DAL
endpoints — and the ``NdaBotEvent`` / ``NdaBotRequest`` / ``NdaBotEnvelope`` models they read/wrote —
have been RETIRED along with the n8n doorway, so their access functions (``record_event``,
``create_request``, ``consume_request``, ``check_allowlist``, ``save_envelope``) are gone.

What survives is the flow-step idempotency backed by the single kept ``NdaIdempotencyKey`` model: the
``generate-nda`` and ``review`` endpoints use it for idempotent replay (a retried POST returns the
FIRST result byte-for-byte / maps to the first review id).

Every function is dialect-portable (Postgres in prod, SQLite in dev/tests): the idempotency store uses
a ``try/commit/except IntegrityError`` race handler instead of a dialect-specific ``ON CONFLICT``
clause, so one code path serves both backends.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models_bot import NdaIdempotencyKey

#: How long an idempotency row protects against a duplicate replay before the worker sweeps it.
#: Retry storms (n8n timeouts, Slack redeliveries) are minutes-long; 48h is a generous ceiling
#: that still keeps stored generate-nda payload bytes transient.
IDEMPOTENCY_RETENTION_H = 48


# --------------------------------------------------------------------------- #
# Flow-step idempotency (nda_idempotency_key) — see the model docstring. Scoped
# (principal_id, purpose, key): svc:n8n is ONE shared principal across flows, so
# the caller supplies a per-flow-step uuid key.
# --------------------------------------------------------------------------- #
def idempotency_lookup(
    db: Session, *, principal_id: str, purpose: str, key: str, org_id: str
) -> NdaIdempotencyKey | None:
    """The stored first-result row for this (org, principal, purpose, key), or None. ORG-scoped
    (PL-8) like every stored-result read: service-key principal ids (svc:<slug>) are not unique
    across orgs, so an unscoped lookup could replay one org's stored document to another."""
    return db.execute(
        select(NdaIdempotencyKey).where(
            NdaIdempotencyKey.org_id == org_id,
            NdaIdempotencyKey.principal_id == principal_id,
            NdaIdempotencyKey.purpose == purpose,
            NdaIdempotencyKey.key == key,
        )
    ).scalar_one_or_none()


def idempotency_store(
    db: Session,
    *,
    principal_id: str,
    purpose: str,
    key: str,
    org_id: str,
    review_id: str | None = None,
    response_body: bytes | None = None,
    filename: str | None = None,
) -> NdaIdempotencyKey:
    """Record the FIRST result for this key (portable insert-and-catch-IntegrityError). On a
    concurrent duplicate the first writer wins and ITS row is returned — replays must converge
    on one result, never overwrite it."""
    row = NdaIdempotencyKey(
        principal_id=principal_id,
        purpose=purpose,
        key=key[:128],
        org_id=org_id,
        review_id=review_id,
        response_body=response_body,
        filename=filename,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = idempotency_lookup(
            db,
            principal_id=principal_id,
            purpose=purpose,
            key=key[:128],
            org_id=org_id,
        )
        if winner is None:  # pragma: no cover — conflict without a row is not reachable
            raise
        return winner
    return row


def idempotency_sweep(now: datetime | None = None, session_factory=None) -> int:
    """Delete idempotency rows past retention (worker job; mirrors sweep_expired_nonces).
    Returns the count removed."""
    from app.db import SessionLocal

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=IDEMPOTENCY_RETENTION_H)
    factory = session_factory or SessionLocal
    with factory() as s:
        res = s.execute(
            delete(NdaIdempotencyKey).where(NdaIdempotencyKey.created_at < cutoff)
        )
        s.commit()
        return getattr(res, "rowcount", 0) or 0
