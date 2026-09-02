"""ORM models — the whole Legal Helper schema (plan §5): ``users``, ``sessions``, ``reviews``,
``llm_calls``. One flat module is enough for four tables in a teaching codebase; no ``models/``
package, no per-domain split.

``reviews``/``llm_calls`` are written by Phase 2 (the review pipeline) and Phase 3 (usage stats) —
they are declared here now, together with the one Alembic baseline migration, so the schema never
needs a second migration later (see ``alembic/versions/0001_legal_helper_baseline.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import JSON_VARIANT, Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """A login account. Sign-in is username + password (argon2id hash, ``auth/security.py``).

    Each user's own OpenRouter API key lives here, Fernet-encrypted (``app.crypto``) — there is no
    shared server key (plan §1). ``preferred_model_{quick,deep}`` are optional per-user overrides
    of the env defaults (``MODEL_QUICK``/``MODEL_DEEP``); ``None`` means "use the default".
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2id PHC string
    role: Mapped[str] = mapped_column(String(16), default="user")  # 'user' | 'admin'
    openrouter_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    openrouter_key_last4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    openrouter_key_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preferred_model_quick: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    preferred_model_deep: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Session(Base):
    """An opaque bearer-token session (plan §1: bearer, not cookies — no CSRF surface).

    The client holds the raw token (returned once, in the login JSON body); only its
    ``sha256`` is ever persisted, so a DB leak yields no usable tokens.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Review(Base):
    """One document review (Phase 2 writes these; Phase 3 aggregates them for usage stats).

    ``result_json`` holds the full findings payload the add-in renders (native ``jsonb`` on
    Postgres, plain ``JSON`` on SQLite — see ``app.db.JSON_VARIANT``). ``doc_object_key`` is the
    bucket key for the original ``.docx`` (Phase 4); ``NULL`` means nothing is stored (either the
    bucket is disabled, storage failed fail-soft, or — for seeded demo rows — nothing was ever
    uploaded).
    """

    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255), default="")
    doc_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    our_side: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default="quick")  # 'quick' | 'deep'
    status: Mapped[str] = mapped_column(
        String(16), default="queued"
    )  # 'queued'|'running'|'done'|'failed'
    risk_tier: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )  # 'green'|'yellow'|'red'
    adherence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    doc_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[dict | list | None] = mapped_column(JSON_VARIANT, nullable=True)
    error: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LlmCall(Base):
    """One OpenRouter call, attributed to the review and user that triggered it (Phase 2).

    ``cost_usd`` comes straight from OpenRouter's ``usage.cost`` — never recomputed locally — so
    the monthly-budget check and the usage screens (Phase 3) read one authoritative number.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent: Mapped[str] = mapped_column(
        String(32), default=""
    )  # 'classifier'|'reviewer'|'coverage'
    model: Mapped[str] = mapped_column(String(128), default="")
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
