"""Auth routes (P0-6): login / logout / me / password change + reset (prefix /api/auth).

Sign-in is USER_ID + PASSWORD. On success a random opaque session token is set as an HttpOnly
``sid`` cookie (only its hash is stored) plus a readable ``csrf`` cookie for double-submit CSRF on
state-changing POSTs. Login is anti-enumeration (unknown user and wrong password return a
BYTE-IDENTICAL generic 401 after a comparable argon2 verify) and rate-limited per user (lockout ->
423). Password change/reset rotate the session (revoke-all + epoch bump). Every auth action writes
an ``AuthAuditEvent``. Password-reset email delivery (PLAN §6) is wired to the ported SMTP sender
(:mod:`app.auth.reset_email`), capability-gated on ``email_out`` and BACKGROUNDED so it never
perturbs the anti-enumeration response (a disabled email plane is a safe no-op).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api.errors import EngineError
from app.auth.deps import SESSION_COOKIE, get_current_user
from app.auth.models import AuthAuditEvent, PasswordResetToken, UserAccount
from app.auth.reset_email import ResetEmailSender, build_reset_link
from app.auth.security import (
    csrf_matches,
    dummy_verify,
    hash_password,
    needs_rehash,
    new_csrf_token,
    new_token,
    verify_password,
)
from app.auth.sessions import (
    DEFAULT_SESSION_TTL,
    Principal,
    create_session,
    hash_token,
    revoke,
    revoke_all_for_user,
    validate,
)
from app.capabilities import EMAIL_OUT, CapabilityState
from app.config import settings
from app.db import get_db

log = logging.getLogger("nda.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

CSRF_COOKIE = "csrf"
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
RESET_TOKEN_TTL = timedelta(hours=1)

_PRUNE_EVERY = (
    500  # full-dict sweep cadence (in inserts), not on the hot allowed/blocked path
)


# --------------------------------------------------------------------------- #
# Per-IP sliding-window throttle (login + reset-request)
# --------------------------------------------------------------------------- #
class _SlidingWindow:
    """In-process sliding-window hit counter keyed by an arbitrary string (here: client IP). Mirrors
    the shape of ``app.auth.rate_store``'s per-principal limiter (deque of monotonic timestamps,
    lock-guarded — FastAPI runs these sync routes in a threadpool) but is deliberately NOT that
    shared module: this throttle is IP-keyed (not principal-keyed), needs a peek-without-consuming
    mode (`blocked`) so a check can run BEFORE the per-user lockout logic without itself burning
    budget, and is scoped to just these two auth routes. See settings.auth_ip_throttle_enabled for
    the single-replica-store rationale.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._since_prune = 0

    def _pruned_locked(self, key: str, cutoff: float) -> deque[float]:
        """Caller must hold ``self._lock``. Returns ``key``'s deque with stale (pre-cutoff) entries
        popped off the front."""
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        while dq and dq[0] <= cutoff:
            dq.popleft()
        return dq

    def _sweep_locked(self, cutoff: float) -> None:
        """Drop keys whose entire window has already expired, opportunistically, so a flood of
        distinct IPs (each hitting once and never returning) can't grow the dict unbounded. Caller
        must hold ``self._lock``."""
        self._since_prune = 0
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]
        for k in stale:
            self._hits.pop(k, None)

    def blocked(
        self, key: str, limit: int, window_s: float, *, now: float | None = None
    ) -> float | None:
        """Peek-only (no insert): ``None`` if ``key`` is currently under ``limit`` hits in the
        trailing ``window_s`` seconds, else the seconds until the oldest hit ages out. Safe to call
        before an attempt is known to "count" (e.g. before the per-user lockout / DB lookup), since
        it never itself consumes budget."""
        if limit <= 0:
            return None  # disabled
        t = time.monotonic() if now is None else now
        cutoff = t - window_s
        with self._lock:
            dq = self._pruned_locked(key, cutoff)
            if len(dq) >= limit:
                return max(0.0, dq[0] + window_s - t)
        return None

    def record(self, key: str, window_s: float, *, now: float | None = None) -> None:
        """Unconditionally consume one slot for ``key`` (e.g. a FAILED login). Never rejects —
        pair with a preceding ``blocked()`` check if the caller must also gate on the limit."""
        t = time.monotonic() if now is None else now
        cutoff = t - window_s
        with self._lock:
            dq = self._pruned_locked(key, cutoff)
            dq.append(t)
            self._since_prune += 1
            if self._since_prune >= _PRUNE_EVERY:
                self._sweep_locked(cutoff)

    def allow(
        self, key: str, limit: int, window_s: float, *, now: float | None = None
    ) -> float | None:
        """Check-and-consume atomically: ``None`` if ``key`` is admitted (and the hit is recorded),
        else the seconds until retry (a rejected call is NOT recorded — retrying immediately after
        does not extend the block). Used where EVERY call should burn budget regardless of outcome
        (e.g. reset-request, which must not leak a success/failure split to an enumerating caller)."""
        if limit <= 0:
            return None  # disabled
        t = time.monotonic() if now is None else now
        cutoff = t - window_s
        with self._lock:
            dq = self._pruned_locked(key, cutoff)
            if len(dq) >= limit:
                return max(0.0, dq[0] + window_s - t)
            dq.append(t)
            self._since_prune += 1
            if self._since_prune >= _PRUNE_EVERY:
                self._sweep_locked(cutoff)
        return None

    def reset(self) -> None:
        """Test hook: drop all counters."""
        with self._lock:
            self._hits.clear()
            self._since_prune = 0


