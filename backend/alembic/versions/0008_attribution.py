"""attribution: template_version.created_by

Adds the nullable ``created_by`` column to ``template_version`` (P6 attribution) — WHO uploaded/published
a version: an admin user id from the studio upload path (``routes_studio``), or ``slack:<user_id>`` from
the Slack template publish flow (``bot.intents.template_admin``). Nullable so pre-existing versions (and
programmatic/seed writes) stay NULL, surfaced as an em dash on the templates list page.

Mirrors ``app.models_v2.TemplateVersion.created_by`` so a fresh ``create_all`` stays schema-equivalent to
``alembic upgrade head`` — the invariant asserted by tests/test_migrations*. ``batch_alter_table`` is used
so the ADD/DROP COLUMN runs under SQLite's copy-and-rebuild (render_as_batch) as well as Postgres.

Revision ID: 0008_attribution
Revises: 0007_studio_ops
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_attribution"
down_revision: str | Sequence[str] | None = "0007_studio_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("template_version", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("created_by", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("template_version", schema=None) as batch_op:
        batch_op.drop_column("created_by")
