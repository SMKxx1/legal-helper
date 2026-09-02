"""Cache-folder watcher persistence (PLAN §3.10, reference §3.11) — the ``nda_cache_processed`` table.

One table, ``nda_cache_processed`` — the durable dedup + status ledger for the Drive cache-folder
watcher (:mod:`app.archive.watcher`). It replaces the n8n ``nda_cache_processed`` table the
"Cache Folder Watcher" workflow hand-created, and — critically — closes that workflow's named
SQL-injection risk (reference §3.11: several of its queries string-interpolated Drive ids / file names
with hand-rolled regex sanitization). Here every access is parameterized SQLAlchemy.

The Drive ``file_id`` is the PRIMARY KEY, so a completed-envelope PDF is claimed exactly once no matter
how many times the watcher polls (the ported ``INSERT … ON CONFLICT (file_id) DO NOTHING`` dedup,
expressed as the portable insert-and-catch-``IntegrityError`` pattern used across the bot-core tables).
``status`` walks the ported lifecycle:

    processing          -- claimed, mid-classification (the initial insert)
    renamed             -- classified + filed under <yyyyMMdd>_<issuer>_<mNDA|uNDA>_<recipient>.pdf
    saved_default_name  -- classification failed/incomplete: filed under its original name
    duplicate_skipped   -- an identically-named file already exists in the destination folder
    failed              -- a download/upload error; the file is LEFT in the cache for the next pass

Conventions match the bot-core tables (``app.bot.models``): ``JSON_VARIANT`` is unused here (all columns
are scalar), UTC-aware timestamps carry a ``server_default`` so a raw INSERT still stamps a time.
Registered on ``Base.metadata`` via ``app.models`` (``from .archive import models``) so a fresh
``create_all`` is schema-equivalent to ``alembic upgrade head`` (created by migration ``0005_archive``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..db import Base

# ---- Status lifecycle (the ported values, reference §3.11) ------------------------------------- #
STATUS_PROCESSING = "processing"
STATUS_RENAMED = "renamed"
STATUS_SAVED_DEFAULT_NAME = "saved_default_name"
STATUS_DUPLICATE_SKIPPED = "duplicate_skipped"
STATUS_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(UTC)


class NdaCacheProcessed(Base):
    """One row per Drive cache-folder file the watcher has seen (PLAN §3.10, reference §3.11).

    ``file_id`` (Drive's stable file id) is the PK — the dedup key: the watcher's claim inserts a row
    and a second poll over the same file collides on the PK and is skipped (insert-and-catch). Never
    deleted by the watcher (the ported "Never deletes from cache" invariant); a ``failed`` row is
    retried on the next pass by re-attempting the work, not by re-inserting.
    """

    __tablename__ = "nda_cache_processed"
    __table_args__ = (
        # The watcher's status scan (e.g. surface failures) — status then recency.
        Index("ix_nda_cache_processed_status_processed", "status", "processed_at"),
    )

    #: Drive file id — PRIMARY KEY, the fail-closed dedup for cache processing.
    file_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    file_name: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    #: The cache SUBFOLDER the file was found in (``Envelope_<id>`` or ``''`` for the cache root) —
    #: the requester-DM seam derives the envelope id from it (PLAN §3.10, VERIFY-WITH-MAIN-PROJECT).
    envelope_folder: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=STATUS_PROCESSING,
        server_default=STATUS_PROCESSING,
    )
    #: The auto-name the file was filed under (NULL until it is renamed / saved under its default name).
    renamed_to: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: The failure reason on a ``failed`` row (class name + short context; internals stay in the logs).
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=func.now(),
        onupdate=_now,
    )


def claim_cache_file(
    db: Session, *, file_id: str, file_name: str, envelope_folder: str = ""
) -> bool:
    """Atomically claim a cache file for processing, returning ``True`` iff THIS caller won.

    The ported ``INSERT … ON CONFLICT (file_id) DO NOTHING RETURNING`` dedup (reference §3.11), written
    as the portable insert-and-catch-``IntegrityError`` shape (works on SQLite dev/tests and Postgres
    prod without a dialect-specific ``ON CONFLICT``): a pre-check short-circuits the common already-seen
    case; the ``IntegrityError`` catch closes the concurrent-poll race so two watcher ticks never both
    process the same file. A won claim leaves a ``processing`` row for the caller to transition.
    """
    existing = db.execute(
        select(NdaCacheProcessed.file_id).where(NdaCacheProcessed.file_id == file_id)
    ).scalar_one_or_none()
    if existing is not None:
        return False
    db.add(
        NdaCacheProcessed(
            file_id=file_id,
            file_name=file_name or "",
            envelope_folder=envelope_folder or "",
            status=STATUS_PROCESSING,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def mark_cache_status(
    db: Session,
    file_id: str,
    status: str,
    *,
    renamed_to: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Transition a claimed row's ``status`` (+ optional ``renamed_to`` / ``error``), parameterized.

    ``processed_at`` is written explicitly (tz-aware UTC) so a status scan / the ``ON CONFLICT`` recovery
    ordering is consistent across dialects. A no-op if the row is absent (e.g. never claimed)."""
    values: dict[str, object] = {"status": status, "processed_at": now or _now()}
    if renamed_to is not None:
        values["renamed_to"] = renamed_to
    if error is not None:
        values["error"] = error
    db.execute(
        update(NdaCacheProcessed)
        .where(NdaCacheProcessed.file_id == file_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()


__all__ = [
    "NdaCacheProcessed",
    "claim_cache_file",
    "mark_cache_status",
    "STATUS_PROCESSING",
    "STATUS_RENAMED",
    "STATUS_SAVED_DEFAULT_NAME",
    "STATUS_DUPLICATE_SKIPPED",
    "STATUS_FAILED",
]
