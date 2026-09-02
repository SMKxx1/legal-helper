"""FastAPI composition root.

``create_app`` wires the app in a fixed order: settings -> logging -> capability registry (its report
is logged) -> telemetry export -> exception handlers -> middleware (CSRF -> CORS -> correlation-id) ->
routers -> shallow ``/healthz`` -> a catch-all default-deny 404 (registered LAST). It must boot with
ZERO env vars set; missing configuration only disables capabilities.

P1 additions over the P0 skeleton (kept surgical — the P0 structure, logging, capability boot,
correlation middleware, shallow ``/healthz``, and the catch-all 404 are unchanged):
* a ``lifespan`` that runs ``init_db`` + the boot-time seed hooks (default template, bootstrap admin)
  — NOT migrations (those run pre-deploy);
* the ported API routers: ``/api/auth``, ``/api/admin``, the admin-only ``/api`` meta/settings/
  templates routers, and the ``/v1`` engine + ``/v1/support_task`` generation planes;
* credentialed CORS from ``settings.cors_origin_list`` (EXACT origins, never a wildcard);
* double-submit CSRF for cookie-authenticated state-changing ``/api/*`` requests;
* the single ``{"error": {...}}`` envelope for every error path.

Routes:
* ``GET /healthz`` — shallow public liveness (200 ``{"status":"ok"}`` / 503). No capability detail
  leaks here (PLAN §6); the detailed report is reserved for the future admin surface.
* a catch-all that returns a JSON 404 for everything else — an explicit default-deny fallback,
  registered AFTER every router so specific routes always match first.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .api import (
    routes_addin,
    routes_admin,
    routes_admin_ui,
    routes_auth,
    routes_providers,
    routes_settings,
    routes_studio,
    routes_support,
    routes_tally,
    routes_templates,
    routes_tokens_ui,
    routes_tokens_v1,
    routes_v1,
)
from .api.errors import EngineError, engine_error_handler
from .auth.deps import SESSION_COOKIE, require_admin
from .auth.security import CSRF_COOKIE, csrf_matches, is_csrf_protected
from .bot.channels.slack import mount_slack
from .bot.delivery import wire_delivery
from .capabilities import CapabilityRegistry, build_registry
from .config import Settings, get_settings
from .db import SessionLocal, init_db
from .seed import seed_default_template
from .telemetry import (
    CorrelationIdMiddleware,
    configure_logging,
    configure_tracing,
    get_logger,
)

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

log = get_logger("nda.main")

_BOOTSTRAP_LOCK_KEY = (
    0x4E444141  # fixed Postgres advisory-lock key for bootstrap-admin seeding
)

# The single ``{"error": {"code", "message", "details"}}`` envelope maps HTTP status -> code so
# EVERY error path (EngineError, raw HTTPException from admin routers + FastAPI built-ins,
# validation 422, unhandled 500) returns one uniform shape.
_HTTP_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "request_too_large",
    415: "unsupported_media_type",
    422: "unprocessable",
    429: "rate_limited",
}


def _stamp_path_security_headers(path: str, response: Response) -> None:
    """Re-apply the path-scoped hardening headers on an error response.

    Starlette's ``ServerErrorMiddleware`` (which invokes the unhandled-``Exception`` handler for a
    genuine 500) sits OUTSIDE the user middleware stack, so a real 500 never re-enters
    :class:`~app.api.routes_forms.FormSecurityHeadersMiddleware` /
    :class:`~app.api.routes_admin_ui.AdminSecurityHeadersMiddleware` and would otherwise ship WITHOUT the
    ``/f`` CSP / no-store / no-referrer guarantees (the PII surface) or the ``/admin`` hardening headers.
    This mirrors those middlewares' EXACT path scopes so an unhandled error on ``/f`` or ``/admin`` still
    carries its headers (PLAN §3.6/§6). Handled errors (EngineError/HTTPException/422) run in the INNER
    exception middleware, so their responses already pass back through the stamping middleware — only the
    outermost 500 path needs this.
    """
    if path == "/admin" or path.startswith("/admin/"):
        for key, value in routes_admin_ui.ADMIN_SECURITY_HEADERS.items():
            response.headers[key] = value


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF for state-changing /api/auth + /api/admin requests: the X-CSRF-Token
    header must echo the (non-HttpOnly) csrf cookie. Cookie-authenticated mutations are otherwise
    forgeable cross-site; an attacker's page can't read the csrf cookie to set the header."""

    async def dispatch(self, request: Request, call_next):
        # CSRF double-submit applies only to COOKIE-authenticated state-changing requests: a request
        # with no session cookie has nothing to forge, so let auth return its 401 rather than a
        # misleading csrf 403. A genuine cross-site forgery DOES carry the victim's session cookie
        # (the browser sends it automatically), so gating on cookie presence never weakens this.
        if (
            is_csrf_protected(request.method, request.url.path)
            and request.cookies.get(SESSION_COOKIE)
            and not csrf_matches(
                request.cookies.get(CSRF_COOKIE, ""),
                request.headers.get("x-csrf-token", ""),
            )
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "csrf_failed",
                        "message": "CSRF token missing or invalid.",
                        "details": {},
                    }
                },
            )
        return await call_next(request)


