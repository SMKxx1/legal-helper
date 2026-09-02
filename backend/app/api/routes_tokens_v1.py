"""SERVICE-authed token-registry + template-draft plane for the in-Word tokenizer (``/v1``).

The Word add-in tokenises a template *inside Word* (where the document IS the document, so the
tokenisation is byte-exact — unlike a browser preview that can only approximate a .docx). To do that
it needs two things the engine already knows how to produce, exposed under the engine ``X-API-Key``
(SERVICE principal) auth the add-in already uses:

* ``GET  /v1/tokens`` — the registry token palette (name / label / help_text / data_type / party /
  scope_code) PLUS the exact ``{{name}}`` placeholder text to splice into the document, so the add-in
  never has to reconstruct the brace form itself.
* ``POST /v1/support_task/template-draft`` — land the tokenised ``.docx`` the author produced in Word
  as a NEW DRAFT ``template_version`` (``is_current=False``) for the resolved (jurisdiction,
  counterparty, mutuality) template + variant. It deliberately does NOT publish: an admin still
  publishes it through the studio, which keeps the required-token checklist gate in front of go-live.

Both routes reuse the same authorization the sibling ``/v1/support_task/generate-nda`` route enforces:
:func:`app.auth.principal.engine_principal` resolves the SERVICE principal, and the route then requires
the principal to hold AT LEAST ONE engine entitlement (rejects an empty/viewer principal with 403
``not_entitled``). This is a feature module kept separate from ``routes_support`` for fault isolation;
``app.main`` mounts it with a single :func:`register` call.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.errors import EngineError
from app.api.uploads import guard_zip_bomb
from app.auth.principal import ResolvedPrincipal, engine_principal
from app.config import settings
from app.db import get_db
from app.models_v2 import DocumentBlob, Template, TemplateVersion
from app.registry.tokens import list_tokens
from app.studio.docview import load_document
from app.support_task.generator import DOCX_MIME, normalize_codes
from app.telemetry import get_logger

log = get_logger("nda.tokens_v1")

router = APIRouter(tags=["tokens_v1"])

#: The two template variants (mirrors ``app.seed_catalog._VARIANTS`` / ``ref_template_variant``).
_VARIANTS = ("empty", "tokenised")


def _require_engine_entitlement(principal: ResolvedPrincipal, action: str) -> None:
    """The SAME per-action gate ``generate_nda`` enforces: ``engine_principal`` only RESOLVES a caller;
    a write/read here still requires the principal to hold AT LEAST ONE engine entitlement (an empty /
    viewer principal is rejected 403 ``not_entitled``)."""
    if not getattr(principal, "entitlements", None):
        raise EngineError(
            403,
            "not_entitled",
            "This principal is not entitled to use the tokenizer plane.",
            {"action": action},
        )


# --------------------------------------------------------------------------- #
# GET /v1/tokens — the registry palette for the in-Word tokenizer
# --------------------------------------------------------------------------- #
@router.get("/v1/tokens")
def tokens(
    principal: ResolvedPrincipal = Depends(engine_principal),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return every registry token with the metadata the add-in palette renders PLUS the exact
    ``{{name}}`` placeholder text to insert. Gated on the same >=1-entitlement rule as generate-nda."""
    _require_engine_entitlement(principal, "support.tokens")
    views = list_tokens(db)
    payload = [
        {
            "name": t.name,
            "label": t.label,
            "help_text": t.help_text,
            "data_type": t.data_type,
            "party": t.party,
            "scope_code": t.scope_code,
            # The literal text the add-in splices into the document — never reconstructed client-side.
            "placeholder": "{{" + t.name + "}}",
        }
        for t in views
    ]
    return JSONResponse({"tokens": payload})


# --------------------------------------------------------------------------- #
# POST /v1/support_task/template-draft — land a tokenised .docx as a NEW DRAFT version
# --------------------------------------------------------------------------- #
def _store_blob(db: Session, data: bytes) -> DocumentBlob:
    """Content-addressed blob write (mirrors ``routes_studio._store_blob``): reuse the row with this raw
    sha256 or create one; a blob row is never mutated in place."""
    import hashlib

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


def _resolve_template(
    db: Session, jur: str, cp: str, mut: str, *, org_id: str | None
) -> Template:
    """The single ``Template`` row for the (jurisdiction, counterparty, mutuality) combo — scoped to the
    principal's org like ``generator.resolve_template_docx``. Raises 404 for an unknown combination."""
    stmt = select(Template).where(
        Template.jurisdiction_code == jur,
        Template.counterparty_type_code == cp,
        Template.mutuality_code == mut,
    )
    if org_id:
        stmt = stmt.where(Template.org_id == org_id)
    template = db.execute(stmt).scalars().first()
    if template is None:
        raise EngineError(
            404,
            "template_not_found",
            f"No template for {jur}/{cp}/{mut} — check the jurisdiction/counterparty/mutuality combo.",
        )
    return template


@router.post("/v1/support_task/template-draft")
async def template_draft(
    file: UploadFile = File(...),
    jurisdiction: str = Form(...),
    counterparty_type: str = Form(...),
    mutuality: str | None = Form(default=None),
    variant: str = Form(default="tokenised"),
    principal: ResolvedPrincipal = Depends(engine_principal),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Land a tokenised ``.docx`` (produced by the in-Word tokenizer) as a NEW DRAFT ``template_version``
    for the resolved template + variant. Never publishes (``is_current`` stays ``False``) — an admin
    publishes it via the studio, preserving the required-token checklist gate before go-live.

    Rejections (all in the typed ``{"error": {...}}`` envelope): a non-.docx / empty / unreadable file,
    an over-cap upload (``max_upload_mb``), an unknown variant, and an unknown
    jurisdiction/counterparty/mutuality combination.
    """
    _require_engine_entitlement(principal, "support.template_draft")

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
    max_bytes = settings.max_upload_bytes
    data = await file.read(max_bytes + 1)  # bounded — never buffer an oversized body
    if len(data) > max_bytes:
        raise EngineError(
            413,
            "request_too_large",
            f"File exceeds the {settings.max_upload_mb} MB limit.",
        )
    if not data:
        raise EngineError(422, "empty_file", "The uploaded file is empty.")
    guard_zip_bomb(data)  # same decompression-bomb defence as the other upload paths
    load_document(data)  # raises studio_bad_docx (422) if it is not a readable .docx

    jur, cp, mut = normalize_codes(jurisdiction, counterparty_type, mutuality)
    template = _resolve_template(
        db, jur, cp, mut, org_id=getattr(principal, "org_id", None)
    )

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
        is_current=False,  # DRAFT — an admin publishes via the studio (checklist gate stays in front)
        created_by=principal.principal_id,  # attribution: the SERVICE principal that uploaded it
    )
    db.add(version)
    db.commit()
    log.info(
        "tokens_v1.template_draft",
        version_id=version.id,
        template_id=template.id,
        variant=variant,
        version_no=next_no,
        created_by=principal.principal_id,
    )
    return JSONResponse(
        {
            "version_id": version.id,
            "template": template.name,
            "variant": variant,
            "version_no": next_no,
        },
        status_code=201,
    )


def register(app: FastAPI) -> None:
    """Mount the tokenizer router. The EngineError handler is registered once in ``app.main``."""
    app.include_router(router)