_login_ip_throttle = _SlidingWindow()
_reset_ip_throttle = _SlidingWindow()

#: The reset-email sender (PLAN §6). A module global so a test can swap in one built with a fake
#: transport (``ResetEmailSender(transport_factory=...)``) — mirroring how the conftest resets the
#: throttles above — and capture the outbound message with zero network.
_reset_email_sender = ResetEmailSender()


def _ip_rate_limited(retry_after_s: float, message: str) -> EngineError:
    return EngineError(
        429,
        "ip_rate_limited",
        message,
        {"retry_after_s": round(retry_after_s, 1)},
        headers={"Retry-After": str(max(1, int(retry_after_s)))},
    )


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class ResetRequestIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=200)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if (dt is None or dt.tzinfo is not None) else dt.replace(tzinfo=UTC)


def _client_ip(request: Request) -> str:
    # Reuse the ONE canonical client-IP resolver (app.auth.admin_ip.client_ip): it honours
    # X-Forwarded-For ONLY behind a trusted edge (settings.trust_forwarded_proto), else the direct
    # socket peer — so a directly-reachable API can't be tricked into fresh throttle buckets with a
    # spoofed header (which would defeat the per-IP login/reset rate limit entirely).
    from app.auth.admin_ip import client_ip

    return client_ip(request)


