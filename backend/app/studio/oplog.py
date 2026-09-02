"""The per-draft-version operations log — server-side undo/redo for the template studio.

Every tokenize drop on a draft :class:`~app.models_v2.TemplateVersion` goes through here so that
(PLAN §3.6/§3.7): the operation is **logged** (a ``studio_ops`` row holding the full reversible
:class:`~app.studio.tokenize_ops.OpRecord`), the draft **blob is updated atomically with the log
row** (one transaction: on any refusal or failure neither is written), and the whole session is
**hash-chained** — each op records the content hash before and after, so a concurrent studio
session (or any out-of-band edit) is refused with ``studio_stale_view`` instead of silently
clobbered.

Editor semantics:

- :func:`apply_op` / :func:`apply_batch` — apply new operation(s). Any live redo tail (undone,
  not-dead ops) is truncated first: those rows are marked ``dead`` (kept for audit, permanently
  un-redoable) — standard editor behavior.
- :func:`undo` — reverses the LAST non-undone op by restoring the recorded paragraph snapshot
  (byte-faithful at the XML level, verified to hash back to the op's ``prior_hash``) and marks
  the row ``undone``.
- :func:`redo` — re-applies the oldest undone-but-not-dead op (undo/redo walk the same timeline
  in opposite directions) and clears its flag, verifying the replay reproduces ``new_hash``.

Blob writes are **content-addressed**: ``document_blob`` rows are looked up/deduped by raw-bytes
sha256 (the table's own convention) and the version's ``blob_id`` is re-pointed — a blob row is
never mutated in place, so a published version sharing the same blob can never be corrupted by
draft edits. Note the two hash namespaces: ``document_blob.sha256`` is over raw file bytes
(storage identity), while the op-record ``prior/new`` hashes are the studio's canonical-XML
*content* hashes (edit identity, stable across serializers) — see ``docview.content_hash``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models_v2 import DocumentBlob, TemplateVersion
from app.support_task.generator import DOCX_MIME

from .errors import DraftBlobMissingError, NothingToRedoError, NothingToUndoError
from .findmap import TokenMapping, map_all
from .models import StudioOp
from .tokenize_ops import OpRecord, apply_tokenize, redo_tokenize, undo_tokenize


def _draft(
    db: Session, template_version_id: str
) -> tuple[TemplateVersion, DocumentBlob]:
    """The version + its draft blob (with bytes present), or a typed refusal."""
    version = db.get(TemplateVersion, template_version_id)
    if version is None or not version.blob_id:
        raise DraftBlobMissingError(template_version_id)
    blob = db.get(DocumentBlob, version.blob_id)
    if blob is None or not blob.bytes:
        raise DraftBlobMissingError(template_version_id)
    return version, blob


def _store_blob(db: Session, data: bytes) -> DocumentBlob:
    """Content-addressed blob write: reuse the row with this raw sha256 or create one."""
    sha = hashlib.sha256(data).hexdigest()
    existing = (
        db.execute(select(DocumentBlob).where(DocumentBlob.sha256 == sha))
        .scalars()
        .first()
    )
    if existing is not None:
        if (
            existing.bytes is None
        ):  # row migrated to object storage; re-hydrate for the draft
            existing.bytes = data
        return existing
    blob = DocumentBlob(
        sha256=sha, byte_size=len(data), mime_type=DOCX_MIME, bytes=data
    )
    db.add(blob)
    db.flush()  # allocate blob.id for the version re-point
    return blob


def _next_seq(db: Session, template_version_id: str) -> int:
    current = db.execute(
        select(func.max(StudioOp.seq)).where(
            StudioOp.template_version_id == template_version_id
        )
    ).scalar()
    return int(current or 0) + 1


def _truncate_redo_tail(db: Session, template_version_id: str) -> None:
    """A new op invalidates every undone-but-redoable op — mark them dead (kept for audit)."""
    db.execute(
        update(StudioOp)
        .where(
            StudioOp.template_version_id == template_version_id,
            StudioOp.undone.is_(True),
            StudioOp.dead.is_(False),
        )
        .values(dead=True)
    )


def history(db: Session, template_version_id: str) -> list[StudioOp]:
    """The full operations trail for a version, oldest first (including undone/dead rows)."""
    return list(
        db.execute(
            select(StudioOp)
            .where(StudioOp.template_version_id == template_version_id)
            .order_by(StudioOp.seq.asc())
        ).scalars()
    )


def apply_op(
    db: Session,
    template_version_id: str,
    *,
    locator: str,
    start: int,
    end: int,
    token_name: str,
    view_hash: str,
    created_by: str | None = None,
    end_locator: str | None = None,
) -> StudioOp:
    """Apply one tokenize drop to the draft blob and log it — atomically.

    ``view_hash`` is the ``content_hash`` embedded in the view the user highlighted in; if the
    draft has since changed (another session, another op) the drop is refused with
    ``studio_stale_view`` and nothing is written.
    """
    try:
        version, blob = _draft(db, template_version_id)
        new_bytes, record = apply_tokenize(
            blob.bytes or b"",
            locator,
            start,
            end,
            token_name,
            expected_hash=view_hash,
            end_locator=end_locator,
        )
        _truncate_redo_tail(db, template_version_id)
        op = StudioOp(
            template_version_id=template_version_id,
            seq=_next_seq(db, template_version_id),
            op_json=record.to_dict(),
            created_by=created_by,
        )
        db.add(op)
        version.blob_id = _store_blob(db, new_bytes).id
        db.commit()
    except Exception:
        db.rollback()
        raise
    return op


def apply_batch(
    db: Session,
    template_version_id: str,
    mappings: Sequence[TokenMapping | Mapping[str, Any]],
    *,
    view_hash: str,
    created_by: str | None = None,
) -> list[StudioOp]:
    """Apply a find-and-map batch: each mapping is logged as its OWN op (individually undoable),
    but the batch commits all-or-nothing — a refusal anywhere leaves the draft untouched."""
    try:
        version, blob = _draft(db, template_version_id)
        new_bytes, records = map_all(
            blob.bytes or b"", mappings, expected_hash=view_hash
        )
        if not records:
            return []
        _truncate_redo_tail(db, template_version_id)
        first_seq = _next_seq(db, template_version_id)
        ops = [
            StudioOp(
                template_version_id=template_version_id,
                seq=first_seq + offset,
                op_json=record.to_dict(),
                created_by=created_by,
            )
            for offset, record in enumerate(records)
        ]
        db.add_all(ops)
        version.blob_id = _store_blob(db, new_bytes).id
        db.commit()
    except Exception:
        db.rollback()
        raise
    return ops


def undo(db: Session, template_version_id: str) -> StudioOp:
    """Reverse the last non-undone op (restore its paragraph snapshot) and mark it undone."""
    try:
        version, blob = _draft(db, template_version_id)
        op = (
            db.execute(
                select(StudioOp)
                .where(
                    StudioOp.template_version_id == template_version_id,
                    StudioOp.undone.is_(False),
                )
                .order_by(StudioOp.seq.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if op is None:
            raise NothingToUndoError()
        record = OpRecord.from_dict(op.op_json)
        restored = undo_tokenize(blob.bytes or b"", record)
        op.undone = True
        version.blob_id = _store_blob(db, restored).id
        db.commit()
    except Exception:
        db.rollback()
        raise
    return op


def redo(db: Session, template_version_id: str) -> StudioOp:
    """Re-apply the oldest undone (non-dead) op and clear its undone flag."""
    try:
        version, blob = _draft(db, template_version_id)
        op = (
            db.execute(
                select(StudioOp)
                .where(
                    StudioOp.template_version_id == template_version_id,
                    StudioOp.undone.is_(True),
                    StudioOp.dead.is_(False),
                )
                .order_by(StudioOp.seq.asc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if op is None:
            raise NothingToRedoError()
        record = OpRecord.from_dict(op.op_json)
        reapplied = redo_tokenize(blob.bytes or b"", record)
        op.undone = False
        version.blob_id = _store_blob(db, reapplied).id
        db.commit()
    except Exception:
        db.rollback()
        raise
    return op
