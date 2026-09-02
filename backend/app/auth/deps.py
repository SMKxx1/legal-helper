"""FastAPI auth dependencies: ``Authorization: Bearer <token>`` -> the signed-in ``User``.

No cookies, no CSRF (plan §1). Every ``/api/*`` route except ``POST /api/auth/login`` and
``GET /api/status`` depends on :func:`get_current_user`; a missing, malformed, or expired token is
a 401 ``unauthenticated`` (:class:`~app.api.errors.EngineError`, rendered by the app-wide handler).
"""

from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy.orm import Session as DbSession

from ..api.errors import EngineError
from ..db import get_db
from ..models import User
from . import sessions as session_store


def bearer_token(authorization: str | None) -> str:
    """The raw token from an ``Authorization: Bearer <token>`` header, or ``""`` if the header is
    absent or not a bearer scheme."""
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()
    return ""


def get_current_user(
    authorization: str | None = Header(default=None), db: DbSession = Depends(get_db)
) -> User:
    token = bearer_token(authorization)
    user = session_store.resolve_user(db, token) if token else None
    if user is None:
        raise EngineError(401, "unauthenticated", "Sign-in required.")
    db.commit()  # persists any last_seen_at touch from resolve_user
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise EngineError(403, "forbidden", "Admin role required.")
    return user
