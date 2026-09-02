"""bot core: inbox dedup, allowlist, pending approvals, correlation

Creates the four P2 bot-core tables (PLAN §3.3–§3.6) on top of the squashed 0001 baseline:

  * ``bot_inbox``            — fail-closed dedup (UNIQUE ``event_key``) + durable intake record.
  * ``nda_allowlist``        — the real allowlist (verified principals) — was an always-allow stub.
  * ``nda_pending_requests`` — pending-approval flow for gated-intent misses.
  * ``bot_correlation``      — confirmation/form state keyed by a short token, with a swept expiry.

Hand-authored to match ``app.bot.models`` (registered on ``Base.metadata`` via ``app.models``), so a
fresh ``create_all`` is schema-equivalent to ``alembic upgrade head`` — the invariant asserted by
tests/test_migrations.py + tests/test_migrations_bot.py. ``batch_alter_table`` is used for index
creation so the migration runs under SQLite's copy-and-rebuild (render_as_batch) as well as Postgres.

Revision ID: 0002_bot_core
Revises: 0001_baseline
Create Date: 2026-07-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_bot_core"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bot_inbox",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("bot_inbox", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_bot_inbox_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_bot_inbox_event_key"), ["event_key"], unique=True
        )
        batch_op.create_index(
            "ix_bot_inbox_status_created", ["status", "created_at"], unique=False
        )

    op.create_table(
        "nda_allowlist",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_key", sa.String(length=255), nullable=False),
        sa.Column("added_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_type", "principal_key", name="uq_nda_allowlist_principal"
        ),
    )

    op.create_table(
        "nda_pending_requests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("requester", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_key", name="uq_nda_pending_request_key"),
    )
    with op.batch_alter_table("nda_pending_requests", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_nda_pending_requests_created_at"),
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_nda_pending_requests_status_created",
            ["status", "created_at"],
            unique=False,
        )

    op.create_table(
        "bot_correlation",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_bot_correlation_key"),
    )
    with op.batch_alter_table("bot_correlation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_bot_correlation_expires_at"), ["expires_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_correlation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bot_correlation_expires_at"))
    op.drop_table("bot_correlation")

    with op.batch_alter_table("nda_pending_requests", schema=None) as batch_op:
        batch_op.drop_index("ix_nda_pending_requests_status_created")
        batch_op.drop_index(batch_op.f("ix_nda_pending_requests_created_at"))
    op.drop_table("nda_pending_requests")

    op.drop_table("nda_allowlist")

    with op.batch_alter_table("bot_inbox", schema=None) as batch_op:
        batch_op.drop_index("ix_bot_inbox_status_created")
        batch_op.drop_index(batch_op.f("ix_bot_inbox_event_key"))
        batch_op.drop_index(batch_op.f("ix_bot_inbox_created_at"))
    op.drop_table("bot_inbox")
