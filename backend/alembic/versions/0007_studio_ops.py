"""studio: per-draft-version operations trail (undo/redo)

Creates the ``studio_ops`` table (PLAN §3.6 undo/redo requirement, §3.7 template studio) — the
server-side operations log behind the highlight→click tokenizer: one row per logged tokenize
operation on a draft ``template_version``, ``seq`` monotonic per version, ``op_json`` carrying the
full reversible op record, ``undone``/``dead`` implementing standard editor redo-tail semantics.

Hand-authored to match ``app.studio.models.StudioOp`` (registered on ``Base.metadata`` via
``app.models``), so a fresh ``create_all`` stays schema-equivalent to ``alembic upgrade head`` —
the invariant asserted by tests/test_migrations*. ``batch_alter_table`` is used for index creation
so it runs under SQLite's copy-and-rebuild (render_as_batch) as well as Postgres.

NOTE: ``down_revision`` is PINNED to ``0006_token_registry`` (authored concurrently by the token
registry agent, per the agreed migration order). 0006 has since landed, so the chain
0001..0007 resolves and the ``alembic upgrade head``-based tests exercise this migration.

Revision ID: 0007_studio_ops
Revises: 0006_token_registry
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_studio_ops"
down_revision: str | Sequence[str] | None = "0006_token_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    """JSONB on Postgres, JSON on SQLite — mirrors ``app.db.JSON_VARIANT``."""
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "studio_ops",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("template_version_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("op_json", _json(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("undone", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("dead", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_version_id"], ["template_version.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_version_id", "seq", name="uq_studio_ops_version_seq"
        ),
    )
    with op.batch_alter_table("studio_ops", schema=None) as batch_op:
        batch_op.create_index(
            "ix_studio_ops_version_state",
            ["template_version_id", "undone", "dead"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("studio_ops", schema=None) as batch_op:
        batch_op.drop_index("ix_studio_ops_version_state")
    op.drop_table("studio_ops")