def _is_secure(request: Request) -> bool:
    # Behind Caddy, X-Forwarded-Proto carries the real (https) scheme; over plain http (dev/tests)
    # the Secure flag is off so the cookie is usable. The header is only honoured when a trusted
    # edge is in front (settings.trust_forwarded_proto) — without that, a directly-reachable API
    # over plain http must not let a client-sent header coerce the Secure flag.
    if request.url.scheme == "https":
        return True
    return settings.trust_forwarded_proto and (
        request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


def _set_session_cookies(
    response: Response, request: Request, sid: str, csrf: str
) -> None:
    secure = _is_secure(request)
    max_age = int(DEFAULT_SESSION_TTL.total_seconds())
    # PLAN §6: strict cookies (Secure / HttpOnly / SameSite=Strict). SameSite=Strict (matching the
    # /f form-session cookie) keeps the session + CSRF cookies from ever riding a cross-site request,
    # closing the CSRF surface at the browser before the double-submit check is even reached. Secure
    # is HTTPS-gated via _is_secure so the cookie stays usable over plain http in dev/tests.
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,  # readable by the SPA (double-submit); still Secure + SameSite=Strict
        secure=secure,
        samesite="strict",
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _audit(
    db,
    *,
    action: str,
    actor_principal: str,
    actor_type: str = "user",
    org_id: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuthAuditEvent(
            action=action,
            actor_principal=(actor_principal or "")[:128],
            actor_type=actor_type,
            org_id=org_id,
            target=target,
            detail=detail,
            ip=ip,
        )
    )


def _schedule_reset_email(
    background: BackgroundTasks,
    request: Request,
    *,
    to_email: str | None,
    token: str,
) -> None:
    """Capability-gated scheduling of the reset email (PLAN §6).

    Reads the ``email_out`` capability off ``app.state``: DISABLED/UNHEALTHY (or missing registry) =>
    the ported SAFE NO-OP (the P0 behaviour, minus the token log — a disabled email plane just doesn't
    deliver, the endpoint's 200 body is unchanged). ENABLED => the send is queued as a BACKGROUND task
    so it runs AFTER the response is flushed, adding ZERO latency to the response. That last point is a
    security property, not just an optimisation: only a real, active account reaches this function, so
    an INLINE SMTP send would make a known user measurably slower than an unknown one — a new
    enumeration oracle. Backgrounding keeps the known-vs-unknown wall-time parity the ported
    ``dummy_verify`` already establishes for the DB writes."""
    registry = getattr(request.app.state, "capabilities", None)
    if registry is None or registry.state(EMAIL_OUT) is not CapabilityState.ENABLED:
        log.info("password reset email delivery skipped (email_out capability off)")
        return
    if not to_email:
        # No address on the account: nothing to deliver to, but the response is unchanged.
        log.info("password reset email delivery skipped (no account email)")
        return
    reset_link = build_reset_link(settings, token)
    background.add_task(
        _reset_email_sender.send, settings, to_email=to_email, reset_link=reset_link
    )


def require_csrf(request: Request) -> None:
    """Double-submit CSRF for state-changing authenticated POSTs."""
    if not csrf_matches(
        request.cookies.get(CSRF_COOKIE, ""), request.headers.get("x-csrf-token", "")
    ):
        raise EngineError(403, "csrf_failed", "CSRF token missing or invalid.")


def _is_locked(user: UserAccount, now: datetime) -> bool:
    if user.status == "locked":
        return True
    lu = _aware(user.locked_until)
    return lu is not None and lu > now


_GENERIC_401 = EngineError(401, "invalid_credentials", "Invalid user ID or password.")


def _session_user_dict(
    *,
    user_id: str,
    role: str,
    org_id: str,
    must_change_password: bool,
    team: str | None,
    view_all_docs: bool,
    view_all_spend: bool,
    manage_permissions: bool,
) -> dict:
    """The signed-in-user payload returned by /login and /me. ``permissions`` are EFFECTIVE (the
    broad grants are implicit for admin/reviewer) so the SPA can gate on them directly without
    re-deriving role rules; the admin Users API returns the RAW per-user flags for editing."""
    broad = role in ("admin", "reviewer")
    return {
        "user_id": user_id,
        "role": role,
        "org_id": org_id,
        "must_change_password": bool(must_change_password),
        "team": team,
        "permissions": {
            "view_all_docs": broad or bool(view_all_docs),
            "view_all_spend": broad or bool(view_all_spend),
            "manage_permissions": role == "admin" or bool(manage_permissions),
        },
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post("/login")
def login(
    body: LoginIn, request: Request, response: Response, db=Depends(get_db)
) -> dict:
    now = _now()
    ip = _client_ip(request)

    # Per-IP throttle runs BEFORE the user lookup / per-user lockout, so an IP already over its
    # window never touches the DB or the per-user failed_login_count — an attacker spraying guesses
    # across many user_ids from one IP (credential stuffing) is capped here, and this also means the
    # per-user lockout can no longer be weaponized as a DoS via one IP alone (see auth_ip_* settings).
    if settings.auth_ip_throttle_enabled:
        retry = _login_ip_throttle.blocked(
            ip, settings.auth_ip_max_attempts, settings.auth_ip_window_s
        )
        if retry is not None:
            _audit(
                db,
                action="login_ip_throttled",
                actor_principal=body.user_id,
                ip=ip,
            )
            db.commit()
            raise _ip_rate_limited(
                retry, "Too many login attempts from this address. Try again later."
            )

    user = db.execute(
        select(UserAccount).where(UserAccount.user_id == body.user_id)
    ).scalar_one_or_none()

    # A locked account is rejected with 423 BEFORE verifying (so it can't keep incrementing). The
    # 423 does reveal the account EXISTS — but only to an attacker who has ALREADY driven that exact
    # user_id to the lockout threshold, so it is no oracle beyond what they already know. The
    # unknown-user vs wrong-password paths below stay byte-identical (both -> the generic 401).
    if user is not None and _is_locked(user, now):
        _audit(
            db,
            action="login_locked",
            actor_principal=body.user_id,
            org_id=user.org_id,
            ip=ip,
        )
        db.commit()
        raise EngineError(
            423, "account_locked", "Account temporarily locked. Try again later."
        )

    # A prior lock that has since EXPIRED (we're past the check above, so the account is NOT currently
    # locked): reset the counter so a single post-window miss doesn't instantly re-lock at the stale count.
    if user is not None and user.locked_until is not None:
        user.failed_login_count = 0
        user.locked_until = None

    # Anti-enumeration: always run a comparable argon2 verify (real for a known active user; dummy
    # otherwise) so an unknown user and a wrong password are indistinguishable by timing or body.
    if user is not None and user.status == "active":
        ok = verify_password(user.password_hash, body.password)
    else:
        dummy_verify(body.password)
        ok = False

    if not ok:
        # Only a FAILED attempt burns per-IP budget (a successful login never does — see
        # auth_ip_max_attempts) — recorded for BOTH the known-user and unknown-user branches below,
        # same as the per-user counter only applying to the known-user branch.
        if settings.auth_ip_throttle_enabled:
            _login_ip_throttle.record(ip, settings.auth_ip_window_s)
        if user is not None:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            locked = user.failed_login_count >= MAX_FAILED_LOGINS
            if locked:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            _audit(
                db,
                action="login_failed",
                actor_principal=body.user_id,
                org_id=user.org_id,
                ip=ip,
                detail=f"fails={user.failed_login_count}",
            )
            db.commit()
            if locked:
                raise EngineError(
                    423,
                    "account_locked",
                    "Account temporarily locked. Try again later.",
                )
        else:
            _audit(
                db,
                action="login_failed",
                actor_principal=body.user_id,
                ip=ip,
                detail="unknown_user",
            )
            db.commit()
        raise _GENERIC_401

    # Success.
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)  # transparent rehash-on-login
    sid = create_session(db, user, user_agent=request.headers.get("user-agent"), ip=ip)
    csrf = new_csrf_token()
    _audit(db, action="login", actor_principal=user.user_id, org_id=user.org_id, ip=ip)
    db.commit()
    _set_session_cookies(response, request, sid, csrf)
    return _session_user_dict(
        user_id=user.user_id,
        role=user.role,
        org_id=user.org_id,
        must_change_password=bool(user.must_change_password),
        team=user.team,
        view_all_docs=user.can_view_all_docs,
        view_all_spend=user.can_view_all_spend,
        manage_permissions=user.can_manage_permissions,
    )


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request, response: Response, db=Depends(get_db)) -> dict:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        # Resolve who is logging out BEFORE revoking, so the audit row is attributable (not blank).
        principal = validate(db, sid)
        revoke(db, sid)
        _audit(
            db,
            action="logout",
            actor_principal=principal.user_id if principal else "",
            org_id=principal.org_id if principal else None,
            ip=_client_ip(request),
        )
        db.commit()
    _clear_session_cookies(response)
    return {"ok": True}


