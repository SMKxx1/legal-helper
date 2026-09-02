"""Identity & access-control ORM models (CLM Phase 0).

Registered on the shared ``app.db.Base`` so they live in the same metadata/migrations as the
engine models. Enum-like columns are plain strings; the canonical enumerations live in
``app.schemas`` (UserRole / UserStatus / ActorType). Every identity row carries ``org_id`` so
multi-tenancy is not a wide change later (single default org for now — schemas.DEFAULT_ORG_ID).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Org(Base):
    """A tenant. One default org today; ``org_id`` is carried everywhere for future multi-tenancy."""

    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UserAccount(Base):
    """A login account. Sign-in is USER_ID + PASSWORD (argon2id hash). ``role`` gates the web app;
    the separate engine-access allow-list (Phase 1) governs review-engine entitlements."""

    __tablename__ = "user_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        ForeignKey("orgs.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )  # the login handle
    name: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # display name (admin UI)
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    # The user's Slack member id (``U…``), so a Slack-origin bot request can resolve to this account
    # and inherit its role for the approval gate. Nullable + UNIQUE (one account per Slack identity).
    slack_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), default=""
    )  # argon2id PHC string
    role: Mapped[str] = mapped_column(
        String(16), default="viewer", index=True
    )  # schemas.UserRole
    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # schemas.UserStatus
    # Free-form team label for org grouping / spend reporting; NULL = unassigned.
    team: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Granular grants layered ON TOP of role. admin/reviewer get the broad ones implicitly; these let
    # an org promote a specific viewer. Surfaced on the Principal + /me so the web app can gate, and
    # enforced server-side: can_view_all_spend on the spend endpoints, can_view_all_docs on the
    # contract list/detail. can_manage_permissions is stored + surfaced (admin still owns user CRUD).
    # server_default=false() mirrors the 0012 migration so an `alembic upgrade head` DB is
    # schema-identical to a `create_all` one (test_alembic_head_matches_create_all).
    can_view_all_docs: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    can_view_all_spend: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    can_manage_permissions: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-user session generation. Bumped to revoke ALL of a user's sessions at once (password
    # change / admin disable): a session is valid only while its snapshot still equals this.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # admin user_account id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IdentitySession(Base):
    """An opaque server-side session. The cookie carries a random token; only its sha256 is stored.
    ``revocation_epoch`` + ``revoked_at`` give instant invalidation (admin disable / logout)."""

    __tablename__ = "identity_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )  # sha256 hex of the cookie token
    revocation_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuthAuditEvent(Base):
    """Append-only auth/admin audit trail (separate from ReviewEvent, which audits reviews)."""

    __tablename__ = "auth_audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor_principal: Mapped[str] = mapped_column(
        String(128), default=""
    )  # user_id / key id / slack id
    actor_type: Mapped[str] = mapped_column(
        String(16), default="user"
    )  # schemas.ActorType
    action: Mapped[str] = mapped_column(
        String(64), index=True
    )  # login, login_failed, user_create, ...
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class PasswordResetToken(Base):
    """A single-use, short-TTL (<=1h) password-reset token. Only the sha256 is stored."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ServiceAccountKey(Base):
    """A machine API key bound to a service-account principal, with per-key entitlements + caps.
    Replaces the single shared ENGINE_API_KEY (P1-4): each caller (Word add-in, n8n, a partner) gets
    its own key so the /v1 engine path is attributable and individually rate/cost-capped."""

    __tablename__ = "service_account_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        ForeignKey("orgs.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), default="")
    key_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )  # sha256 hex of the raw key
    # The bound service-principal id (<=32 so it fits EngineReview.actor_user_id String(32)).
    principal_id: Mapped[str] = mapped_column(String(32), index=True)
    entitlements_json: Mapped[str] = mapped_column(
        Text, default="[]"
    )  # JSON string[] of action keys
    rate_per_min: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # sliding-window cap; None -> default
    monthly_cost_cap_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # None -> uncapped
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ServiceAccountUsage(Base):
    """Per-key, per-month usage counter backing the monthly cost cap + a coarse request count (P1-4).
    One row per (key_id, period 'YYYY-MM'); the cap reads/increments cost_usd transactionally."""

    __tablename__ = "service_account_usage"
    __table_args__ = (
        UniqueConstraint(
            "key_id", "period", name="uq_service_account_usage_key_period"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    key_id: Mapped[str] = mapped_column(
        ForeignKey("service_account_keys.id"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7))  # "YYYY-MM" (UTC month bucket)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
