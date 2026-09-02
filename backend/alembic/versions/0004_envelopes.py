"""envelopes: DocuSign create-and-send audit + requester mapping

Creates the ``nda_envelopes`` table (PLAN §3.9, §3.10) on top of the forms migration ``0003_forms``:

  * one row per DocuSign create-and-send ATTEMPT (successful or failed),
  * ``idempotency_key`` UNIQUE — the fail-closed dedup for outbound sends,
  * the ``envelope_id`` ↔ requester mapping the P4 cache-folder watcher DMs from.

Hand-authored to match ``app.integrations.models.NdaEnvelope`` (registered on ``Base.metadata`` via
``app.integrations``, which ``app.bot`` imports), so a fresh ``create_all`` is schema-equivalent to
``alembic upgrade head`` — the invariant asserted by tests/test_migrations*. ``batch_alter_table`` is
used for index creation so it runs under SQLite's copy-and-rebuild (render_as_batch) as well as
Postgres.

NOTE (parallel authoring): ``down_revision`` is PINNED to ``0003_forms`` (the forms agent's revision).
Until that migration lands on disk, ``alembic upgrade head`` cannot resolve the chain — the
create_all==head parity + head-upgrade migration tests are EXPECTED to fail meanwhile; they go green
once both migrations are present. Do not repoint this to 0002.

Revision ID: 0004_envelopes
Revises: 0003_forms
Create Date: 2026-07-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_envelopes"
down_revision: str | Sequence[str] | None = "0003_forms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "nda_envelopes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("envelope_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column(
            "slack_channel", sa.String(length=255), server_default="", nullable=False
        ),
        sa.Column(
            "slack_thread_ts", sa.String(length=64), server_default="", nullable=False
        ),
        sa.Column(
            "email_message_id",
            sa.String(length=512),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "requested_by", sa.String(length=255), server_default="", nullable=False
        ),
        sa.Column(
            "signer_emails",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "cc_emails",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("routing", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_nda_envelopes_idempotency_key"),
    )
    with op.batch_alter_table("nda_envelopes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_nda_envelopes_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            "ix_nda_envelopes_envelope_id", ["envelope_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("nda_envelopes", schema=None) as batch_op:
        batch_op.drop_index("ix_nda_envelopes_envelope_id")
        batch_op.drop_index(batch_op.f("ix_nda_envelopes_created_at"))
    op.drop_table("nda_envelopes")