@router.get("/me")
def me(principal: Principal = Depends(get_current_user)) -> dict:
    return _session_user_dict(
        user_id=principal.user_id,
        role=principal.role,
        org_id=principal.org_id,
        must_change_password=principal.must_change_password,
        team=principal.team,
        view_all_docs=principal.can_view_all_docs,
        view_all_spend=principal.can_view_all_spend,
        manage_permissions=principal.can_manage_permissions,
    )


@router.post("/password/change", dependencies=[Depends(require_csrf)])
def change_password(
    body: PasswordChangeIn,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    user = db.get(UserAccount, principal.user_pk)
    if user is None or not verify_password(user.password_hash, body.old_password):
        raise EngineError(400, "invalid_old_password", "Current password is incorrect.")
    now = _now()
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.password_changed_at = now
    revoke_all_for_user(db, user)  # epoch bump + revoke ALL sessions (incl. this one)
    sid = create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )  # rotate: issue a fresh session under the new epoch
    csrf = new_csrf_token()
    _audit(
        db,
        action="password_change",
        actor_principal=user.user_id,
        org_id=user.org_id,
        ip=_client_ip(request),
    )
    db.commit()
    _set_session_cookies(response, request, sid, csrf)
    return {"ok": True}


@router.post("/password/reset-request")
def reset_request(
    body: ResetRequestIn,
    request: Request,
    background: BackgroundTasks,
    db=Depends(get_db),
) -> dict:
    """Create a reset token for an existing active account. The response BODY is IDENTICAL regardless
    of whether the account exists, and a constant argon2 dominator (below) is burned on BOTH paths so
    the extra DB writes for a real account don't make it measurably slower (best-effort anti-
    enumeration, mirroring login; the residual sub-dominator delta is below network jitter). Email
    delivery (PLAN §6) is capability-gated and BACKGROUNDED so it never perturbs the response
    (body/status/timing) — see ``_schedule_reset_email``."""
    ip = _client_ip(request)
    # EVERY call counts here (unlike login, which only counts failures) — a per-user_id split would
    # itself be a new enumeration oracle (existing vs. unknown accounts throttling differently), and
    # this check runs BEFORE any user lookup so it can't touch that oracle at all.
    if settings.auth_ip_throttle_enabled:
        retry = _reset_ip_throttle.allow(
            ip, settings.auth_reset_ip_max, settings.auth_ip_window_s
        )
        if retry is not None:
            raise _ip_rate_limited(
                retry, "Too many reset requests from this address. Try again later."
            )
    user = db.execute(
        select(UserAccount).where(UserAccount.user_id == body.user_id)
    ).scalar_one_or_none()
    # Same fixed argon2 cost on existing AND unknown user_ids — it dominates the wall time so the
    # token-insert/audit/commit done only for a real account is statistically masked (no timing oracle).
    dummy_verify(body.user_id or "")
    if user is not None and user.status == "active":
        token = new_token(32)
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=_now() + RESET_TOKEN_TTL,
            )
        )
        _audit(
            db,
            action="password_reset_request",
            actor_principal=user.user_id,
            org_id=user.org_id,
            ip=_client_ip(request),
        )
        db.commit()
        # Real delivery (PLAN §6), capability-gated + backgrounded so it never changes the response
        # (body/status/timing) and email_out-off is a safe no-op. Nothing user-controlled is emailed;
        # only the server-minted token travels (in the reset link).
        _schedule_reset_email(background, request, to_email=user.email, token=token)
    return {
        "ok": True,
        "message": "If that account exists, a password reset has been initiated.",
    }


