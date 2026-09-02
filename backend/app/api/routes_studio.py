"""Template studio pages + API (PLAN §3.7) — the ``/admin`` surface for tokenising NDA templates.

Wires the wave-A studio/registry engines (``app.studio.*``, ``app.registry.*``) into a server-rendered,
CSP-safe admin UI:

* ``GET  /admin/templates`` — the slot list: the 8 templates × {empty, tokenised} variants, each with
  its current version, full version history, and a .docx upload that lands a new DRAFT version.
* ``POST /admin/templates/{template_id}/{variant}/upload`` — .docx-only (hard-enforced), content-
  addressed into ``document_blob`` + a new draft ``template_version`` (``is_current=False``).
* ``GET  /admin/studio/{version_id}`` — the editor: a faithful, addressable document view
  (``studio.docview``), the registry token palette, the live checklist (``studio.checklist`` vs the
  template scope's required tokens), and the find-and-map suggestions (``studio.findmap``).
* ``POST /admin/studio/{version_id}/tokenize|map|undo|redo`` — the operations log (``studio.oplog``);
  each returns the freshly re-extracted view/checklist/findmap so the page re-renders in place. A
  ``studio_stale_view`` (409) propagates so the page can re-extract + retry once.
* ``GET  /admin/studio/{version_id}/test-drive`` — fills the draft with obvious dummy values per token
  data type (``support_task.fill_docx``) and streams the filled .docx.
* ``POST /admin/studio/{version_id}/publish`` — gated on ``missing_required`` being empty: promotes the
  draft to ``is_current`` (archiving the prior) and emits ``drift.emit_template_published`` with the
  old→new token diff (the awaiting caller of wave A).
* ``POST /admin/templates/versions/{version_id}/rollback`` — re-points ``is_current`` to a prior
  version and emits the same drift event.

All page GETs sit behind :func:`admin_page` (anonymous → login redirect, non-admin → 403); all JSON /
download endpoints behind :func:`admin_api` (standard envelope) + :func:`require_admin_csrf` for writes.
The admin security headers are stamped by ``AdminSecurityHeadersMiddleware`` (mounted in ``app.main``).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.errors import EngineError
from app.api.routes_admin_ui import (
    admin_api,
    admin_page,
    page_context,
    require_admin_csrf,
    templates,
)
from app.api.uploads import guard_zip_bomb
from app.auth.sessions import Principal
from app.config import settings
from app.db import get_db
from app.models_v2 import DocumentBlob, Template, TemplateVersion
from app.registry.docx_scan import scan_docx_tokens
from app.registry.drift import DriftNotifier, emit_template_published
from app.registry.tokens import TokenView, list_tokens
from app.seed_catalog import template_matches_scope
from app.studio import oplog
from app.studio.checklist import analyze, scan_token_names
from app.studio.docview import (
    DocumentView,
    ViewPart,
    ViewSegment,
    extract_view,
    load_document,
)
from app.studio.errors import DraftBlobMissingError
from app.studio.findmap import detect_placeholders
from app.support_task.generator import DOCX_MIME, fill_docx, normalize_codes
from app.telemetry import get_logger

log = get_logger("nda.admin.studio")

router = APIRouter(tags=["studio"])

#: The two template variants (mirrors ``app.seed_catalog._VARIANTS`` / ``ref_template_variant``).
_VARIANTS = ("empty", "tokenised")


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class TokenizeBody(BaseModel):
    locator: str
    start: int
    end: int
    token: str
    view_hash: str
    end_locator: str | None = None


class MappingBody(BaseModel):
    locator: str
    start: int
    end: int
    token_name: str


class MapBody(BaseModel):
    view_hash: str
    mappings: list[MappingBody]


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
def _load_version(db: Session, version_id: str) -> TemplateVersion:
    version = db.get(TemplateVersion, version_id)
    if version is None:
        raise EngineError(404, "not_found", "Template version not found.")
    return version


def _blob_bytes(db: Session, version: TemplateVersion) -> bytes | None:
    """The loaded .docx bytes for a version, or ``None`` when no blob is loaded (draft not uploaded)."""
    blob = db.get(DocumentBlob, version.blob_id) if version.blob_id else None
    data = blob.bytes if blob is not None else None
    return data or None


def _draft_bytes(db: Session, version: TemplateVersion) -> bytes:
    """The loaded .docx bytes, or the typed ``studio_draft_blob_missing`` refusal (never returns None)."""
    data = _blob_bytes(db, version)
    if not data:
        raise DraftBlobMissingError(version.id)
    return data


def _template_of(db: Session, version: TemplateVersion) -> Template | None:
    return db.get(Template, version.template_id)


def _store_blob(db: Session, data: bytes) -> DocumentBlob:
    """Content-addressed blob write (mirrors ``oplog._store_blob``): reuse the row with this raw sha256
    or create one; a blob row is never mutated in place."""
    sha = hashlib.sha256(data).hexdigest()
    existing = (
        db.execute(select(DocumentBlob).where(DocumentBlob.sha256 == sha))
        .scalars()
        .first()
    )
    if existing is not None:
        if existing.bytes is None:  # row migrated to object storage; re-hydrate
            existing.bytes = data
        return existing
    blob = DocumentBlob(
        sha256=sha, byte_size=len(data), mime_type=DOCX_MIME, bytes=data
    )
    db.add(blob)
    db.flush()
    return blob


# --------------------------------------------------------------------------- #
# Registry-derived required/known sets + document-view context
# --------------------------------------------------------------------------- #
def _required_and_known(
    db: Session, template: Template | None, variant: str
) -> tuple[list[str], list[str], list[TokenView]]:
    """The checklist inputs: ``known`` = every registry token name; ``required`` = the tokens whose
    scope matches THIS template's dimensions — but only for the ``tokenised`` variant (the ``empty``
    variant is the pristine document and carries no token requirements)."""
    token_views = list_tokens(db)
    known = [t.name for t in token_views]
    if variant == "tokenised" and template is not None:
        required = [
            t.name
            for t in token_views
            if template_matches_scope(
                t.scope_code,
                template.counterparty_type_code,
                template.mutuality_code,
            )
        ]
    else:
        required = []
    return required, known, token_views


def _friendly_label(name: str) -> str:
    """Title-Cased fallback label for a token the registry does not know (``city_zip`` → ``City Zip``)."""
    return " ".join(p.capitalize() for p in (name or "").split("_") if p) or name


def _part_ctx(part: ViewPart, labels: dict[str, str]) -> dict[str, Any]:
    """One render part. ``plen`` is ALWAYS the part's length in the underlying ``paragraph_text``
    (for a token part: ``len("{{name}}")`` verbatim — NOT the label length): the client recovers
    tokenize offsets by summing ``data-plen``, so this is the offset contract with studio.js."""
    if part.is_token:
        return {
            "token": True,
            "name": part.name,
            "label": labels.get(part.name) or _friendly_label(part.name),
            "plen": len(part.text),
        }
    return {
        "token": False,
        "text": part.text,
        "plen": len(part.text),
        "bold": part.bold,
        "italic": part.italic,
        "underline": part.underline,
    }


def _seg_ctx(seg: ViewSegment, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "locator": seg.locator,
        "kind": seg.kind,
        "heading": seg.heading,
        "align": seg.align,
        "parts": [_part_ctx(p, labels) for p in seg.parts],
    }


_SegItems = list[tuple[list[str], ViewSegment]]


def _doc_blocks(view: DocumentView, labels: dict[str, str]) -> list[dict[str, Any]]:
    """Group the flat, ordered view segments into a render tree — paragraphs in view order and
    tables as real row/cell grids (recursively, from the ``tbl:t:r:c`` locator components) — so
    the template can render ``<table>`` markup. This is a pure regrouping of the SAME segments:
    every paragraph keeps its own locator/parts, and no offset or text ever changes."""
    items: _SegItems = [(s.locator.split("/"), s) for s in view.segments]
    blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(items):  # group consecutive segments per document part (body/hdr/ftr)
        j = i
        while j < len(items) and items[j][0][0] == items[i][0][0]:
            j += 1
        blocks.extend(_blocks_at(items[i:j], 1, labels))
        i = j
    return blocks


def _blocks_at(
    items: _SegItems, depth: int, labels: dict[str, str]
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(items):
        comps, seg = items[i]
        comp = comps[depth]
        if comp.startswith("p:"):
            blocks.append({"type": "p", "seg": _seg_ctx(seg, labels)})
            i += 1
            continue
        # A table: consume every consecutive item under the same tbl:<t> at this depth.
        t_id = comp.split(":")[1]
        j = i
        while j < len(items):
            c = items[j][0][depth]
            if not (c.startswith("tbl:") and c.split(":")[1] == t_id):
                break
            j += 1
        blocks.append({"type": "table", "rows": _table_rows(items[i:j], depth, labels)})
        i = j
    return blocks


def _table_rows(
    items: _SegItems, depth: int, labels: dict[str, str]
) -> list[list[dict[str, Any]]]:
    """Row-major cell grid from consecutive same-table items (the traversal emits each cell whole,
    row-major, and lists a merged cell once — so consecutive grouping by (row, col) is exact)."""
    cells: list[tuple[tuple[str, str], _SegItems]] = []
    for comps, seg in items:
        _tag, _t, r, c = comps[depth].split(":")
        if not cells or cells[-1][0] != (r, c):
            cells.append(((r, c), []))
        cells[-1][1].append((comps, seg))
    rows: list[tuple[str, list[dict[str, Any]]]] = []
    for (r, _c), cell_items in cells:
        if not rows or rows[-1][0] != r:
            rows.append((r, []))
        rows[-1][1].append({"blocks": _blocks_at(cell_items, depth + 1, labels)})
    return [row for _r, row in rows]


def _label_map(token_views: list[TokenView]) -> dict[str, str]:
    """Registry token name → human label (the resolved label the chips render)."""
    return {t.name: (t.label or "") for t in token_views}


def _render_partial(name: str, ctx: dict[str, Any]) -> str:
    """Render a studio partial to an HTML string (same autoescaping Jinja env as the pages)."""
    return templates.env.get_template(name).render(ctx)


def _op_state(db: Session, version: TemplateVersion) -> dict[str, Any]:
    """The JSON payload every op endpoint returns: the freshly re-extracted view + checklist + findmap
    as ready-to-swap HTML, the current content hash, and the undo/redo/publish availability flags."""
    view = extract_view(_draft_bytes(db, version))
    template = _template_of(db, version)
    required, known, token_views = _required_and_known(
        db, template, version.variant_code
    )
    checklist = analyze(view, required, known)
    candidates = detect_placeholders(
        view, [(t.name, t.label or "") for t in token_views]
    )
    hist = oplog.history(db, version.id)
    return {
        "content_hash": view.content_hash,
        "doc_html": _render_partial(
            "studio/_docview.html",
            {"blocks": _doc_blocks(view, _label_map(token_views))},
        ),
        "checklist_html": _render_partial(
            "studio/_checklist.html", {"checklist": checklist}
        ),
        "findmap_html": _render_partial(
            "studio/_findmap.html",
            {
                "candidates": [c.to_dict() for c in candidates],
                "view_hash": view.content_hash,
            },
        ),
        "can_undo": any(not o.undone for o in hist),
        "can_redo": any(o.undone and not o.dead for o in hist),
        "missing_required": checklist["missing_required"],
        "publishable": not checklist["missing_required"],
    }


# --------------------------------------------------------------------------- #
# Templates list
# --------------------------------------------------------------------------- #
def _fmt_dt(dt: Any) -> str:
    if dt is None:
        return ""
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 — display only
        return str(dt)


def _version_view(db: Session, v: TemplateVersion) -> dict[str, Any]:
    blob = db.get(DocumentBlob, v.blob_id) if v.blob_id else None
    data = blob.bytes if blob is not None else None
    return {
        "id": v.id,
        "version_no": v.version_no,
        "is_current": bool(v.is_current),
        "has_blob": bool(data),
        "token_count": len(scan_docx_tokens(data)) if data else 0,
        "created_at": _fmt_dt(v.created_at),
        "created_by": v.created_by
        or "",  # attribution (P6): "—" when unknown, in the template
    }


def _template_view(db: Session, t: Template) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for variant in _VARIANTS:
        versions = (
            db.execute(
                select(TemplateVersion)
                .where(
                    TemplateVersion.template_id == t.id,
                    TemplateVersion.variant_code == variant,
                )
                .order_by(TemplateVersion.version_no.desc())
            )
            .scalars()
            .all()
        )
        vlist = [_version_view(db, v) for v in versions]
        current = next((vv for vv in vlist if vv["is_current"]), None)
        variants.append({"variant": variant, "current": current, "versions": vlist})
    return {
        "id": t.id,
        "name": t.name,
        "jurisdiction_code": t.jurisdiction_code,
        "counterparty_type_code": t.counterparty_type_code,
        "mutuality_code": t.mutuality_code,
        "variants": variants,
    }


@router.get("/admin/templates", response_class=HTMLResponse)
def templates_list(
    request: Request,
    principal: Principal = Depends(admin_page),
    db: Session = Depends(get_db),
) -> Response:
    tmpls = (
        db.execute(
            select(Template).order_by(
                Template.jurisdiction_code,
                Template.counterparty_type_code,
                Template.mutuality_code,
            )
        )
        .scalars()
        .all()
    )
    view_models = [_template_view(db, t) for t in tmpls]
    return templates.TemplateResponse(
        request,
        "studio/templates_list.html",
        page_context(request, principal, active_nav="templates", templates=view_models),
    )


@router.post(
    "/admin/templates/{template_id}/{variant}/upload",
    dependencies=[Depends(require_admin_csrf)],
)
async def upload_version(
    template_id: str,
    variant: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    """Upload a .docx as a new DRAFT version for a (template, variant) slot. Templates are .docx ONLY —
    hard-enforced with a plain-English rejection (PLAN §3.7)."""
    template = db.get(Template, template_id)
    if template is None:
        raise EngineError(404, "not_found", "Template not found.")
    if variant not in _VARIANTS:
        raise EngineError(400, "bad_request", "Variant must be 'empty' or 'tokenised'.")

    filename = file.filename or ""
    if not filename.lower().endswith(".docx"):
        raise EngineError(
            422,
            "not_docx",
            "Templates must be .docx files — that is the only supported format. "
            "Export or save your document as .docx and try again.",
        )
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise EngineError(
            413,
            "request_too_large",
            f"File exceeds the {settings.max_upload_mb} MB limit.",
        )
    version = _create_draft_version(db, template, variant, data, principal.user_id)
    return JSONResponse(
        {
            "ok": True,
            "version_id": version.id,
            "studio_url": f"/admin/studio/{version.id}",
        },
        status_code=201,
    )


def _create_draft_version(
    db: Session, template: Template, variant: str, data: bytes, created_by: str
) -> TemplateVersion:
    """Validate ``data`` as a readable .docx and land it as a new NON-current draft version for
    (``template``, ``variant``). Shared by the single upload and the bulk upload; the caller has already
    checked the variant is valid and enforced the size cap. Raises the same friendly ``EngineError``s
    (empty / zip-bomb / bad .docx) so both paths report identically."""
    if not data:
        raise EngineError(422, "empty_file", "The uploaded file is empty.")
    guard_zip_bomb(data)
    load_document(data)  # raises studio_bad_docx (422) if it is not a readable .docx

    blob = _store_blob(db, data)
    next_no = (
        int(
            db.execute(
                select(func.max(TemplateVersion.version_no)).where(
                    TemplateVersion.template_id == template.id,
                    TemplateVersion.variant_code == variant,
                )
            ).scalar()
            or 0
        )
        + 1
    )
    version = TemplateVersion(
        template_id=template.id,
        variant_code=variant,
        version_no=next_no,
        blob_id=blob.id,
        is_current=False,
        created_by=created_by,  # attribution (P6): who uploaded this draft
    )
    db.add(version)
    db.commit()
    log.info(
        "studio.upload",
        template_id=template.id,
        variant=variant,
        version_no=next_no,
        created_by=created_by,
    )
    return version


# --------------------------------------------------------------------------- #
# Bulk template upload + downloadable token reference
# --------------------------------------------------------------------------- #
#: Optional trailing filename segment selecting the variant (default ``empty``).
_VARIANT_ALIASES = {
    "empty": "empty",
    "tokenised": "tokenised",
    "tokenized": "tokenised",
}
_JURISDICTIONS = {"sg": "SG", "us": "US"}
_COUNTERPARTIES = {
    "company": "Company",
    "serviceprovider": "ServiceProvider",
    "sp": "ServiceProvider",
    "individual": "Individual",
}
_MUTUALITIES = {"mutual": "Mutual", "unilateral": "Unilateral"}


def parse_template_filename(name: str) -> tuple[str, str, str, str]:
    """Parse a bulk-upload filename into ``(jurisdiction, counterparty, mutuality, variant)`` strings.

    Convention (case-insensitive, ``_``-separated, ``.docx``):
    ``<SG|US>_<Company|ServiceProvider|SP|Individual>[_<Mutual|Unilateral>][_<empty|tokenised>].docx``
    — e.g. ``SG_Company.docx``, ``US_Individual_Mutual.docx``, ``SG_ServiceProvider_tokenised.docx``.
    Mutuality is REQUIRED for Individual and forbidden for Company / ServiceProvider.

    STRICT: every segment must be recognized. An unknown jurisdiction / counterparty / mutuality / variant
    (e.g. a typo like ``Unilaterl`` or ``tokenisd``) raises ``ValueError`` rather than silently mis-routing
    the upload to the wrong slot — the bulk route turns that into a clear per-file error. (This is why we
    validate here instead of leaning on ``normalize_codes``, which coerces unknown mutuality to Mutual.)"""
    stem = re.sub(r"\.docx$", "", (name or "").strip(), flags=re.IGNORECASE)
    parts = [p for p in (seg.strip() for seg in stem.split("_")) if p]
    variant = "empty"
    if parts and parts[-1].lower() in _VARIANT_ALIASES:
        variant = _VARIANT_ALIASES[parts.pop().lower()]
    if not 2 <= len(parts) <= 3:
        raise ValueError(
            "name must look like 'SG_Company.docx' or 'US_Individual_Mutual.docx' "
            "(jurisdiction_counterparty[_mutuality][_empty|tokenised].docx)"
        )
    jurisdiction = _JURISDICTIONS.get(parts[0].lower())
    if jurisdiction is None:
        raise ValueError(f"unknown jurisdiction {parts[0]!r} (expected SG or US)")
    counterparty = _COUNTERPARTIES.get(parts[1].lower())
    if counterparty is None:
        raise ValueError(
            f"unknown counterparty type {parts[1]!r} "
            "(expected Company, ServiceProvider, or Individual)"
        )
    mutuality = ""
    if len(parts) == 3:
        if counterparty != "Individual":
            raise ValueError(
                f"{counterparty} templates take no mutuality — drop the {parts[2]!r} segment "
                "(or, for a variant, spell it 'empty' or 'tokenised')"
            )
        resolved = _MUTUALITIES.get(parts[2].lower())
        if resolved is None:
            raise ValueError(
                f"unknown mutuality {parts[2]!r} (expected Mutual or Unilateral)"
            )
        mutuality = resolved
    elif counterparty == "Individual":
        raise ValueError(
            "Individual templates need a mutuality — e.g. 'US_Individual_Mutual.docx'"
        )
    return jurisdiction, counterparty, mutuality, variant


@router.post("/admin/templates/bulk-upload", dependencies=[Depends(require_admin_csrf)])
async def bulk_upload(
    files: list[UploadFile] = File(...),
    variant: str = Form("empty"),
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    """Upload many .docx at once — each filename routed to its template-combo slot as a new draft
    version of the chosen ``variant`` (``empty`` | ``tokenised``, selected once for the whole batch).
    Per-file isolation: one bad file (unparseable name, unknown combo, non-.docx, unreadable, oversize)
    is reported in its ``results`` row and never fails the rest of the batch."""
    if variant not in _VARIANTS:
        raise EngineError(400, "bad_request", "Variant must be 'empty' or 'tokenised'.")
    results: list[dict[str, Any]] = []
    for file in files:
        filename = file.filename or "(unnamed)"
        row: dict[str, Any] = {"filename": filename, "ok": False}
        try:
            if not filename.lower().endswith(".docx"):
                raise ValueError("templates must be .docx files")
            # The batch ``variant`` selector is authoritative; any filename variant suffix is tolerated
            # (so a name like ``US_Company_tokenised.docx`` still parses) but ignored here.
            jur_s, cp_s, mut_s, _ = parse_template_filename(filename)
            jur, cp, mut = normalize_codes(jur_s, cp_s, mut_s)
            template = db.execute(
                select(Template).where(
                    Template.jurisdiction_code == jur,
                    Template.counterparty_type_code == cp,
                    Template.mutuality_code == mut,
                )
            ).scalar_one_or_none()
            if template is None:
                raise ValueError(f"no {jur} / {cp} / {mut} template is seeded")
            data = await file.read(settings.max_upload_bytes + 1)
            if len(data) > settings.max_upload_bytes:
                raise ValueError(f"exceeds the {settings.max_upload_mb} MB limit")
            version = _create_draft_version(
                db, template, variant, data, principal.user_id
            )
            row.update(
                ok=True,
                combo=f"{jur} / {cp} / {mut}",
                variant=variant,
                version_no=version.version_no,
                version_id=version.id,
            )
        except EngineError as exc:
            db.rollback()
            row["error"] = exc.message
        except ValueError as exc:
            db.rollback()
            row["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — one bad file must not fail the batch
            db.rollback()
            log.warning(
                "studio.bulk_upload.file_failed", filename=filename, error=repr(exc)
            )
            row["error"] = "could not process this file"
        results.append(row)
    log.info(
        "studio.bulk_upload",
        count=len(results),
        ok=sum(1 for r in results if r["ok"]),
        by=principal.user_id,
    )
    return JSONResponse({"results": results})


@router.get("/admin/templates/token-reference.pdf")
def token_reference_pdf(principal: Principal = Depends(admin_page)) -> Response:
    """Download the canonical token reference as a branded PDF (content lives in
    :mod:`app.admin.token_reference`; rendered on the fly)."""
    from app.admin.token_reference import PDF_FILENAME, build_token_reference_pdf

    return Response(
        content=build_token_reference_pdf(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{PDF_FILENAME}"'},
    )


# --------------------------------------------------------------------------- #
# Studio editor page
# --------------------------------------------------------------------------- #
@router.get("/admin/studio/{version_id}", response_class=HTMLResponse)
def studio_page(
    version_id: str,
    request: Request,
    principal: Principal = Depends(admin_page),
    db: Session = Depends(get_db),
) -> Response:
    version = _load_version(db, version_id)
    data = _blob_bytes(db, version)
    if not data:
        return templates.TemplateResponse(
            request,
            "studio/needs_upload.html",
            page_context(request, principal, active_nav="templates"),
        )
    view = extract_view(data)
    template = _template_of(db, version)
    required, known, token_views = _required_and_known(
        db, template, version.variant_code
    )
    checklist = analyze(view, required, known)
    candidates = detect_placeholders(
        view, [(t.name, t.label or "") for t in token_views]
    )
    hist = oplog.history(db, version_id)
    ctx = page_context(
        request,
        principal,
        active_nav="templates",
        version_id=version_id,
        view=view,
        blocks=_doc_blocks(view, _label_map(token_views)),
        checklist=checklist,
        candidates=[c.to_dict() for c in candidates],
        view_hash=view.content_hash,
        tokens=token_views,
        can_undo=any(not o.undone for o in hist),
        can_redo=any(o.undone and not o.dead for o in hist),
        publishable=not checklist["missing_required"],
        template_name=(template.name if template else "Template"),
        jurisdiction=(template.jurisdiction_code if template else ""),
        counterparty_type=(template.counterparty_type_code if template else ""),
        mutuality=(template.mutuality_code if template else ""),
        variant=version.variant_code,
        version_no=version.version_no,
        is_current=bool(version.is_current),
    )
    return templates.TemplateResponse(request, "studio/studio.html", ctx)


@router.get("/admin/studio/{version_id}/state")
def studio_state(
    version_id: str,
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    """The current view/checklist/findmap state (used by the page to refresh after a stale-view 409)."""
    version = _load_version(db, version_id)
    return JSONResponse(_op_state(db, version))


# --------------------------------------------------------------------------- #
# Operations (oplog) — each returns the fresh op-state payload
# --------------------------------------------------------------------------- #
@router.post(
    "/admin/studio/{version_id}/tokenize", dependencies=[Depends(require_admin_csrf)]
)
def studio_tokenize(
    version_id: str,
    body: TokenizeBody,
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    """Highlight→click tokenize: replace the ``[start, end)`` span at ``locator`` with ``{{token}}``.
    A stale ``view_hash`` raises ``studio_stale_view`` (409) which propagates into the envelope so the
    page re-extracts and retries once."""
    _load_version(db, version_id)
    oplog.apply_op(
        db,
        version_id,
        locator=body.locator,
        start=body.start,
        end=body.end,
        token_name=body.token,
        view_hash=body.view_hash,
        created_by=principal.user_id,
        end_locator=body.end_locator,
    )
    return JSONResponse(_op_state(db, _load_version(db, version_id)))


@router.post(
    "/admin/studio/{version_id}/map", dependencies=[Depends(require_admin_csrf)]
)
def studio_map(
    version_id: str,
    body: MapBody,
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    """Accept find-and-map suggestions as a batch (each mapping is its own undoable op)."""
    _load_version(db, version_id)
    mappings = [
        {
            "locator": m.locator,
            "start": m.start,
            "end": m.end,
            "token_name": m.token_name,
        }
        for m in body.mappings
    ]
    oplog.apply_batch(
        db,
        version_id,
        mappings,
        view_hash=body.view_hash,
        created_by=principal.user_id,
    )
    return JSONResponse(_op_state(db, _load_version(db, version_id)))


@router.post(
    "/admin/studio/{version_id}/undo", dependencies=[Depends(require_admin_csrf)]
)
def studio_undo(
    version_id: str,
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    _load_version(db, version_id)
    oplog.undo(db, version_id)  # studio_nothing_to_undo (409) propagates if none
    return JSONResponse(_op_state(db, _load_version(db, version_id)))


@router.post(
    "/admin/studio/{version_id}/redo", dependencies=[Depends(require_admin_csrf)]
)
def studio_redo(
    version_id: str,
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    _load_version(db, version_id)
    oplog.redo(db, version_id)  # studio_nothing_to_redo (409) propagates if none
    return JSONResponse(_op_state(db, _load_version(db, version_id)))


# --------------------------------------------------------------------------- #
# Test-drive (fill with dummy values) — downloads a filled .docx
# --------------------------------------------------------------------------- #
def _dummy_value(name: str, view: TokenView | None) -> str:
    dt = (view.data_type if view else "text") or "text"
    label = (view.label if view else "") or name
    if dt == "date":
        return "2026-01-01"
    if dt == "email":
        return "sample@example.com"
    return f"Sample {label}"


@router.get("/admin/studio/{version_id}/test-drive")
def studio_test_drive(
    version_id: str,
    principal: Principal = Depends(admin_page),
    db: Session = Depends(get_db),
) -> Response:
    """Fill the draft with obvious dummy values (per token data type) and stream the filled .docx —
    the final human check before publish (PLAN §3.7 sample-NDA test drive)."""
    version = _load_version(db, version_id)
    data = _draft_bytes(db, version)
    token_by_name = {t.name: t for t in list_tokens(db)}
    values = {
        name: _dummy_value(name, token_by_name.get(name))
        for name in scan_token_names(data)
    }
    filled = fill_docx(data, values, strip_unfilled=False)
    filename = f"test-drive-{version.variant_code}-v{version.version_no}.docx"
    return Response(
        content=filled,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Publish + rollback — promote a version to is_current + emit the drift event
# --------------------------------------------------------------------------- #
def _drift_notifier(request: Request) -> DriftNotifier:
    """Best-effort owner notification over the process's wired ReplyService (fail-soft; the form is
    flagged regardless of whether an owner channel is reachable)."""
    return DriftNotifier(service=getattr(request.app.state, "reply_service", None))


def _promote(
    db: Session, request: Request, version: TemplateVersion, data: bytes
) -> dict[str, Any]:
    """Make ``version`` the current one for its (template, variant), archiving the prior current, then
    emit ``template_published`` drift with the old→new token diff (the awaiting caller of wave A)."""
    new_tokens = scan_docx_tokens(data)
    prior = (
        db.execute(
            select(TemplateVersion).where(
                TemplateVersion.template_id == version.template_id,
                TemplateVersion.variant_code == version.variant_code,
                TemplateVersion.is_current.is_(True),
            )
        )
        .scalars()
        .first()
    )
    old_tokens: set[str] = set()
    if prior is not None and prior.id != version.id and prior.blob_id:
        prior_blob = db.get(DocumentBlob, prior.blob_id)
        if prior_blob and prior_blob.bytes:
            old_tokens = scan_docx_tokens(prior_blob.bytes)

    siblings = (
        db.execute(
            select(TemplateVersion).where(
                TemplateVersion.template_id == version.template_id,
                TemplateVersion.variant_code == version.variant_code,
            )
        )
        .scalars()
        .all()
    )
    # Promote ``version`` and RETIRE every other version of this (template, variant): once a version is
    # made current, the superseded drafts are deleted so only the live version remains in the history
    # (old token diff was already captured from ``prior`` above). Blobs are left intact — they may be
    # shared by sha256 and are cleaned up out of band.
    deleted = 0
    for sib in siblings:
        if sib.id == version.id:
            sib.is_current = True
        else:
            db.delete(sib)
            deleted += 1
    db.commit()

    added = sorted(new_tokens - old_tokens)
    removed = sorted(old_tokens - new_tokens)
    emit_template_published(
        db,
        version.template_id,
        added_tokens=added,
        removed_tokens=removed,
        notifier=_drift_notifier(request),
    )
    log.info(
        "studio.promoted",
        version_id=version.id,
        template_id=version.template_id,
        variant=version.variant_code,
        added=len(added),
        removed=len(removed),
        retired_drafts=deleted,
    )
    return {
        "template_id": version.template_id,
        "version_id": version.id,
        "variant": version.variant_code,
        "added_tokens": added,
        "removed_tokens": removed,
    }


@router.post(
    "/admin/studio/{version_id}/publish", dependencies=[Depends(require_admin_csrf)]
)
def studio_publish(
    version_id: str,
    request: Request,
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    """Publish a draft: gated on the checklist's ``missing_required`` being empty (never trusting the
    client's disabled button), then promote + emit drift."""
    version = _load_version(db, version_id)
    data = _draft_bytes(db, version)
    template = _template_of(db, version)
    required, known, _ = _required_and_known(db, template, version.variant_code)
    checklist = analyze(data, required, known)
    if checklist["missing_required"]:
        raise EngineError(
            409,
            "publish_blocked",
            "Required tokens are still missing — add them before publishing.",
            {"missing_required": checklist["missing_required"]},
        )
    return JSONResponse({"ok": True, **_promote(db, request, version, data)})


@router.post(
    "/admin/templates/versions/{version_id}/rollback",
    dependencies=[Depends(require_admin_csrf)],
)
def rollback_version(
    version_id: str,
    request: Request,
    principal: Principal = Depends(admin_api),
    db: Session = Depends(get_db),
) -> Response:
    """One-click rollback: re-point ``is_current`` to a prior version (which must have loaded bytes) and
    emit the same ``template_published`` drift event with the token diff."""
    version = _load_version(db, version_id)
    data = _draft_bytes(db, version)
    return JSONResponse({"ok": True, **_promote(db, request, version, data)})


def register(app: FastAPI) -> None:
    """Mount the studio router. The shell (static, security headers, login redirect) is registered by
    ``app.api.routes_admin_ui.register`` from ``app.main``."""
    app.include_router(router)
