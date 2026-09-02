"""DB-backed service-account key auth + per-key rate / monthly-cost caps (P1-4).

Replaces the env key map (``service_keys.py``) with the ``service_account_keys`` table: each
presented ``X-API-Key`` hashes (sha256) to a row carrying its bound principal, JSON entitlements, a
sliding-window request cap, and a monthly cost cap. Usage (request count + spend) accrues in
``service_account_usage`` per (key, UTC-month) so the cost cap is enforceable. The legacy env
``ENGINE_API_KEY`` is retained as a bootstrap fallback (see ``app.auth.principal``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.auth import rate_store
from app.auth.models import ServiceAccountKey, ServiceAccountUsage
from app.config import settings


#: Default per-key request cap (per minute) when a key sets no ``rate_per_min`` of its own. Falls
#: back to the global ``engine_rate_limit_per_min`` setting (0 disables).
def _default_rate() -> int:
    return int(getattr(settings, "engine_rate_limit_per_min", 0) or 0)


def hash_key(raw: str) -> str:
    """sha256 hex of a raw API key — what ``service_account_keys.key_hash`` stores."""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ServiceKeyAuth:
    key_id: str
    principal_id: str  # <=32, persisted on EngineReview.actor_user_id
    org_id: str
    entitlements: frozenset[str]  # engine action keys this key may perform
    rate_per_min: int | None
    monthly_cost_cap_usd: float | None


def any_active_service_key(db) -> bool:
    """True if the engine is configured via the DB key table (any ACTIVE ``ServiceAccountKey`` exists).

    Used to fail CLOSED on a keyless request in a DB-keys-only deployment (no env keys): the engine IS
    configured, so a missing/unknown key must 401 rather than bind the open svc:local dev principal.
    A DB error here propagates (a broken auth DB must not silently fail open to svc:local)."""
    return bool(
        db.scalar(
            select(func.count())
            .select_from(ServiceAccountKey)
            .where(ServiceAccountKey.active.is_(True))
        )
    )


def resolve_service_key(db, x_api_key: str | None) -> ServiceKeyAuth | None:
    """Resolve an ACTIVE ``service_account_keys`` row from the presented key, or None (no key / no
    match — the caller falls back to the legacy env key or denies). The lookup is by the one-way
    sha256 digest (an indexed equality, not a timing oracle for the raw key); ``compare_digest`` is
    a defensive belt-and-suspenders on the fetched hash."""
    if not x_api_key:
        return None
    kh = hash_key(x_api_key)
    try:
        row = db.execute(
            select(ServiceAccountKey).where(
                ServiceAccountKey.key_hash == kh, ServiceAccountKey.active.is_(True)
            )
        ).scalar_one_or_none()
    except SQLAlchemyError:
        # The service_account_keys table is unavailable (migration 0005 not yet applied, or a DB
        # hiccup): fall back to the legacy env-key path rather than denying — env keys must keep
        # authenticating across the migration window. The caller treats None as 'try legacy'.
        db.rollback()
        return None
    if row is None or not hmac.compare_digest(row.key_hash, kh):
        return None
    try:
        ents = frozenset(json.loads(row.entitlements_json or "[]"))
    except (ValueError, TypeError):
        ents = frozenset()
    return ServiceKeyAuth(
        key_id=row.id,
        principal_id=row.principal_id,
        org_id=row.org_id,
        entitlements=ents,
        rate_per_min=row.rate_per_min,
        monthly_cost_cap_usd=row.monthly_cost_cap_usd,
    )


# --------------------------------------------------------------------------- #
# Monthly usage counter (cost cap backing)
# --------------------------------------------------------------------------- #
def _period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def current_month_cost(db, key_id: str, now: datetime | None = None) -> float:
    row = db.execute(
        select(ServiceAccountUsage.cost_usd).where(
            ServiceAccountUsage.key_id == key_id,
            ServiceAccountUsage.period == _period(now),
        )
    ).scalar_one_or_none()
    return float(row or 0.0)


def record_usage(db, key_id: str, cost_usd: float, now: datetime | None = None) -> None:
    """Increment the (key, month) request_count + cost_usd, creating the row on first use. Runs in
    the caller's transaction (committed by the route). Get-or-create + increment — NO savepoint
    (the engine deliberately omits the pysqlite SAVEPOINT recipe; see app/db.py)."""
    period = _period(now)
    row = db.execute(
        select(ServiceAccountUsage).where(
            ServiceAccountUsage.key_id == key_id, ServiceAccountUsage.period == period
        )
    ).scalar_one_or_none()
    if row is None:
        row = ServiceAccountUsage(
            key_id=key_id, period=period, request_count=0, cost_usd=0.0
        )
        db.add(row)
    row.request_count += 1
    row.cost_usd = float(row.cost_usd or 0.0) + float(cost_usd or 0.0)
    row.updated_at = now or datetime.now(UTC)


def over_monthly_cap(db, auth: ServiceKeyAuth, now: datetime | None = None) -> bool:
    """True if this key has reached its monthly cost cap (None cap -> never). SOFT pre-flight read:
    a concurrent burst can overshoot slightly (the atomic per-key counter is a 'Later' item)."""
    cap = auth.monthly_cost_cap_usd
    if cap is None or cap <= 0:
        return False
    return current_month_cost(db, auth.key_id, now) >= cap


# --------------------------------------------------------------------------- #
# Per-key request rate cap (sliding window via the shared rate store — Redis when
# configured for multi-replica correctness, else in-process; PL-6)
# --------------------------------------------------------------------------- #
def enforce_rate(auth: ServiceKeyAuth) -> None:
    """Raise 429 (via EngineError) if this key is over its per-key request rate. The key's own
    ``rate_per_min`` wins; else the global default. 0 disables."""
    limit = (
        auth.rate_per_min
        if (auth.rate_per_min is not None and auth.rate_per_min > 0)
        else _default_rate()
    )
    rate_store.enforce(auth.principal_id, int(limit or 0))


def reset_rate() -> None:
    """Test hook — clear the shared rate store + its selection cache."""
    rate_store.reset()