@router.post("/password/reset-confirm")
def reset_confirm(body: ResetConfirmIn, request: Request, db=Depends(get_db)) -> dict:
    rt = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(body.token)
        )
    ).scalar_one_or_none()
    now = _now()
    exp = None if rt is None else _aware(rt.expires_at)
    if rt is None or rt.used_at is not None or exp is None or exp < now:
        raise EngineError(400, "invalid_token", "Invalid or expired reset token.")
    user = db.get(UserAccount, rt.user_id)
    if user is None:
        raise EngineError(400, "invalid_token", "Invalid or expired reset token.")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.password_changed_at = now
    # A completed self-service reset also clears any brute-force lockout (mirrors the admin reset +
    # login-success paths): otherwise a user who locked themselves out by forgetting their password —
    # the most common lockout trigger — would still be 423'd for up to LOCKOUT_MINUTES right after
    # successfully resetting it.
    user.locked_until = None
    user.failed_login_count = 0
    # Consume EVERY outstanding reset token for this user (not just the presented one) — after a
    # successful reset, any previously-issued token must no longer be usable.
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)
        )
        .values(used_at=now)
    )
    revoke_all_for_user(db, user)  # all existing sessions are invalidated
    _audit(
        db,
        action="password_reset_confirm",
        actor_principal=user.user_id,
        org_id=user.org_id,
        ip=_client_ip(request),
    )
    db.commit()
    return {"ok": True}
