"""approval access: user slack id + allowlist role/label + pending doc stash

Dashboard-managed, role-tied approval gate (PLAN §3.4 rework):
* ``user_accounts.slack_user_id`` — bridges a Slack member id to a web account so the account's role
  drives the bot approval gate (unique, nullable).
* ``nda_allowlist.role`` (``admin`` | ``member``, default ``member``) + ``label`` — an admin entry may
  approve others and is exempt; a member is exempt only; ``label`` is the display name for the confirm
  card.
* ``nda_pending_requests.document_blob_id`` (FK ``document_blob`` SET NULL) + ``review_depth`` — the
  stashed submitted document so an approved review auto-runs without re-asking. New allowed ``status``
  value ``awaiting_confirmation`` (no schema change — it's a String column).

Mirrors app.auth.models.UserAccount / app.bot.models.NdaAllowlist / NdaPendingRequest so a fresh
``create_all`` stays schema-equivalent to ``alembic upgrade head`` (tests/test_migrations).

Revision ID: 0010_approval_access
Revises: 0009_tally_dropforms
Create Date: 2026-07-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_approval_access"
down_revision: str | Sequence[str] | None = "0009_tally_dropforms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("user_accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("slack_user_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_user_accounts_slack_user_id", ["slack_user_id"], unique=True
        )

    with op.batch_alter_table("nda_allowlist", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "role",
                sa.String(length=16),
                nullable=False,
                server_default="member",
            )
        )
        batch_op.add_column(sa.Column("label", sa.String(length=255), nullable=True))

    with op.batch_alter_table("nda_pending_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("document_blob_id", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("document_filename", sa.String(length=512), nullable=True)
        )
        batch_op.add_column(
            sa.Column("review_depth", sa.String(length=16), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_nda_pending_requests_document_blob",
            "document_blob",
            ["document_blob_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("nda_pending_requests", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_nda_pending_requests_document_blob", type_="foreignkey"
        )
        batch_op.drop_column("review_depth")
        batch_op.drop_column("document_filename")
        batch_op.drop_column("document_blob_id")

    with op.batch_alter_table("nda_allowlist", schema=None) as batch_op:
        batch_op.drop_column("label")
        batch_op.drop_column("role")

    with op.batch_alter_table("user_accounts", schema=None) as batch_op:
        batch_op.drop_index("ix_user_accounts_slack_user_id")
        batch_op.drop_column("slack_user_id")
