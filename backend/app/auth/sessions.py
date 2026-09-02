"""Opaque server-side sessions with an in-process validation cache (P0-4).

The cookie carries a random 256-bit token; only ``sha256(token)`` is stored, so a DB leak yields
no usable cookies. ``validate`` serves a short-TTL in-process cache so the hot auth path does not
hit the DB on every request (important on single-worker SQLite/WAL). Invalidation is IMMEDIATE:
``revoke`` evicts the token's cache entry and sets ``revoked_at``; ``revoke_all_for_user`` bumps the
user's ``session_epoch`` (so every outstanding session's snapshot stops matching) and flushes the
cache. Eviction runs both immediately AND after the caller's commit (``_evict_after_commit``), so a
concurrent request can't re-cache a still-committed pre-revoke snapshot during the commit window.
The cache TTL only bounds staleness if eviction can't reach another process (multi-worker later);
within one process, revocation is effective immediately.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, select, update

from .models import IdentitySession, UserAccount
from .security import new_token

DEFAULT_SESSION_TTL = timedelta(hours=12)
#: How long a validated session may be served from cache before the DB is re-checked.
CACHE_TTL_SECONDS = 30.0


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite may hand back naive datetimes for DateTime(timezone=True); treat them as UTC.
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """The resolved identity behind a valid session cookie (everything the auth deps need, so the
    hot path stays DB-free on a cache hit)."""

    user_pk: str  # UserAccount.id
    user_id: str  # login handle
    org_id: str
    role: str
    session_id: str
    expires_at: datetime
    must_change_password: bool = False
    team: str | None = None
    # Granular grants (admin/reviewer hold the broad ones implicitly — see effective_* helpers).
    can_view_all_docs: bool = False
    can_view_all_spend: bool = False
    can_manage_permissions: bool = False

    @property
    def effective_view_all_docs(self) -> bool:
        return self.role in ("admin", "reviewer") or self.can_view_all_docs

    @property
    def effective_view_all_spend(self) -> bool:
        return self.role in ("admin", "reviewer") or self.can_view_all_spend


class _SessionCache:
    """token_hash -> (Principal, cached_at_monotonic). Thread-safe; bounded by an explicit TTL."""

    def __init__(self) -> None:
        self._d: dict[str, tuple[Principal, float]] = {}
        self._lock = threading.Lock()

    def get(self, token_hash: str) -> Principal | None:
        with self._lock:
            v = self._d.get(token_hash)
            if v is None:
                return None
            principal, cached_at = v
            if (time.monotonic() - cached_at) > CACHE_TTL_SECONDS:
                self._d.pop(token_hash, None)
                return None
            return principal

    def put(self, token_hash: str, principal: Principal) -> None:
        with self._lock:
            self._d[token_hash] = (principal, time.monotonic())

    def evict(self, token_hash: str) -> None:
        with self._lock:
            self._d.pop(token_hash, None)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


_cache = _SessionCache()


def _evict_after_commit(db, fn) -> None:
    """Run ``fn`` once, AFTER this Session's next commit.

    Revocation evicts the cache so a stale Principal isn't served — but eviction must take effect
    relative to COMMITTED state. If we only evict BEFORE the caller commits, a concurrent request for
    the same token (on its own DB session, seeing only committed state) re-reads the still-un-revoked
    row, passes validation, and RE-POPULATES the cache, which then survives the full cache TTL.
    Re-evicting after commit closes that race: anything re-cached during the window is dropped, and
    after commit the row reads revoked so it can never be re-cached. Idempotent (fires at most once);
    the listener rides the short-lived per-request Session and is GC'd with it.

    A vanishingly small residual remains in a single process — a reader preempted AFTER this
    after-commit clear but BEFORE it writes its (pre-commit-read) Principal could re-cache it for one
    TTL. It is far smaller than the original window and self-heals in <=CACHE_TTL_SECONDS; the
    Phase-1 shared (Redis) session store removes it entirely.
    """
    fired = {"done": False}

    def _cb(_session) -> None:
        if not fired["done"]:
            fired["done"] = True
            fn()

    event.listen(db, "after_commit", _cb)


def create_session(
    db,
    user: UserAccount,
    *,
    ttl: timedelta = DEFAULT_SESSION_TTL,
    user_agent: str | None = None,
    ip: str | None = None,
) -> str:
    """Create a session for ``user`` and return the OPAQUE token (only its hash is persisted).

    The session snapshots the user's current ``session_epoch`` so a later ``revoke_all_for_user``
    (epoch bump) invalidates it. The caller commits.
    """
    token = new_token(32)
    sess = IdentitySession(
        user_id=user.id,
        token_hash=hash_token(token),
        revocation_epoch=user.session_epoch or 0,
        expires_at=_now() + ttl,
        user_agent=(user_agent or "")[:512] or None,
        ip=(ip or "")[:64] or None,
    )
    db.add(sess)
    db.flush()
    return token


def validate(db, token: str) -> Principal | None:
    """Resolve a cookie token to a Principal, or None. The 2nd+ call within CACHE_TTL_SECONDS is
    served from the in-process cache WITHOUT touching ``db``."""
    if not token:
        return None
    th = hash_token(token)

    cached = _cache.get(th)
    if cached is not None:
        if _now() < cached.expires_at:  # honor expiry even on a cache hit
            return cached
        _cache.evict(th)
        return None

    row = db.execute(
        select(IdentitySession, UserAccount)
        .join(UserAccount, UserAccount.id == IdentitySession.user_id)
        .where(IdentitySession.token_hash == th)
    ).first()
    if row is None:
        return None
    sess, user = row
    expires_at = _aware(sess.expires_at)
    if (
        sess.revoked_at is not None
        or expires_at is None
        or _now() >= expires_at
        or user.status != "active"
        or (sess.revocation_epoch or 0) != (user.session_epoch or 0)
    ):
        return None

    principal = Principal(
        user_pk=user.id,
        user_id=user.user_id,
        org_id=user.org_id,
        role=user.role,
        session_id=sess.id,
        expires_at=expires_at,
        must_change_password=bool(user.must_change_password),
        team=user.team,
        can_view_all_docs=bool(user.can_view_all_docs),
        can_view_all_spend=bool(user.can_view_all_spend),
        can_manage_permissions=bool(user.can_manage_permissions),
    )
    _cache.put(th, principal)
    return principal


def touch_last_seen(db, token: str) -> None:
    """Best-effort ``last_seen_at`` bump (the caller throttles how often this is called)."""
    db.execute(
        update(IdentitySession)
        .where(IdentitySession.token_hash == hash_token(token))
        .values(last_seen_at=_now())
    )


def revoke(db, token: str) -> None:
    """Revoke a SINGLE session (logout). Evicts the cache now AND after the caller commits (the
    after-commit evict closes the re-population race — see ``_evict_after_commit``)."""
    th = hash_token(token)
    db.execute(
        update(IdentitySession)
        .where(IdentitySession.token_hash == th)
        .values(revoked_at=_now())
    )
    _cache.evict(th)
    _evict_after_commit(db, lambda: _cache.evict(th))


def revoke_all_for_user(db, user: UserAccount) -> None:
    """Revoke EVERY session for ``user`` (password change / admin disable).

    Bumps ``session_epoch`` (outstanding snapshots stop matching), marks the rows revoked, and
    flushes the cache so nothing stale is served — now AND after the caller commits, so a concurrent
    request cannot re-cache a still-committed pre-revoke snapshot inside the window. Caller commits.
    """
    user.session_epoch = (user.session_epoch or 0) + 1
    db.execute(
        update(IdentitySession)
        .where(IdentitySession.user_id == user.id, IdentitySession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    db.flush()
    _cache.clear()
    _evict_after_commit(db, _cache.clear)
