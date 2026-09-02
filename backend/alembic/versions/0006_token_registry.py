"""token registry: token_registry_meta companion table

Creates the ``token_registry_meta`` table (PLAN §3.7 — the user-managed token registry) on top of the
archive migration ``0005_archive``. It is an ADDITIVE 1:1 companion to the ported ``token`` table:
``token_id`` is the PK and a CASCADE FK to ``token.id``, holding the user-editable
label/help/data_type/party/fallback that hangs off an existing token row. The ported ``token`` schema is
untouched, so code that reads ``Token`` (generator, ``token_template`` query, seed catalog) needs no
change.

Hand-authored to match ``app.registry.models.TokenMeta`` (registered on ``Base.metadata`` via
``app.models`` — ``from .registry import models``), so a fresh ``create_all`` is schema-equivalent to
``alembic upgrade head`` — the invariant asserted by tests/test_migrations*. ``batch_alter_table`` /
inline CHECK constraints run under SQLite's copy-and-rebuild (render_as_batch) as well as Postgres.

Revision ID: 0006_token_registry
Revises: 0005_archive
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_token_registry"
down_revision: str | Sequence[str] | None = "0005_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "token_registry_meta",
        sa.Column("token_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=255), server_default="", nullable=False),
        sa.Column("help_text", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "data_type", sa.String(length=16), server_default="text", nullable=False
        ),
        sa.Column(
            "party", sa.String(length=16), server_default="internal", nullable=False
        ),
        sa.Column("fallback_text", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "data_type IN ('text','date','email','choice')",
            name="ck_token_registry_meta_data_type",
        ),
        sa.CheckConstraint(
            "party IN ('internal','counterparty')",
            name="ck_token_registry_meta_party",
        ),
        sa.ForeignKeyConstraint(["token_id"], ["token.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("token_registry_meta")
