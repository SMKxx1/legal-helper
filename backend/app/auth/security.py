"""Password hashing (argon2id) + token/CSRF helpers for CLM auth (P0-4).

Hashing uses argon2-cffi (argon2id, memory-hard). ``verify_password`` is timing-attack
resistant; an UNKNOWN user is processed through ``dummy_verify`` against a fixed dummy hash so
login wall-time does not reveal whether an account exists (anti-enumeration). ``needs_rehash``
drives rehash-on-login so argon2 parameters can be raised over time without invalidating hashes.
"""

from __future__ import annotations

import hmac
import secrets

from argon2 import PasswordHasher

# Tuned argon2id parameters — a login is interactive, so balance cost against latency. Raising
# these later is safe: check_needs_rehash() flags old hashes for transparent re-hash on next login.
_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)

# A fixed hash of a throwaway secret. The unknown-user path verifies against THIS so its failure
# burns argon2 time comparable to a real verify (defeats user-enumeration via response timing).
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(32))


def hash_password(plaintext: str) -> str:
    """Return an argon2id PHC hash string for ``plaintext``."""
    return _ph.hash(plaintext)


def verify_password(password_hash: str, plaintext: str) -> bool:
    """True iff ``plaintext`` matches ``password_hash``. Fails CLOSED on any error."""
    try:
        return _ph.verify(password_hash, plaintext)
    except Exception:  # noqa: BLE001 — mismatch / malformed hash / any error == not verified
        return False


def dummy_verify(plaintext: str) -> bool:
    """Unknown-user path: run a comparable argon2 verify, always return False (anti-enumeration)."""
    try:
        _ph.verify(_DUMMY_HASH, plaintext)
    except Exception:  # noqa: BLE001 — always mismatches; the point is the constant work
        pass
    return False


def needs_rehash(password_hash: str) -> bool:
    """True if the stored hash uses weaker-than-current parameters (rehash on next login)."""
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001
        return False


def new_token(nbytes: int = 32) -> str:
    """A URL-safe opaque random token (default 256-bit of entropy)."""
    return secrets.token_urlsafe(nbytes)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


# --- CSRF (double-submit cookie) ---------------------------------------------------------- #
CSRF_COOKIE = "csrf"

_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
#: Unauthenticated state-changing endpoints — no prior session cookie exists to abuse, so CSRF
#: double-submit does not apply (login establishes the session; reset uses a body token).
_CSRF_EXEMPT_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/password/reset-request",
        "/api/auth/password/reset-confirm",
    }
)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_token: str, header_token: str) -> bool:
    """True iff the CSRF header echoes the CSRF cookie (constant-time, both non-empty)."""
    return (
        bool(cookie_token)
        and bool(header_token)
        and constant_time_equals(cookie_token, header_token)
    )


def is_csrf_protected(method: str, path: str) -> bool:
    """Whether a request must pass CSRF double-submit. EVERY state-changing COOKIE-authenticated
    /api request is protected — auth, admin, AND the legacy admin plane (settings/templates/reviews/
    test-runs/providers), which performs cookie-authed writes including provider API keys
    (PUT /api/settings). EXEMPT: the unauthenticated login/reset endpoints, and the MACHINE planes
    that authenticate with X-API-Key / HMAC and cannot carry a CSRF token (/v1 engine, the DocuSign
    Connect webhook)."""
    if method.upper() in _CSRF_SAFE_METHODS:
        return False
    if path in _CSRF_EXEMPT_PATHS:
        return False
    # Exempt the machine plane on a PATH BOUNDARY (so a hypothetical "/v1X" can't slip the CSRF gate
    # by prefix alone): exact match or the prefix followed by "/". /v1 is X-API-Key / signed-header
    # authenticated (no cookie, so no CSRF surface). (The old "/api/docusign/connect" exemption was
    # removed with the DocuSign integration — no such route is registered.)
    if path == "/v1" or path.startswith("/v1/"):
        return False
    return path.startswith("/api/")
