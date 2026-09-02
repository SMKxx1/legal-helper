"""tally intake + drop in-house form service

Intake moved from the in-house ``/f`` form service to the external **Tally** form. This migration:

  * creates ``tally_submissions`` — the fail-closed webhook dedup ledger
    (``app.integrations.models.TallySubmission``), and
  * drops the retired form-service tables ``form_submissions`` → ``form_instances`` → ``forms``
    (FK-safe order; created by ``0003_forms``).

Hand-authored to match the ORM models after the form-service removal so a fresh ``create_all`` stays
schema-equivalent to ``alembic upgrade head`` — the invariant asserted by tests/test_migrations
(``TallySubmission`` is now on ``Base.metadata``; the ``Form``/``FormInstance``/``FormSubmission`` models
were deleted). ``batch_alter_table`` is used for index creation so it runs under SQLite's
copy-and-rebuild (render_as_batch) as well as Postgres.

``downgrade`` is fully reversible: it drops ``tally_submissions`` AND recreates the three form tables
exactly as ``0003_forms`` created them, so a full-chain downgrade (upgrade head → downgrade base,
exercised by tests/test_migrations_bot) can then run ``0003``'s own downgrade to drop them again.

Revision ID: 0009_tally_dropforms
Revises: 0008_attribution
Create Date: 2026-07-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_tally_dropforms"
down_revision: str | Sequence[str] | None = "0008_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    """JSONB on Postgres, JSON on SQLite — mirrors ``app.db.JSON_VARIANT``."""
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Create ``tally_submissions``; drop the retired form-service tables."""
    op.create_table(
        "tally_submissions",
        sa.Column("submission_id", sa.String(length=64), nullable=False),
        sa.Column("form_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("channel", sa.String(length=255), server_default="", nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="received", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("submission_id"),
    )
    with op.batch_alter_table("tally_submissions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_tally_submissions_created_at"),
            ["created_at"],
            unique=False,
        )

    # Drop the in-house form service tables (FK-safe: submissions → instances → forms).
    op.drop_table("form_submissions")
    op.drop_table("form_instances")
    op.drop_table("forms")


def downgrade() -> None:
    """Drop ``tally_submissions``; recreate the three form tables (verbatim from ``0003_forms``)."""
    with op.batch_alter_table("tally_submissions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tally_submissions_created_at"))
    op.drop_table("tally_submissions")

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
