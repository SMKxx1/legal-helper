"""FastAPI composition root.

``create_app`` wires the app in a fixed order: settings -> logging -> capability registry (its report
is logged) -> exception handlers -> middleware (correlation-id) -> routers -> shallow ``/healthz`` ->
a catch-all default-deny 404 (registered LAST). It must boot with ZERO env vars set; missing
configuration only disables capabilities.

Routers mounted so far: the Word add-in static bundle, ``/api/auth`` and ``/api/me`` (Phase 1),
``/api/reviews`` (Phase 2), ``/api/me/usage``/``/api/admin/usage`` and ``/``/``/api/status``
(Phase 3) — this file's job is the fixed shell around them, not the routes themselves.

Routes:
* ``GET /healthz`` — shallow public liveness (200 ``{"status":"ok"}`` / 503). No capability detail
  leaks here; the detailed report is ``GET /api/status``.
* a catch-all that returns a JSON 404 for everything else — an explicit default-deny fallback,
  registered AFTER every router so specific routes always match first.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from .api import (
    reviews_repo,
    routes_addin,
    routes_auth,
    routes_me,
    routes_pages,
    routes_reviews,
    routes_usage,
)
from .api.errors import EngineError, engine_error_handler
from .capabilities import CapabilityRegistry, build_registry
from .config import Settings, get_settings
from .db import SessionLocal, init_db
from .telemetry import CorrelationIdMiddleware, configure_logging, get_logger

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

# The single ``{"error": {"code", "message", "details"}}`` envelope maps HTTP status -> code so
# EVERY error path (EngineError, a raw HTTPException, validation 422, unhandled 500) returns one
# uniform shape.
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


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble and return the FastAPI application. Safe to call with no environment configured."""
    settings = settings or get_settings()
    configure_logging(settings)
    log = get_logger("legal_helper.main")

    registry = build_registry(settings)
    log.info(
        "startup",
        app_env=settings.app_env,
        log_level=settings.log_level,
        log_format=settings.log_format,
        capabilities=registry.report(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # The idempotent fresh-DB/test safety net; it never ALTERs an existing table. Alembic is the
        # sole source of truth for schema changes (deploy runs `python -m app.db_migrate` first).
        init_db()
        # Runs each capability's health probe (today: `database` checks APP_SECRET_KEY in prod) so
        # /healthz reflects real boot-time health, not just config presence.
        await registry.run_probes()
        # Crash recovery (plan §3): a process restart mid-review leaves its row stuck at
        # queued/running forever — the add-in would poll it indefinitely. Every such row belongs
        # to the process that just died, so all of them are failed here regardless of age.
        with SessionLocal() as db:
            stale = reviews_repo.fail_stale_jobs(db, older_than_minutes=0)
            if stale:
                log.warning("reviews.stale_jobs_failed", count=stale)
        yield

    # OpenAPI/docs stay disabled (default-deny; this is a teaching demo backend, not a public API
    # surface). They return through the JSON 404 fallback below.
    app = FastAPI(
        title="Legal Helper",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.capabilities = registry
    # GET /api/status's uptime_s (routes_pages) — monotonic so a wall-clock adjustment can't skew it.
    app.state.started_at = time.monotonic()

    # --- Error envelope: registered ONCE here so EVERY error path returns the single
    # ``{"error": {"code", "message", "details"}}`` shape. ---
    app.add_exception_handler(EngineError, engine_error_handler)  # type: ignore[arg-type]  # Starlette's handler type is invariant on the exception subclass

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Render raw HTTPExceptions in the same envelope as EngineError, so the API surface is
        uniformly ``{"error": {...}}``."""
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
        (500) — the details are logged server-side, never leaked to the client."""
        log.exception(
            "unhandled_exception", method=request.method, path=request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal",
                    "message": "Internal server error.",
                    "details": {},
                }
            },
        )

    # --- Middleware. Same-origin, bearer-token auth (Phase 1) needs neither CORS nor CSRF. ---
    app.add_middleware(CorrelationIdMiddleware)

    # --- Routers. ---
    # Word add-in (served SAME-ORIGIN with the /api routes below). Registered before the catch-all
    # 404 so /addin/* matches here and non-/addin paths still default-deny.
    routes_addin.register(app)
    app.include_router(routes_auth.router)
    app.include_router(routes_me.router)
    app.include_router(routes_reviews.router)
    app.include_router(routes_usage.router)
    app.include_router(routes_pages.router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Shallow liveness probe. 200 while the process can serve; 503 only if a critical capability
        is unhealthy. Deliberately exposes no capability detail."""
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
