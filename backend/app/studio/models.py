"""Studio operations-trail persistence (PLAN §3.6 undo/redo, §3.7 template studio).

One table, following the ported data-layer conventions (``app.models_v2`` / ``app.forms.models``):
32-char hex-UUID PK, ``JSON_VARIANT`` for the op payload, UTC-aware timestamp with a
``server_default``. Registered on ``Base.metadata`` by an import in ``app.models`` (the forms/bot
pattern); created by Alembic migration ``0007_studio_ops``.

``studio_ops`` is the server-side undo/redo log for one draft :class:`~app.models_v2.TemplateVersion`:

* ``seq`` is monotonic per version (unique together) — the editor timeline;
* ``op_json`` is the full :class:`app.studio.tokenize_ops.OpRecord` (locator, offsets, exact
  replaced text, token name, prior/new content hashes, and the pre-op paragraph XML snapshot);
* ``undone`` marks an op currently reversed (redo candidates are ``undone AND NOT dead``);
* ``dead`` marks undone ops permanently invalidated because a NEW op truncated the redo tail
  (standard editor semantics). Dead rows are kept — the trail stays a full audit log.
  (``dead`` is the one column beyond the §3.7 shortlist; the truncation semantics require the
  marker, and deleting rows would lose the audit trail.)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import JSON_VARIANT, Base


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class StudioOp(Base):
    """One logged, reversible tokenize operation on a draft template version."""

    __tablename__ = "studio_ops"
    __table_args__ = (
        UniqueConstraint(
            "template_version_id", "seq", name="uq_studio_ops_version_seq"
        ),
        # undo scans (version, undone) DESC-by-seq; redo scans (version, undone, dead) ASC.
        Index("ix_studio_ops_version_state", "template_version_id", "undone", "dead"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    template_version_id: Mapped[str] = mapped_column(
        ForeignKey("template_version.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    op_json: Mapped[dict] = mapped_column(JSON_VARIANT, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    undone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    dead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
