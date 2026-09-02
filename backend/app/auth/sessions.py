"""Opaque bearer-token sessions (plan §1: bearer auth, not cookies — no CSRF surface).

A login mints 256 bits of randomness (``security.new_token``); only ``sha256(token)`` is ever
persisted, so a database leak yields no usable tokens. The raw token is handed back to the client
exactly once, in the login response body, and travels back on every later request as
``Authorization: Bearer <token>`` (read by ``auth/deps.py``).

No in-process cache, no session-epoch bumping, no per-org scoping: this is a single-tenant demo
with a handful of concurrent users, so "look the token up in the DB" is fast enough and is the
whole story.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from ..models import Session as SessionRow
from ..models import User
from .security import new_token

#: How long a session stays valid after creation (plan §1: 12h TTL).
SESSION_TTL = timedelta(hours=12)
#: ``last_seen_at`` is only rewritten this often, so the hot auth path isn't a DB write per request.
LAST_SEEN_MIN_INTERVAL = timedelta(minutes=1)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite may hand back a naive datetime for DateTime(timezone=True); treat it as UTC.
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def create_session(db: DbSession, user: User) -> tuple[str, datetime]:
    """Create a session for ``user``. Returns the RAW token (shown to the client exactly once)
    and its expiry. The caller commits."""
    token = new_token(32)
    expires_at = datetime.now(UTC) + SESSION_TTL
    db.add(
        SessionRow(
            user_id=user.id, token_sha256=hash_token(token), expires_at=expires_at
        )
    )
    return token, expires_at


def resolve_user(db: DbSession, token: str) -> User | None:
    """Token -> the signed-in ``User``, or ``None`` if the token is missing/unknown/expired.

    Best-effort touches ``last_seen_at`` (throttled) on the resolved session row; the caller
    commits.
    """
    if not token:
        return None
    row = db.execute(
        select(SessionRow, User)
        .join(User, User.id == SessionRow.user_id)
        .where(SessionRow.token_sha256 == hash_token(token))
    ).first()
    if row is None:
        return None
    session, user = row
    expires_at = _aware(session.expires_at)
    if expires_at is None or datetime.now(UTC) >= expires_at:
        return None

    now = datetime.now(UTC)
    last_seen = _aware(session.last_seen_at)
    if last_seen is None or now - last_seen >= LAST_SEEN_MIN_INTERVAL:
        session.last_seen_at = now
    return user


def revoke(db: DbSession, token: str) -> None:
    """Delete the session row for ``token`` (logout). A no-op if it's already gone. The caller
    commits."""
    if not token:
        return
    db.execute(delete(SessionRow).where(SessionRow.token_sha256 == hash_token(token)))
