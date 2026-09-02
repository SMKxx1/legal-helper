"""``POST /api/auth/login`` and ``POST /api/auth/logout`` — bearer-token session auth (plan §4.1).

Login is throttled per client IP (20 failures / 5 minutes, in-process sliding window) and
constant-time for an unknown username (``dummy_verify`` burns comparable argon2 work so response
timing does not reveal whether an account exists). The session token is returned exactly once, in
the JSON body — nothing is ever set as a cookie (plan §1: bearer, not cookies).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..auth import sessions as session_store
from ..auth.deps import bearer_token
from ..auth.security import dummy_verify, verify_password
from ..db import get_db
from ..models import User
from .errors import EngineError

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: In-process sliding window: an IP past this many failures in the window is throttled outright
#: (no password check at all — the point is to stop guessing, not to keep timing constant here).
_FAIL_LIMIT = 20
_FAIL_WINDOW_S = 300.0
_fail_lock = threading.Lock()
_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    with _fail_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW_S]
        _fails[ip] = recent
        return len(recent) >= _FAIL_LIMIT


def _record_failure(ip: str) -> None:
    with _fail_lock:
        _fails.setdefault(ip, []).append(time.monotonic())


def reset_throttle() -> None:
    """Test-only escape hatch — the throttle state is module-global and otherwise outlives a
    single test."""
    with _fail_lock:
        _fails.clear()


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    username: str
    display_name: str
    role: str
    has_key: bool
    key_last4: str | None
    key_label: str | None


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


def user_out(user: User) -> UserOut:
    return UserOut(
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        has_key=bool(user.openrouter_key_enc),
        key_last4=user.openrouter_key_last4,
        key_label=user.openrouter_key_label,
    )


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest, request: Request, db: DbSession = Depends(get_db)
) -> LoginResponse:
    ip = _client_ip(request)
    if _throttled(ip):
        raise EngineError(
            429,
            "too_many_attempts",
            "Too many failed sign-ins. Try again in a few minutes.",
        )

    user = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if user is None:
        dummy_verify(
            body.password
        )  # burns comparable argon2 work — no enumeration by timing
        _record_failure(ip)
        raise EngineError(401, "invalid_credentials", "Invalid username or password.")
    if not verify_password(user.password_hash, body.password):
        _record_failure(ip)
        raise EngineError(401, "invalid_credentials", "Invalid username or password.")

    token, expires_at = session_store.create_session(db, user)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    return LoginResponse(token=token, expires_at=expires_at, user=user_out(user))


@router.post("/logout", status_code=204)
def logout(
    authorization: str | None = Header(default=None), db: DbSession = Depends(get_db)
) -> None:
    token = bearer_token(authorization)
    user = session_store.resolve_user(db, token) if token else None
    if user is None:
        raise EngineError(401, "unauthenticated", "Sign-in required.")
    session_store.revoke(db, token)
    db.commit()