def seed_bootstrap_admin(
    settings: Settings,
    session_factory=None,
    *,
    user_id: str | None = None,
    password: str | None = None,
) -> None:
    """First-run: if ZERO admins exist in the default org, create one from ADMIN_BOOTSTRAP_* with
    ``must_change_password=True`` (the first action is a forced password change). Idempotent — a
    reboot is a no-op. Race-safe across workers: a Postgres transaction advisory lock serializes the
    check+insert; on SQLite the UNIQUE(user_id) + IntegrityError catch collapses a concurrent
    double-create to one."""
    from sqlalchemy import func, select, text
    from sqlalchemy.exc import IntegrityError

    from .auth.models import UserAccount
    from .auth.security import hash_password
    from .schemas import DEFAULT_ORG_ID

    sf = session_factory or SessionLocal
    uid = (
        (user_id if user_id is not None else settings.admin_bootstrap_user_id) or ""
    ).strip()
    pw = password if password is not None else (settings.admin_bootstrap_password or "")
    if not uid or not pw:
        return

    created = False
    try:
        with sf() as db, db.begin():
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY}
                )
            n_admins = db.execute(
                select(func.count())
                .select_from(UserAccount)
                .where(
                    UserAccount.org_id == DEFAULT_ORG_ID, UserAccount.role == "admin"
                )
            ).scalar_one()
            if n_admins == 0:
                db.add(
                    UserAccount(
                        org_id=DEFAULT_ORG_ID,
                        user_id=uid,
                        password_hash=hash_password(pw),
                        role="admin",
                        status="active",
                        must_change_password=True,
                    )
                )
                created = True
    except IntegrityError:  # SQLite race: another worker won — benign no-op
        log.info("bootstrap_admin.concurrent_create", user_id=uid)
        return
    if created:
        log.warning(
            "bootstrap_admin.created",
            user_id=uid,
            note="temporary password (must_change_password=True); log in, change it, "
            "then ROTATE/CLEAR ADMIN_BOOTSTRAP_* env vars",
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble and return the FastAPI application. Safe to call with no environment configured."""
    settings = settings or get_settings()
    configure_logging(settings)
    log = get_logger("nda.main")

    registry = build_registry(settings)
    configure_tracing(
        settings, registry
    )  # runtime seam: may transition telemetry_export -> unhealthy
    reply_service = wire_delivery(
        settings
    )  # reply sinks for Slack-originated turns processed in this process

    log.info(
        "startup",
        app_env=settings.app_env,
        log_level=settings.log_level,
        log_format=settings.log_format,
        capabilities=registry.report(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Boot-time seed hooks the source ran at startup (NOT migrations — those are a pre-deploy
        # step). ``init_db`` create_all is the idempotent fresh-DB/test safety net; it never ALTERs.
        init_db()
        with SessionLocal() as db:
            tpl = seed_default_template(db)
            if tpl:
                log.info(
                    "default_template.ready", name=tpl.name, clauses=tpl.clause_count
                )
        seed_bootstrap_admin(settings)
        log.info(
            "ai_provider.ready", provider="anthropic", model=settings.anthropic_model
        )
        yield

    # OpenAPI/docs stay disabled (default-deny; this is a bot backend, not a public API surface).
    # They return through the JSON 404 fallback below and re-open behind admin auth in a later phase.
    app = FastAPI(
        title="NDA Assistant",
        version="0.1.0-p1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.capabilities = registry
    # The per-process ReplyService (channel-aware Slack/email sinks). Exposed so the studio's
    # template-publish drift notifier can reach form owners (fail-soft: None/empty degrades to a no-op).
    app.state.reply_service = reply_service

    # --- Error envelope: registered ONCE here so EVERY error path returns the single
    # ``{"error": {"code", "message", "details"}}`` shape. ---
    app.add_exception_handler(EngineError, engine_error_handler)  # type: ignore[arg-type]  # Starlette's handler type is invariant on the exception subclass

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Render raw HTTPExceptions (admin routers + FastAPI built-ins) in the same envelope as
        EngineError, so the API surface is uniformly ``{"error": {...}}``."""
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        message = (
            exc.detail
            if isinstance(exc.detail, str)
            else "Request could not be processed."
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message, "details": {}}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render pydantic body/query validation failures (422) in the SAME envelope — and STRIP the
        submitted ``input`` (and ``ctx``/``url``) from each error so a 422 never reflects the client's
        payload back (FastAPI's default 422 echoes ``input`` verbatim — e.g. a submitted password)."""
        errors = [
            {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "unprocessable",
                    "message": "Request validation failed.",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Catch-all so an UNHANDLED exception still returns the one ``{"error": {...}}`` envelope
        (500) — the details are logged server-side, never leaked to the client. EngineError and
        HTTPException keep their own (more specific) handlers."""
        log.exception(
            "unhandled_exception", method=request.method, path=request.url.path
        )
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal",
                    "message": "Internal server error.",
                    "details": {},
                }
            },
        )
        # A genuine 500 is handled by the OUTERMOST ServerErrorMiddleware, bypassing the /f + /admin
        # header-stamping middlewares — re-apply their path-scoped hardening headers here (PLAN §3.6/§6).
        _stamp_path_security_headers(request.url.path, response)
        return response

    # --- Middleware. ``add_middleware`` prepends, so the LAST added runs OUTERMOST: add CSRF (inner)
    # -> CORS -> correlation-id (outer) so the correlation id is set FIRST (around everything, incl.
    # the CORS preflight + the error handlers' logging) and CORS handles the preflight before CSRF. ---
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        # Cookie-session auth requires CREDENTIALED CORS, which forbids wildcards: pin exact origins
        # and explicit methods/headers. PROD is same-origin (SameSite=Lax cookies sent, CORS not even
        # engaged); this is the safety net for any genuinely cross-origin caller in cors_origin_list.
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,  # EXACT origins only (never "*") with credentials
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-API-Key", "Authorization"],
    )
    app.add_middleware(
        CorrelationIdMiddleware
    )  # id set first, around every downstream middleware + handler
    # Stamp the strict admin hardening headers (self-only no-inline CSP / no-store / nosniff / frame
    # DENY / no-referrer) on EVERY /admin response — pages, JSON API, static, downloads, errors, 404.
    # Added after the form one (also OUTERMOST, disjoint path scope) so it sees the final /admin
    # response (PLAN §6). Owned by the admin shell (agent 1); covers the concurrently-authored areas.
    app.add_middleware(routes_admin_ui.AdminSecurityHeadersMiddleware)

    # --- Routers. ---
    app.include_router(routes_auth.router)  # public (login) + cookie-guarded per route
    app.include_router(routes_admin.router)  # already require_role('admin') per route
    # The admin-plane /api routers (provider info, settings, templates) perform cookie-authed writes
    # including provider API keys, so they sit behind admin auth at the router level (no handler missed).
    _admin_only = [Depends(require_admin)]
    app.include_router(routes_providers.router, dependencies=_admin_only)
    app.include_router(routes_settings.router, dependencies=_admin_only)
    app.include_router(routes_templates.router, dependencies=_admin_only)
    # P5 wave B admin pages (token registry). The router carries require_admin + the optional
    # require_admin_ip allowlist at the router LEVEL, so every route (pages + JSON) is gated;
    # mutations live under /api/ so the CSRF middleware covers them.
    app.include_router(routes_tokens_ui.router)
    routes_v1.register(app)  # /v1 review engine API
    routes_support.register(
        app
    )  # /v1/support_task generation plane (engine X-API-Key auth)
    routes_tokens_v1.register(
        app
    )  # /v1/tokens + /v1/support_task/template-draft — the in-Word tokenizer plane (SERVICE auth)
    # Bot channels: Slack events + interactivity (PLAN §3.3), mounted BEFORE the catch-all 404 so the
    # /slack/* routes match first. Capability-gated + boot-safe: a disabled/misconfigured slack
    # capability serves clean 503 stubs and never constructs the Bolt app (no crash on missing secret).
    mount_slack(app, settings, registry=registry)

    # Tally intake webhook (POST /integrations/tally/webhook) — the external form's callback (PLAN
    # §3.6). Capability-gated + boot-safe like Slack: a disabled ``tally`` capability serves a clean
    # 503 stub. Registered before the catch-all 404 so the route matches first.
    routes_tally.register(app, settings, registry=registry)

    # Admin static (P5 wave B, agent 2): each admin page area owns its own subdir under admin/static.
    # Mounted at the specific /admin/static/<area> prefix (the shared shell may also mount the parent
    # /admin/static — both resolve the same files, so this stays correct either way). Registered before
    # the catch-all 404 like every other mount.
    _admin_static = Path(__file__).resolve().parent / "admin" / "static"
    app.mount(
        "/admin/static/tokens",
        StaticFiles(directory=str(_admin_static / "tokens")),
        name="admin-tokens-static",
    )

    # Admin shell (agent 1): login/logout + home pages, /admin/static/{shell,studio} mounts, the
    # login-redirect handler for anonymous page hits, and the shared base.html. Registered before the
    # catch-all 404 so /admin/* matches here. The studio router (templates list + tokenizer + publish)
    # mounts on top; both sit behind require_admin + the admin-IP allowlist per route.
    routes_admin_ui.register(app)
    routes_studio.register(app)

    # Word add-in (P6): served SAME-ORIGIN with /v1 so the SameSite=Lax session cookie + the
    # X-API-Key reach the engine with no CORS. GET /addin/config.js is synthesized per request
    # (injects ENGINE_API_KEY, no-store); the rest of /addin/* is the static bundle. Registered
    # before the catch-all 404 so /addin/* matches here and non-/addin paths still default-deny.
    routes_addin.register(app)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Shallow liveness probe. 200 while the process can serve; 503 only if a critical capability
        is unhealthy. Deliberately exposes no capability detail (PLAN §6)."""
        reg: CapabilityRegistry = app.state.capabilities
        if reg.healthy():
            return JSONResponse({"status": "ok"}, status_code=200)
        return JSONResponse({"status": "unhealthy"}, status_code=503)

    @app.api_route("/{full_path:path}", methods=_ALL_METHODS)
    async def not_found(full_path: str) -> JSONResponse:
        """Default-deny fallback: anything not explicitly routed is a JSON 404. Registered LAST so
        every specific route (routers + /healthz) matches first."""
        return JSONResponse(
            {"error": "not_found", "path": "/" + full_path}, status_code=404
        )

    return app
