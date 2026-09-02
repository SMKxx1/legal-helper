"""Seed the bundled default NDA template on first run."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BaselineTemplate as Template  # legacy "templates" baseline table

BUNDLED_TEMPLATE_PATH = Path(__file__).parent / "default_nda_template.md"
DEFAULT_TEMPLATE_NAME = "Standard Mutual NDA (Default)"


def _count_clauses(text: str) -> int:
    """Best-effort clause count. Uses the segmenter if present, else a heuristic."""
    try:
        from ..ingestion.segmenter import segment_clauses  # lazy: leaf module

        return len(segment_clauses(text))
    except Exception:
        return len(re.findall(r"(?m)^\s*\d+\.\s+\S", text))


def seed_default_template(db: Session) -> Template | None:
    """Create the bundled default template if no default exists yet."""
    existing = db.scalar(select(Template).where(Template.is_default.is_(True)))
    if existing is not None:
        return existing
    if not BUNDLED_TEMPLATE_PATH.exists():
        return None

    text = BUNDLED_TEMPLATE_PATH.read_text(encoding="utf-8")
    dest = settings.templates_path / "default_nda_template.md"
    shutil.copyfile(BUNDLED_TEMPLATE_PATH, dest)

    tpl = Template(
        name=DEFAULT_TEMPLATE_NAME,
        description="Bundled standard mutual NDA used as the comparison baseline.",
        filename="default_nda_template.md",
        storage_path=str(dest),
        file_format="md",
        is_default=True,
        clause_count=_count_clauses(text),
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl
