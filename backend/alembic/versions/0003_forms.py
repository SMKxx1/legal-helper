"""form service: forms, instances, submissions (+ pending-request thread context)

Creates the three P3 form-service tables (PLAN §3.6) on top of the 0002 bot-core chain, and adds the
four nullable thread-context columns to ``nda_pending_requests`` (P2 open item) so an approval
notification threads back into the origin conversation:

  * ``forms``             — reusable form definition (draft/published blocks, version, settings).
  * ``form_instances``    — one live send: correlation linkage + envelope_context + session_generation.
  * ``form_submissions``  — answers keyed by block uuid, with the public_id resume handle.
  * ALTER ``nda_pending_requests`` +slack_channel/+slack_thread_ts/+email_message_id/+email_subject.

Hand-authored to match ``app.forms.models`` + the extended ``app.bot.models.NdaPendingRequest`` (both
registered on ``Base.metadata`` via ``app.models``), so a fresh ``create_all`` stays schema-equivalent
to ``alembic upgrade head`` — the invariant asserted by tests/test_migrations*. ``batch_alter_table``
is used for index creation + the ALTER so the migration runs under SQLite's copy-and-rebuild
(render_as_batch) as well as Postgres.

Revision ID: 0003_forms
Revises: 0002_bot_core
Create Date: 2026-07-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_forms"
down_revision: str | Sequence[str] | None = "0002_bot_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    """JSONB on Postgres, JSON on SQLite — mirrors ``app.db.JSON_VARIANT``."""
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    # ---- forms --------------------------------------------------------------------------------- #
    op.create_table(
        "forms",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("draft_blocks", _json(), nullable=False),
        sa.Column("blocks", _json(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("settings", _json(), nullable=False),
        sa.Column("removed_blocks", _json(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="draft", nullable=False
        ),
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
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("forms", schema=None) as batch_op:
        batch_op.create_index("ix_forms_kind_status", ["kind", "status"], unique=False)

    # ---- form_instances ------------------------------------------------------------------------ #
    op.create_table(
        "form_instances",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("form_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("correlation_key", sa.String(length=128), nullable=True),
        sa.Column("envelope_context", _json(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="open", nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "session_generation", sa.Integer(), server_default="0", nullable=False
        ),
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
        sa.ForeignKeyConstraint(["form_id"], ["forms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("form_instances", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_form_instances_form_id"), ["form_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_form_instances_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            "ix_form_instances_correlation_key", ["correlation_key"], unique=False
        )
        batch_op.create_index(
            "ix_form_instances_status_expires", ["status", "expires_at"], unique=False
        )

    # ---- form_submissions ---------------------------------------------------------------------- #
    op.create_table(
        "form_submissions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("instance_id", sa.String(length=32), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("data", _json(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="partial", nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", _json(), nullable=False),
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
        sa.ForeignKeyConstraint(["instance_id"], ["form_instances.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_form_submissions_public_id"),
    )
    with op.batch_alter_table("form_submissions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_form_submissions_instance", ["instance_id"], unique=False
        )

    # ---- nda_pending_requests: thread-context columns (P2 open item) --------------------------- #
    with op.batch_alter_table("nda_pending_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("slack_channel", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("slack_thread_ts", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("email_message_id", sa.String(length=512), nullable=True)
        )
        batch_op.add_column(
            sa.Column("email_subject", sa.String(length=1024), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("nda_pending_requests", schema=None) as batch_op:
        batch_op.drop_column("email_subject")
        batch_op.drop_column("email_message_id")
        batch_op.drop_column("slack_thread_ts")
        batch_op.drop_column("slack_channel")

    with op.batch_alter_table("form_submissions", schema=None) as batch_op:
        batch_op.drop_index("ix_form_submissions_instance")
    op.drop_table("form_submissions")

    with op.batch_alter_table("form_instances", schema=None) as batch_op:
        batch_op.drop_index("ix_form_instances_status_expires")
        batch_op.drop_index("ix_form_instances_correlation_key")
        batch_op.drop_index(batch_op.f("ix_form_instances_created_at"))
        batch_op.drop_index(batch_op.f("ix_form_instances_form_id"))
    op.drop_table("form_instances")

    with op.batch_alter_table("forms", schema=None) as batch_op:
        batch_op.drop_index("ix_forms_kind_status")
    op.drop_table("forms")
