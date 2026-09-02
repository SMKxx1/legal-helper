"""Admin console shell — the server-rendered ``/admin`` surface (PLAN §3.7, §6).

This module owns the SHARED admin shell that every admin page extends:

* :class:`AdminSecurityHeadersMiddleware` — stamps the strict, self-only, no-inline CSP + no-store /
  nosniff / frame-DENY / no-referrer headers on EVERY ``/admin`` response (mirror of the ``/f``
  hardening; PLAN §6). No admin page may carry an inline ``<script>``/``style``.
* the auth dependencies every ``/admin`` route composes — :func:`admin_page` (HTML pages: redirect an
  anonymous user to the login page, 403 a non-admin) and :func:`admin_api` (JSON endpoints: 401/403 in
  the standard envelope), both after the optional admin-IP allowlist (:func:`require_admin_ip`) — plus
  :func:`require_admin_csrf` (double-submit for state-changing ``/admin`` writes; the global CSRF
  middleware only covers ``/api/*``, so ``/admin`` enforces its own).
* the login / logout / home pages, and the Jinja env + static mount used by the studio pages.

The login page drives the EXISTING hardened session API (``app.api.routes_auth``) entirely client-side
(``login.js``) — this module only serves the shell; it mints no cookies of its own.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.errors import EngineError
from app.auth import sessions
from app.auth.admin_ip import require_admin_ip
from app.auth.deps import SESSION_COOKIE
from app.auth.security import csrf_matches
from app.auth.sessions import Principal
from app.capabilities import CapabilityRegistry
from app.db import get_db
from app.telemetry import get_logger

log = get_logger("nda.admin.ui")

_ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
_TEMPLATES_DIR = _ADMIN_DIR / "templates"
_STATIC_DIR = _ADMIN_DIR / "static"

#: Shared Jinja env for the admin shell + studio pages (autoescape on for .html).
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(tags=["admin-ui"])

#: Strict admin hardening headers (PLAN §6). Self-only, NO inline script/style (so all admin JS/CSS is
#: served as static files from our origin), no framing, no referrer leak, never cached.
ADMIN_SECURITY_HEADERS = MappingProxyType(
    {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
    }
)


class AdminSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply :data:`ADMIN_SECURITY_HEADERS` to every ``/admin`` response — pages, JSON API, static
    assets, downloads, error envelopes and the catch-all 404. Added OUTERMOST in ``app.main`` so it
    sees the final response for any ``/admin`` path; non-``/admin`` paths are untouched."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/admin" or path.startswith("/admin/"):
            for key, value in ADMIN_SECURITY_HEADERS.items():
                response.headers[key] = value
        return response


# --------------------------------------------------------------------------- #
# Auth: page-redirect vs JSON-envelope guards (both after the admin-IP allowlist)
# --------------------------------------------------------------------------- #
class AdminLoginRedirect(Exception):
    """Raised by :func:`admin_page` when an anonymous (or must-change) user hits an HTML admin page —
    the handler turns it into a 303 redirect to the login page, preserving the requested ``next``."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path
        super().__init__("admin login required")


def _principal(request: Request, db: Any) -> Principal | None:
    return sessions.validate(db, request.cookies.get(SESSION_COOKIE) or "")


def admin_page(request: Request, db=Depends(get_db)) -> Principal:
    """Guard for server-rendered admin PAGES. Order: admin-IP allowlist (403 if the network is not
    permitted) → session (redirect to login if anonymous) → admin role (403 for a non-admin) →
    must-change-password (redirect to login, where the change form is offered)."""
    require_admin_ip(
        request
    )  # 403 admin_ip_forbidden when an allowlist is set and the IP is off it
    principal = _principal(request, db)
    if principal is None:
        raise AdminLoginRedirect(request.url.path)
    if principal.role != "admin":
        raise EngineError(403, "forbidden", "Admin access is required.")
    if principal.must_change_password:
        raise AdminLoginRedirect(request.url.path)
    return principal


def admin_api(request: Request, db=Depends(get_db)) -> Principal:
    """Guard for admin JSON endpoints — same checks as :func:`admin_page` but every failure renders in
    the standard ``{"error": {...}}`` envelope (401 anonymous, 403 non-admin / must-change)."""
    require_admin_ip(request)
    principal = _principal(request, db)
    if principal is None:
        raise EngineError(401, "unauthenticated", "Sign-in required.")
    if principal.role != "admin":
        raise EngineError(403, "forbidden", "Admin access is required.")
    if principal.must_change_password:
        raise EngineError(
            403,
            "password_change_required",
            "You must change your password before continuing.",
        )
    return principal


