"""support_task plane — NDA document generation (``/v1/support_task``).

A feature AREA parallel to Review (``/v1``). The Slack/form frontend posts a token→value table
plus either an uploaded tokenised ``.docx`` or a template selector; the backend fills the
placeholders and returns the completed ``.docx``. Reuses the engine ``X-API-Key`` auth
(``engine_principal``) and the shared ``EngineError`` envelope.
"""

from __future__ import annotations

import json
import logging
import re
from functools import partial

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.errors import EngineError
from app.api.uploads import guard_zip_bomb
from app.auth.principal import ResolvedPrincipal, engine_principal
from app.config import settings
from app.db import get_db
from app.support_task import bot_dal
from app.support_task.generator import (
    DOCX_MIME,
    fill_docx,
    normalize_codes,
    resolve_template_docx,
)

log = logging.getLogger("nda.support_task")

router = APIRouter(prefix="/v1/support_task", tags=["support_task"])

_SAFE_NAME = re.compile(r'[\r\n"\\/]+')


def _safe_filename(name: str | None) -> str:
    base = _SAFE_NAME.sub("", (name or "").strip()) or "NDA"
    if not base.lower().endswith(".docx"):
        base += ".docx"
    return base


@router.post(
    "/generate-nda",
    # Returns a binary .docx download, not JSON — document that in OpenAPI so generated clients expect
    # a file body (a Pydantic response_model would mislabel it as an object).
    responses={
        200: {
            "content": {DOCX_MIME: {"schema": {"type": "string", "format": "binary"}}},
            "description": "The filled NDA as a .docx attachment.",
        }
    },
)
async def generate_nda(
    request: Request,
    values: str = Form(
        ..., description="JSON object mapping token name (or {{token}}) -> value"
    ),
    file: UploadFile | None = File(default=None),
    jurisdiction: str | None = Form(default=None),
    counterparty_type: str | None = Form(default=None),
    mutuality: str | None = Form(default=None),
    variant: str = Form(default="tokenised"),
    filename: str | None = Form(default=None),
    idempotency_key: str | None = Form(default=None),
    principal: ResolvedPrincipal = Depends(engine_principal),
    db: Session = Depends(get_db),
) -> Response:
    """Fill a tokenised NDA template and return the completed ``.docx``.

    Source document: the uploaded ``file`` if present, otherwise the current ``variant`` template
    resolved from the DB by (``jurisdiction``, ``counterparty_type``, ``mutuality``).
    ``values`` is a JSON object of token name → value (the "token replacement table").

    ``idempotency_key`` (form field, or the ``X-Idempotency-Key`` header): a caller-supplied
    per-flow-step uuid. A replay of the same key returns the FIRST generated .docx byte-for-byte
    (with ``X-Idempotency-Replayed: true``) — a retried call can never generate against a
    template that changed under it. Keys are scoped to the calling principal and swept after
    ``IDEMPOTENCY_RETENTION_H``.
    """
    try:
        parsed = json.loads(values)
    except (TypeError, ValueError) as exc:
        raise EngineError(
            400, "bad_request", "`values` must be a JSON object."
        ) from exc
    if not isinstance(parsed, dict):
        raise EngineError(
            400, "bad_request", "`values` must be a JSON object of token -> value."
        )

    # SEC: generation is a spend/write action, but this route previously only resolved a principal
    # (engine_principal) without any per-action authorization — so a read-only viewer or any allow-
    # listed principal could generate filled NDAs. There is no dedicated support.generate entitlement
    # yet, so require the caller to hold AT LEAST ONE engine entitlement (rejects empty/viewer),
    # mirroring the authz the sibling /v1/reviews and /v1/redline routes enforce.
    if not getattr(principal, "entitlements", None):
        raise EngineError(
            403,
            "not_entitled",
            "This principal is not entitled to generate documents.",
            {"action": "support.generate"},
        )

    # Idempotent replay: an already-seen (principal, key) returns the FIRST result verbatim —
    # checked AFTER auth (an unauthenticated caller can never probe stored keys).
    idem_key = (
        idempotency_key or request.headers.get("x-idempotency-key") or ""
    ).strip()
    if idem_key:
        prior = await run_in_threadpool(
            bot_dal.idempotency_lookup,
            db,
            principal_id=principal.principal_id,
            purpose="generate_nda",
            key=idem_key[:128],
            org_id=principal.org_id,
        )
        if prior is not None and prior.response_body:
            return Response(
                content=prior.response_body,
                media_type=DOCX_MIME,
                headers={
                    "Content-Disposition": f'attachment; filename="{prior.filename or "NDA.docx"}"',
                    "X-Idempotency-Replayed": "true",
                },
            )

    if file is not None:
        max_bytes = settings.max_upload_mb * 1024 * 1024
        src = await file.read(
            max_bytes + 1
        )  # bounded — don't buffer an oversized body in memory
        if not src:
            raise EngineError(400, "bad_request", "Uploaded file is empty.")
        if len(src) > max_bytes:
            raise EngineError(
                413, "request_too_large", f"Upload exceeds {settings.max_upload_mb} MB."
            )
        if (
            src[:2] != b"PK"
        ):  # OOXML .docx is a zip; reject anything else with a clean 400
            raise EngineError(
                400, "bad_request", "Uploaded file must be a .docx (OOXML) document."
            )
        guard_zip_bomb(
            src
        )  # same decompression-bomb defence as the /v1/reviews upload path
        template_name = None
    else:
        jur, cp, mut = normalize_codes(jurisdiction, counterparty_type, mutuality)
        src, tmpl = resolve_template_docx(
            db, jur, cp, mut, org_id=getattr(principal, "org_id", None), variant=variant
        )
        template_name = getattr(tmpl, "name", None)

    # fill_docx parses + rewrites the OOXML (CPU-bound, blocking) — keep it off the event loop.
    filled = await run_in_threadpool(fill_docx, src, parsed)

    out_name = _safe_filename(
        filename or (f"{template_name} (filled).docx" if template_name else "NDA.docx")
    )
    if idem_key:
        # First-writer-wins: a concurrent duplicate converges on ONE stored result. Serve the
        # winning row's bytes so both callers hold the identical document.
        winner = await run_in_threadpool(
            partial(
                bot_dal.idempotency_store,
                db,
                principal_id=principal.principal_id,
                purpose="generate_nda",
                key=idem_key[:128],
                org_id=principal.org_id,
                response_body=filled,
                filename=out_name,
            )
        )
        filled = winner.response_body or filled
        out_name = winner.filename or out_name
    log.info(
        "support_task.generate-nda: %s bytes -> %s (%s tokens) by %s",
        len(src),
        out_name,
        len(parsed),
        getattr(principal, "principal_id", "?"),
    )
    return Response(
        content=filled,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


def register(app: FastAPI) -> None:
    """Mount the support_task router. The EngineError handler is registered once in app.main."""
    app.include_router(router)
