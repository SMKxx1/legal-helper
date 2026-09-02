"""``POST /api/auth/login`` and ``POST /api/auth/logout`` — bearer-token session auth (plan §4.1).

Login is throttled per client IP (20 failures / 5 minutes, in-process sliding window) and
constant-time for an unknown username (``dummy_verify`` burns comparable argon2 work so response
timing does not reveal whether an account exists). The session token is returned exactly once, in
the JSON body — nothing is ever set as a cookie (plan §1: bearer, not cookies).
"""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .. import crypto
from ..auth import sessions as session_store
from ..auth.deps import bearer_token
from ..auth.security import dummy_verify, hash_password, verify_password
from ..config import settings
from ..db import get_db
from ..models import User
from .errors import EngineError
from .routes_me import validate_openrouter_key

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


#: Sign-up is deliberately cheaper to rate-limit than to CAPTCHA: every registration must present
#: an OpenRouter key that OpenRouter itself accepts, so a bot needs a real funded account per
#: attempt. The per-IP cap below is the backstop against someone scripting that anyway.
_SIGNUP_LIMIT = 10
_SIGNUP_WINDOW_S = 3600.0
_signups: dict[str, list[float]] = {}

_USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30}[a-z0-9])$")
_MIN_PASSWORD_LEN = 8


def _signup_throttled(ip: str) -> bool:
    now = time.monotonic()
    with _fail_lock:
        recent = [t for t in _signups.get(ip, []) if now - t < _SIGNUP_WINDOW_S]
        _signups[ip] = recent
        return len(recent) >= _SIGNUP_LIMIT


def _record_signup(ip: str) -> None:
    with _fail_lock:
        _signups.setdefault(ip, []).append(time.monotonic())


def reset_signup_throttle() -> None:
    """Test-only: the sign-up window is module-global and outlives a single test."""
    with _fail_lock:
        _signups.clear()


class RegisterRequest(BaseModel):
    username: str
    password: str
    api_key: str
    display_name: str | None = None


@router.post("/register", response_model=LoginResponse, status_code=201)
async def register(
    body: RegisterRequest, request: Request, db: DbSession = Depends(get_db)
) -> LoginResponse:
    """Create an account and sign it in, in one step (plan §4.1 extended for self-service).

    The OpenRouter key is validated against OpenRouter BEFORE the row is written, so an account
    never exists in a state where its first review is guaranteed to fail. The key is stored
    Fernet-encrypted exactly like the settings-screen path.
    """
    if not settings.signup_enabled:
        raise EngineError(403, "signup_disabled", "Sign-up is closed on this server.")

    ip = _client_ip(request)
    if _signup_throttled(ip):
        raise EngineError(
            429, "too_many_signups", "Too many accounts created. Try again later."
        )

    username = body.username.strip().lower()
    if not _USERNAME_RE.fullmatch(username):
        raise EngineError(
            422,
            "invalid_username",
            "Use 3-32 characters: lowercase letters, digits, dot, dash or underscore.",
        )
    if len(body.password) < _MIN_PASSWORD_LEN:
        raise EngineError(
            422,
            "weak_password",
            f"Use at least {_MIN_PASSWORD_LEN} characters.",
        )

    taken = db.execute(
        select(User.id).where(User.username == username)
    ).scalar_one_or_none()
    if taken is not None:
        raise EngineError(409, "username_taken", "That username is already registered.")

    # Validate the key before creating anything — raises 422 if OpenRouter rejects it.
    api_key = body.api_key.strip()
    label, _limit_remaining = await validate_openrouter_key(api_key)

    user = User(
        username=username,
        display_name=(body.display_name or "").strip() or username,
        role="user",
        password_hash=hash_password(body.password),
        openrouter_key_enc=crypto.encrypt(api_key),
        openrouter_key_last4=api_key[-4:],
        openrouter_key_label=label,
    )
    db.add(user)
    try:
        db.commit()
    except (
        IntegrityError
    ):  # lost a race against a concurrent signup on the same username
        db.rollback()
        raise EngineError(
            409, "username_taken", "That username is already registered."
        ) from None

    _record_signup(ip)
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
