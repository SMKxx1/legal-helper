"""Schema-redesign domain models (3NF/BCNF) — the CANONICAL layer for new work. New, additive tables
introduced by the normalized schema — see docs/schema-redesign/02-DESIGN.md. (The legacy layer is
``app.models``; prefer this module for new tables/models.)

These live alongside the legacy engine/CLM tables in ``app.models`` during the additive migration
phases; the legacy columns/tables are dropped only in the final, cutover-gated migration. Imported
from ``app.models`` so a fresh ``create_all`` registers them (keeps the alembic-head == create_all
invariant). PKs are 32-char hex UUIDs (cross-dialect: SQLite dev/tests + Postgres prod) rather than
native ``uuid`` — the established project convention.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Binary storage — isolated so the rest of the schema never holds raw bytes and an object-store
# move is a localized change (set bytes=NULL, storage_uri=<url>). NOTE: sha256 is stored as 64-char
# HEX (String) rather than the brief's raw ``bytea`` — so it joins directly to the existing hex
# dedup columns (review.doc_sha256, document.norm_sha256). Single documented deviation.
# --------------------------------------------------------------------------- #
class DocumentBlob(Base):
    __tablename__ = "document_blob"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    sha256: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )  # dedup identical files
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    bytes: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )  # null if migrated out
    storage_uri: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )  # set on object-store move
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --------------------------------------------------------------------------- #
# Parties — supertype + 1:1 specialization (entity / individual). Amperesand = is_internal party.
# --------------------------------------------------------------------------- #
class Template(Base):
    __tablename__ = "template"
    __table_args__ = (
        # Exactly the 8 logical templates: one row per dimension combination.
        UniqueConstraint(
            "jurisdiction_code",
            "counterparty_type_code",
            "mutuality_code",
            name="uq_template_dimensions",
        ),
        # Mutuality applies ONLY to Individual; everything else is NotApplicable. Makes the 8 the
        # ONLY valid combinations (US/SG × {Company, ServiceProvider, Individual-Mutual, Individual-Unilateral}).
        CheckConstraint(
            "(counterparty_type_code = 'Individual' AND mutuality_code IN ('Mutual','Unilateral')) "
            "OR (counterparty_type_code <> 'Individual' AND mutuality_code = 'NotApplicable')",
            name="ck_template_mutuality_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        ForeignKey("orgs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    jurisdiction_code: Mapped[str] = mapped_column(
        ForeignKey("ref_jurisdiction.code", ondelete="RESTRICT"), nullable=False
    )
    counterparty_type_code: Mapped[str] = mapped_column(
        ForeignKey("ref_counterparty_type.code", ondelete="RESTRICT"), nullable=False
    )
    mutuality_code: Mapped[str] = mapped_column(
        ForeignKey("ref_mutuality.code", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class TemplateVersion(Base):
    """Empty vs tokenised variant of a logical template, each a revisable blob-backed version."""

    __tablename__ = "template_version"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "variant_code",
            "version_no",
            name="uq_template_version_variant_no",
        ),
        Index(
            "ix_template_version_current", "template_id", "variant_code", "is_current"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(
        ForeignKey("template.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_code: Mapped[str] = mapped_column(
        ForeignKey("ref_template_variant.code", ondelete="RESTRICT"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    blob_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_blob.id", ondelete="RESTRICT"), nullable=True
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Attribution: WHO uploaded/published this version (P6). A studio publish records the admin user id;
    # a Slack template upload records "slack:<user_id>". Nullable — versions predating this column (and
    # any programmatic/seed writes) leave it NULL, surfaced as "—" on the templates list.
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Token(Base):
    __tablename__ = "token"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )  # e.g. amperesand_signer_name
    placeholder: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False
    )  # e.g. {{amperesand_signer_name}}
    description: Mapped[str] = mapped_column(
        Text, default=""
    )  # expected-value description
    scope_code: Mapped[str] = mapped_column(
        ForeignKey("ref_token_scope.code", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TokenTemplate(Base):
    """The token-applies-to-template mapping — the query n8n + Tally run ("fields for template X").
    Materialized from token.scope_code at seed time; regenerated, never hand-edited."""

    __tablename__ = "token_template"

    token_id: Mapped[str] = mapped_column(
        ForeignKey("token.id", ondelete="CASCADE"), primary_key=True
    )
    template_id: Mapped[str] = mapped_column(
        ForeignKey("template.id", ondelete="CASCADE"), primary_key=True
    )
