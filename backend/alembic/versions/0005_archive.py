"""archive: cache-folder watcher ledger

Creates the ``nda_cache_processed`` table (PLAN §3.10, reference §3.11) on top of the envelopes
migration ``0004_envelopes``:

  * one row per Drive cache-folder file the watcher has seen,
  * ``file_id`` (Drive's stable file id) is the PRIMARY KEY — the fail-closed dedup for cache processing
    (the ported ``INSERT … ON CONFLICT (file_id) DO NOTHING``, here portable insert-and-catch),
  * ``status`` walks the ported lifecycle
    (``processing`` / ``renamed`` / ``saved_default_name`` / ``duplicate_skipped`` / ``failed``).

Hand-authored to match ``app.archive.models.NdaCacheProcessed`` (registered on ``Base.metadata`` via
``app.models`` — ``from .archive import models``), so a fresh ``create_all`` is schema-equivalent to
``alembic upgrade head`` — the invariant asserted by tests/test_migrations*. ``batch_alter_table`` is used
for index creation so it runs under SQLite's copy-and-rebuild (render_as_batch) as well as Postgres.

Revision ID: 0005_archive
Revises: 0004_envelopes
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_archive"
down_revision: str | Sequence[str] | None = "0004_envelopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "nda_cache_processed",
        sa.Column("file_id", sa.String(length=128), nullable=False),
        sa.Column(
            "file_name", sa.String(length=512), server_default="", nullable=False
        ),
        sa.Column(
            "envelope_folder", sa.String(length=512), server_default="", nullable=False
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="processing",
            nullable=False,
        ),
        sa.Column("renamed_to", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("file_id"),
    )
    with op.batch_alter_table("nda_cache_processed", schema=None) as batch_op:
        batch_op.create_index(
            "ix_nda_cache_processed_status_processed",
            ["status", "processed_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("nda_cache_processed", schema=None) as batch_op:
        batch_op.drop_index("ix_nda_cache_processed_status_processed")
    op.drop_table("nda_cache_processed")
