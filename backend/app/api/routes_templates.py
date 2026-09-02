"""Template CRUD: list / fetch / upload / delete NDA comparison baselines."""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import BaselineTemplate as Template  # legacy "templates" baseline table
from ..schemas import TemplateOut
from ..storage import safe_upload_path
from .uploads import guard_zip_bomb

router = APIRouter(prefix="/api/templates", tags=["templates"])

_ALLOWED_EXTS = {"docx", "pdf", "txt", "md"}


def _ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lstrip(".").lower()


def _count_clauses(path: Path) -> int:
    """Best-effort clause count via the ingestion layer; 0 on any failure."""
    try:
        from ..ingestion.parser import parse_document  # lazy: leaf module
        from ..ingestion.segmenter import segment_clauses

        parsed = parse_document(str(path))
        return len(segment_clauses(parsed))
    except Exception:  # noqa: BLE001 - clause count is non-critical
        return 0


@router.get("", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)) -> list[Template]:
    """All templates: the default first, then newest-created first."""
    stmt = select(Template).order_by(
        Template.is_default.desc(), Template.created_at.desc()
    )
    return list(db.scalars(stmt).all())


@router.get("/default", response_model=TemplateOut)
def get_default_template(db: Session = Depends(get_db)) -> Template:
    tpl = db.scalar(select(Template).where(Template.is_default.is_(True)))
    if tpl is None:
        raise HTTPException(status_code=404, detail="No default template configured")
    return tpl


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: str, db: Session = Depends(get_db)) -> Template:
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@router.post("", response_model=TemplateOut, status_code=201)
async def upload_template(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Template:
    filename = file.filename or "upload"
    ext = _ext_of(filename)
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: "
            + ", ".join(sorted(_ALLOWED_EXTS)),
        )

    data = await file.read(
        settings.max_upload_bytes + 1
    )  # bounded — don't buffer an oversized body
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {settings.max_upload_mb} MB",
        )
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Same decompression-bomb guard as /v1/reviews: a crafted .docx that inflates past the
    # cap could OOM the process when _count_clauses parses it. No-op for non-zip (pdf/txt/md).
    guard_zip_bomb(data)

    # Server UUID + sanitized suffix only; the client filename is display-only
    # (Template.filename) and never used in the path. See storage.safe_upload_path.
    dest = safe_upload_path(settings.templates_path, filename)
    dest.write_bytes(data)

    tpl = Template(
        name=(name or "").strip() or Path(filename).stem,
        description="",
        filename=filename,
        storage_path=str(dest),
        file_format=ext,
        is_default=False,
        clause_count=_count_clauses(dest),
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.post("/{template_id}/default", response_model=TemplateOut)
def set_default_template(template_id: str, db: Session = Depends(get_db)) -> Template:
    """Make this template the baseline used for new reviews (unsets the others)."""
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    for other in db.scalars(
        select(Template).where(Template.is_default.is_(True))
    ).all():
        other.is_default = False
    tpl.is_default = True
    db.commit()
    db.refresh(tpl)
    return tpl


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: str, db: Session = Depends(get_db)) -> None:
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.is_default:
        raise HTTPException(
            status_code=400, detail="The default template cannot be deleted"
        )

    if tpl.storage_path:
        with contextlib.suppress(OSError):
            Path(tpl.storage_path).unlink(missing_ok=True)

    db.delete(tpl)
    db.commit()
    return None