def require_admin_csrf(request: Request) -> None:
    """Double-submit CSRF for state-changing ``/admin`` writes. The global CSRF middleware only guards
    ``/api/*``; admin writes live under ``/admin`` so they enforce the same check here.

    Like the global middleware, this applies only to COOKIE-authenticated requests: a request with no
    session cookie has nothing to forge, so ``admin_api`` returns its 401 rather than a misleading CSRF
    403. A genuine cross-site forgery DOES carry the victim's session cookie, so gating on cookie
    presence never weakens the check."""
    if not request.cookies.get(SESSION_COOKIE):
        return
    if not csrf_matches(
        request.cookies.get("csrf", ""), request.headers.get("x-csrf-token", "")
    ):
        raise EngineError(403, "csrf_failed", "CSRF token missing or invalid.")


# --------------------------------------------------------------------------- #
# Shared page context
# --------------------------------------------------------------------------- #
def page_context(
    request: Request, principal: Principal, *, active_nav: str = "", **extra: Any
) -> dict[str, Any]:
    """The base context every shell page needs. Keys match the shared ``admin/base.html`` contract the
    concurrently-authored areas also use — ``user_id`` (signed-in handle) + ``active_nav`` — plus
    ``csrf_token`` (rendered into a meta tag; admin.js falls back to the cookie when it is absent)."""
    ctx: dict[str, Any] = {
        "user_id": principal.user_id,
        "csrf_token": request.cookies.get("csrf", ""),
        "active_nav": active_nav,
    }
    ctx.update(extra)
    return ctx


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    """The login shell (anonymous-accessible). ``login.js`` drives ``/api/auth/*`` and, once signed in,
    redirects to ``?next`` (same-origin admin paths only)."""
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/admin/access", response_class=HTMLResponse)
def access_page(
    request: Request, principal: Principal = Depends(admin_page)
) -> Response:
    """The bot access-control console (PLAN §3.4 rework): the approval allowlist + roles, the admin
    routing (channel/email), and the pending-request queue — all managed here instead of via env keys.
    The page's ``access.js`` drives the ``/api/admin/allowlist`` / ``/pending`` / ``/admin-routing`` JSON
    endpoints (each itself behind ``require_admin``)."""
    return templates.TemplateResponse(
        request,
        "access.html",
        page_context(request, principal, active_nav="access"),
    )


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/", response_class=HTMLResponse)
def home_page(request: Request, principal: Principal = Depends(admin_page)) -> Response:
    """Admin home: area cards + a live capability-health summary (PLAN §6 detail-behind-auth)."""
    registry: CapabilityRegistry = request.app.state.capabilities
    return templates.TemplateResponse(
        request,
        "home.html",
        page_context(
            request, principal, active_nav="home", capabilities=registry.report()
        ),
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def register(app: FastAPI) -> None:
    """Mount the admin shell: static assets, the security-headers middleware, the login-redirect
    handler, and the shell router. Called from ``app.main`` (which owns the middleware stack); the
    studio router registers separately (``app.api.routes_studio``)."""
    # Per-area static mounts (the shell owns shell/ + studio/; the forms/tokens agent mounts its own
    # builder/ + tokens/ subdirs from main). Distinct prefixes, so no mount shadows another.
    app.mount(
        "/admin/static/shell",
        StaticFiles(directory=str(_STATIC_DIR / "shell")),
        name="admin-shell-static",
    )
    app.mount(
        "/admin/static/studio",
        StaticFiles(directory=str(_STATIC_DIR / "studio")),
        name="admin-studio-static",
    )

    @app.exception_handler(AdminLoginRedirect)
    async def _admin_login_redirect(_: Request, exc: AdminLoginRedirect) -> Response:
        target = "/admin/login"
        nxt = exc.next_path or ""
        if nxt and nxt.startswith("/admin") and nxt != "/admin/login":
            from urllib.parse import quote

            target = f"/admin/login?next={quote(nxt, safe='/')}"
        # 303: a GET redirect so the browser navigates to the login page.
        return RedirectResponse(target, status_code=303)

    app.include_router(router)
