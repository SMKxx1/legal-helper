"""Legal Helper baseline schema — users, sessions, reviews, llm_calls (plan §5).

One baseline migration for the whole app: ``reviews``/``llm_calls`` are unused until Phase 2/3
land, but declaring them now avoids a second migration later (plan §6 Phase 1).

Revision ID: 0001_legal_helper_baseline
Revises:
Create Date: 2026-09-02

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from app.db import JSON_VARIANT

# revision identifiers, used by Alembic.
revision: str = "0001_legal_helper_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("openrouter_key_enc", sa.Text(), nullable=True),
        sa.Column("openrouter_key_last4", sa.String(8), nullable=True),
        sa.Column("openrouter_key_label", sa.String(255), nullable=True),
        sa.Column("preferred_model_quick", sa.String(128), nullable=True),
        sa.Column("preferred_model_deep", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_sha256", "sessions", ["token_sha256"], unique=True)

    op.create_table(
        "reviews",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("doc_sha256", sa.String(64), nullable=True),
        sa.Column("doc_type", sa.String(64), nullable=True),
        sa.Column("our_side", sa.String(255), nullable=True),
        sa.Column("mode", sa.String(8), nullable=False, server_default="quick"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("risk_tier", sa.String(8), nullable=True),
        sa.Column("adherence_score", sa.Float(), nullable=True),
        sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("doc_object_key", sa.String(512), nullable=True),
        sa.Column("doc_bytes", sa.Integer(), nullable=True),
        sa.Column("result_json", JSON_VARIANT, nullable=True),
        sa.Column("error", sa.String(64), nullable=True),
    )
    op.create_index("ix_reviews_user_created", "reviews", ["user_id", "created_at"])

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("review_id", sa.String(32), sa.ForeignKey("reviews.id"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent", sa.String(32), nullable=False, server_default=""),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_calls_review_id", "llm_calls", ["review_id"])
    op.create_index("ix_llm_calls_user_created", "llm_calls", ["user_id", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("llm_calls")
    op.drop_table("reviews")
    op.drop_table("sessions")
    op.drop_table("users")
