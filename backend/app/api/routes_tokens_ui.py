"""Token-registry admin pages + JSON API (PLAN §3.7 — "Token registry (user-managed)").

The server-rendered admin surface over :mod:`app.registry.tokens`: list every registry token
(name/label/type/party + a usage count), create one (validated snake_case), edit its metadata, and
run the **usage-gated delete** — which first renders every template version + form field that would
break, and requires a **typed name confirmation** before a forced delete proceeds. Every create/delete
emits the registry's built-in drift event (flag affected forms + notify owners) through the wired
:class:`ReplyService`; a metadata edit changes no token set, so it emits none.

Surfaces, all behind ``require_admin`` + the optional ``require_admin_ip`` allowlist (router-level so
no route is missed):

* ``GET  /admin/tokens``                    — list page (create form inline).
* ``GET  /admin/tokens/{name_or_id}``       — detail/edit page + live usage report.
* ``POST   /api/admin/tokens``              — create (JSON).
* ``PATCH  /api/admin/tokens/{name_or_id}`` — update metadata.
* ``GET    /api/admin/tokens/{name_or_id}/usage`` — usage report JSON (delete-confirm modal).
* ``POST   /api/admin/tokens/{name_or_id}/delete`` — usage-gated delete (force + typed confirm).

House rules: typed, structlog, CSP-clean server-rendered Jinja (no inline JS/CSS), zero network in
tests (drift notification degrades to a no-op with no sink wired).
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api._admin_templating import admin_templates, page_context
from app.api.errors import EngineError
from app.auth.admin_ip import require_admin_ip
from app.auth.deps import require_admin
from app.auth.sessions import Principal
from app.db import get_db
from app.registry import drift as drift_mod
from app.registry import tokens as reg
from app.telemetry import get_logger

log = get_logger("nda.admin.tokens")

router = APIRouter(
    tags=["admin-tokens"],
    dependencies=[Depends(require_admin), Depends(require_admin_ip)],
)


# --------------------------------------------------------------------------- #
# Reply-service seam for drift notifications (fail-soft; no network in tests)
# --------------------------------------------------------------------------- #
def _drift_notifier() -> drift_mod.DriftNotifier:
    """A :class:`DriftNotifier` bound to the process-wide reply service (Slack/email sinks). With no
    service wired — or none of its channels configured (tests) — notification degrades to a no-op; the
    affected forms are still flagged ``needs_update`` regardless (flagging is not fail-soft)."""
    service: Any | None = None
    try:
        from app.bot import router as _bot_router

        delivery = getattr(_bot_router, "_DELIVERY", None)
        if delivery:
            service = delivery[0]
    except Exception:  # noqa: BLE001 — the notifier is optional; never block a token mutation on it
        service = None
    return drift_mod.DriftNotifier(service=service)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _token_dict(view: reg.TokenView) -> dict[str, Any]:
    return {
        "id": view.id,
        "name": view.name,
        "placeholder": view.placeholder,
        "scope_code": view.scope_code,
        "label": view.label,
        "help_text": view.help_text,
        "data_type": view.data_type,
        "party": view.party,
        "fallback_text": view.fallback_text,
    }


def _usage_dict(usage: reg.UsageReport) -> dict[str, Any]:
    return {
        "token_id": usage.token_id,
        "token_name": usage.token_name,
        "in_use": usage.in_use,
        "template_versions": [
            {
                "template_version_id": tv.template_version_id,
                "template_id": tv.template_id,
                "template_name": tv.template_name,
                "variant_code": tv.variant_code,
                "version_no": tv.version_no,
                "is_current": tv.is_current,
            }
            for tv in usage.template_versions
        ],
        "form_bindings": [
            {
                "form_id": fb.form_id,
                "form_name": fb.form_name,
                "kind": fb.kind,
                "block_ids": list(fb.block_ids),
                "in_draft": fb.in_draft,
                "in_published": fb.in_published,
            }
            for fb in usage.form_bindings
        ],
    }


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class TokenCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    label: str = ""
    help_text: str = ""
    data_type: str = "text"
    party: str = "internal"
    fallback_text: str = ""
    scope_code: str = reg.DEFAULT_SCOPE_CODE


class TokenPatchIn(BaseModel):
    label: str | None = None
    help_text: str | None = None
    data_type: str | None = None
    party: str | None = None
    fallback_text: str | None = None


class TokenDeleteIn(BaseModel):
    force: bool = False
    confirm: str = ""


# --------------------------------------------------------------------------- #
# Error mapping — registry exceptions -> the standard envelope
# --------------------------------------------------------------------------- #
def _raise_registry_error(exc: reg.TokenRegistryError) -> NoReturn:
    if isinstance(exc, reg.TokenExistsError):
        raise EngineError(409, "conflict", str(exc))
    if isinstance(exc, reg.TokenNotFoundError):
        raise EngineError(404, "not_found", str(exc))
    # TokenValidationError and any other base registry error
    raise EngineError(400, "bad_request", str(exc))


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("/admin/tokens", response_class=HTMLResponse)
def tokens_list_page(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> Response:
    """List every registry token + an inline create form (PLAN §3.7).

    Usage is loaded LAZILY, per token, via the "View usage" button (``/api/admin/tokens/{name}/usage``).
    The list itself does NO usage scan: computing usage for every token on page load meant re-parsing
    every template ``.docx`` once per token (O(tokens × templates) python-docx parses), which made this
    page slow while every other admin page was instant. Now the list is a plain metadata render.
    """
    rows = [_token_dict(v) for v in reg.list_tokens(db)]
    return admin_templates.TemplateResponse(
        request,
        "tokens/list.html",
        page_context(
            request,
            principal,
            "tokens",
            tokens=rows,
            data_types=list(reg.DATA_TYPES),
            parties=list(reg.PARTIES),
        ),
    )


@router.get("/admin/tokens/{name_or_id}", response_class=HTMLResponse)
def token_detail_page(
    name_or_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> Response:
    """Detail/edit page for one token, with the full delete-safety usage report rendered inline."""
    view = reg.get_token(db, name_or_id)
    if view is None:
        raise EngineError(404, "not_found", f"No such token: {name_or_id!r}.")
    usage = reg.token_usage(db, view.id)
    return admin_templates.TemplateResponse(
        request,
        "tokens/detail.html",
        page_context(
            request,
            principal,
            "tokens",
            token=_token_dict(view),
            usage=_usage_dict(usage),
            data_types=list(reg.DATA_TYPES),
            parties=list(reg.PARTIES),
        ),
    )


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
@router.post("/api/admin/tokens")
def create_token_api(body: TokenCreateIn, db: Session = Depends(get_db)) -> Response:
    """Create a registry token (validated snake_case, unique) + emit ``token_created`` drift."""
    try:
        view = reg.create_token(
            db,
            name=body.name,
            label=body.label,
            help_text=body.help_text,
            data_type=body.data_type,
            party=body.party,
            fallback_text=body.fallback_text,
            scope_code=body.scope_code,
            notifier=_drift_notifier(),
        )
    except reg.TokenRegistryError as exc:
        _raise_registry_error(exc)
    return JSONResponse(status_code=201, content={"token": _token_dict(view)})


@router.patch("/api/admin/tokens/{name_or_id}")
def update_token_api(
    name_or_id: str, body: TokenPatchIn, db: Session = Depends(get_db)
) -> Response:
    """Update a token's metadata in place. Name/placeholder are immutable (a rename is a delete +
    recreate, surfaced via the usage report); no drift — the token set is unchanged."""
    try:
        view = reg.update_meta(
            db,
            name_or_id,
            label=body.label,
            help_text=body.help_text,
            data_type=body.data_type,
            party=body.party,
            fallback_text=body.fallback_text,
        )
    except reg.TokenRegistryError as exc:
        _raise_registry_error(exc)
    return JSONResponse({"token": _token_dict(view)})


@router.get("/api/admin/tokens/{name_or_id}/usage")
def token_usage_api(name_or_id: str, db: Session = Depends(get_db)) -> Response:
    """The full usage report for a token (read-only) — what the delete-confirm modal renders."""
    try:
        usage = reg.token_usage(db, name_or_id)
    except reg.TokenRegistryError as exc:
        _raise_registry_error(exc)
    return JSONResponse({"usage": _usage_dict(usage)})


@router.post("/api/admin/tokens/{name_or_id}/delete")
def delete_token_api(
    name_or_id: str, body: TokenDeleteIn, db: Session = Depends(get_db)
) -> Response:
    """Usage-gated delete (PLAN §3.7). Always returns the usage report. If the token is in use and
    ``force`` is not set, nothing is deleted (``deleted=False``) so the UI can show the consequences.
    A forced delete additionally requires ``confirm`` to exactly equal the token name (the typed
    confirmation) — otherwise a 400. On delete, the built-in ``token_deleted`` drift flags every
    affected form and notifies its owner."""
    view = reg.get_token(db, name_or_id)
    if view is None:
        raise EngineError(404, "not_found", f"No such token: {name_or_id!r}.")

    if body.force and body.confirm.strip() != view.name:
        raise EngineError(
            400,
            "confirmation_required",
            f"To force-delete, type the token name exactly ({view.name!r}) to confirm.",
        )

    try:
        result = reg.delete_token(
            db, view.id, force=body.force, notifier=_drift_notifier()
        )
    except reg.TokenRegistryError as exc:
        _raise_registry_error(exc)

    return JSONResponse(
        {
            "deleted": result.deleted,
            "forced": result.forced,
            "usage": _usage_dict(result.usage),
        }
    )
