"""FastAPI auth dependencies (P0-5): session cookie -> Principal, role + must-change gates.

These resolve the ``sid`` HttpOnly cookie via the cached session service (P0-4), so a cache hit
costs no DB query. Failures raise ``EngineError`` so they render in the standard
``{"error":{code,message,details}}`` envelope. ``require_role`` bundles the must-change-password
gate so any role-guarded endpoint rejects a user who must first reset their password; the
password-change endpoint itself uses ``get_current_user`` (which does NOT enforce that gate).
"""

from __future__ import annotations

import threading
import time

from fastapi import Cookie, Depends

from app.api.errors import EngineError
from app.auth import sessions
from app.auth.sessions import Principal
from app.db import get_db

SESSION_COOKIE = "sid"

# Throttle last_seen_at writes so the hot auth path is not a DB write per request. The map is
# touched from FastAPI's sync-dep threadpool, so guard it with a lock; bound it so it can't grow
# without limit (one entry per distinct session id seen).
_LAST_SEEN_INTERVAL_S = 60.0
_LAST_SEEN_MAX = 10_000
_last_seen_at: dict[str, float] = {}
_last_seen_lock = threading.Lock()


def _maybe_touch_last_seen(db, token: str, session_id: str) -> None:
    now = time.monotonic()
    with _last_seen_lock:
        if now - _last_seen_at.get(session_id, 0.0) < _LAST_SEEN_INTERVAL_S:
            return
        _last_seen_at[session_id] = now
        if (
            len(_last_seen_at) > _LAST_SEEN_MAX
        ):  # prune entries past the throttle window
            cutoff = now - 2 * _LAST_SEEN_INTERVAL_S
            for k in [k for k, t in _last_seen_at.items() if t < cutoff]:
                _last_seen_at.pop(k, None)
    try:  # best-effort; uses the request session (test-overridable)
        sessions.touch_last_seen(db, token)
        db.commit()
    except Exception:  # noqa: BLE001 — last_seen is non-critical
        db.rollback()


def get_current_user(
    sid: str | None = Cookie(default=None), db=Depends(get_db)
) -> Principal:
    """Resolve the signed-in principal from the ``sid`` cookie, or 401. Does NOT enforce the
    must-change-password gate (so the password-change endpoint can use it)."""
    principal = sessions.validate(db, sid or "")
    if principal is None:
        raise EngineError(401, "unauthenticated", "Sign-in required.")
    _maybe_touch_last_seen(db, sid or "", principal.session_id)
    return principal


def current_org(principal: Principal = Depends(get_current_user)) -> str:
    """The signed-in user's org_id (tenant scope)."""
    return principal.org_id


def require_password_changed(
    principal: Principal = Depends(get_current_user),
) -> Principal:
    """Block any entitlement-bearing endpoint until the user has changed a forced-reset password."""
    if principal.must_change_password:
        raise EngineError(
            403,
            "password_change_required",
            "You must change your password before continuing.",
        )
    return principal


def require_role(*roles: str):
    """Dependency factory: require the signed-in user to hold one of ``roles`` (and to have already
    changed any forced-reset password). 403 otherwise."""
    allowed = frozenset(roles)

    def _require_role(
        principal: Principal = Depends(require_password_changed),
    ) -> Principal:
        if principal.role not in allowed:
            raise EngineError(
                403, "forbidden", "You do not have permission for this action."
            )
        return principal

    return _require_role


#: Singleton admin guard — reuse this (not a fresh ``require_role('admin')`` per route) so FastAPI
#: resolves it ONCE per request and routers share the cached principal.
require_admin = require_role("admin")


def require_spend_access(
    principal: Principal = Depends(require_password_changed),
) -> Principal:
    """Gate the (sensitive) engine-spend endpoints. admin/reviewer always pass; a viewer passes only
    with the explicit ``can_view_all_spend`` grant. 403 otherwise."""
    if principal.effective_view_all_spend:
        return principal
    raise EngineError(403, "forbidden", "You do not have permission to view spend.")
