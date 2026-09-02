"""Token-registry metadata model (PLAN §3.7 — the user-managed token registry).

``TokenMeta`` is an ADDITIVE 1:1 companion to ``models_v2.Token``: it hangs the user-editable
presentation/derivation attributes (label, help text, data type, party, fallback text) off an existing
``token`` row without touching the ported ``token`` schema. Ported code that reads ``Token`` (the
generator, ``token_template`` query, seed catalog) stays exactly as-is; the registry service
(:mod:`app.registry.tokens`) creates/updates/deletes the two together.

``token_id`` is the PRIMARY KEY *and* a CASCADE FK to ``token.id`` — one meta row per token, and a
token delete (via the service, after its usage check) takes its meta row (and ``token_template`` rows)
with it. Conventions match the ported data layer (``app.models``/``app.models_v2``): 32-char hex-UUID
identifiers, UTC-aware timestamps with a ``server_default`` so a raw INSERT still stamps a time.
Registered on ``Base.metadata`` by an import in ``app.models`` (mirroring the forms/bot pattern); the
table is created by the Alembic migration ``0006_token_registry`` so a fresh ``create_all`` stays
schema-equivalent to ``alembic upgrade head`` (the invariant asserted by the migration tests).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

#: The closed value sets the CHECK constraints (and the service) enforce.
DATA_TYPES: tuple[str, ...] = ("text", "date", "email", "choice")
PARTIES: tuple[str, ...] = ("internal", "counterparty")


def _now() -> datetime:
    return datetime.now(UTC)


class TokenMeta(Base):
    """User-managed metadata for a registry token (PLAN §3.7).

    A 1:1 additive extension of ``token``: ``token_id`` is both the PK and a CASCADE FK, so there is at
    most one meta row per token and a token deletion removes it automatically. ``data_type`` and
    ``party`` are constrained to their closed sets both here (CHECK) and in the service (a friendly
    error). ``fallback_text`` is the mention-fallback value (PLAN §3.6) written when a bound field is
    left empty.
    """

    __tablename__ = "token_registry_meta"
    __table_args__ = (
        CheckConstraint(
            "data_type IN ('text','date','email','choice')",
            name="ck_token_registry_meta_data_type",
        ),
        CheckConstraint(
            "party IN ('internal','counterparty')",
            name="ck_token_registry_meta_party",
        ),
    )

    token_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("token.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    help_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    data_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text", server_default="text"
    )
    party: Mapped[str] = mapped_column(
        String(16), nullable=False, default="internal", server_default="internal"
    )
    fallback_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        onupdate=_now,
    )


__all__ = ["TokenMeta", "DATA_TYPES", "PARTIES"]
