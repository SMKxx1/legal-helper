"""Fernet encryption for each user's OpenRouter API key, keyed by ``APP_SECRET_KEY``.

Every user's key is stored encrypted at rest (``users.openrouter_key_enc``) and decrypted in
memory only for the duration of one request (plan §1: no shared server key — keys belong to
users). The Fernet key comes from ``APP_SECRET_KEY`` (a urlsafe-base64, 32-byte key — generate one
with ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``).

* ``APP_ENV=dev`` and no key set -> a fixed, clearly-logged dev-only key is derived so the app
  still boots with zero config. Never used once a real key is set.
* Any other ``APP_ENV`` and no key set -> :func:`resolve_fernet_key` returns ``None``; the
  ``database`` capability's probe (``app.capabilities``) turns that into an ``unhealthy`` state,
  which pulls ``/healthz`` to 503 — a loud, health-checked failure instead of a silent one.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings, settings
from .telemetry import get_logger

log = get_logger("legal_helper.crypto")

#: A fixed, well-known seed so the dev fallback key is the SAME on every boot of the same machine
#: (stable across restarts) — never reached once a real APP_SECRET_KEY is set.
_DEV_FALLBACK_SEED = b"legal-helper-dev-fallback-key-do-not-use-in-prod"


def _derive_dev_key() -> bytes:
    digest = hashlib.sha256(_DEV_FALLBACK_SEED).digest()
    return base64.urlsafe_b64encode(digest)


def coerce_fernet_key(raw: str) -> bytes:
    """Turn whatever someone put in ``APP_SECRET_KEY`` into a usable Fernet key.

    Fernet accepts only 32 url-safe-base64-encoded bytes and raises on anything else. That is a
    fine contract for an operator following the README, but the one-click Railway deploy asks a
    stranger for this value — and a passphrase typed into that box would otherwise crash the app
    on the first key save.

    So: a value that already IS a Fernet key is used verbatim — existing deployments keep
    decrypting exactly what they encrypted — and anything else is stretched into one with SHA-256.
    """
    candidate = raw.encode("utf-8")
    try:
        Fernet(candidate)
    except (ValueError, TypeError):
        return base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())
    return candidate


def resolve_fernet_key(s: Settings | None = None) -> bytes | None:
    """The raw Fernet key bytes to use, or ``None`` if none is configured and ``s.app_env`` is
    not ``dev`` (the caller — here, the ``database`` capability's probe — decides what a missing
    key means; this function never raises for it)."""
    s = s or settings
    if s.app_secret_key.strip():
        return coerce_fernet_key(s.app_secret_key.strip())
    if s.app_env == "dev":
        log.warning(
            "crypto.dev_fallback_key",
            note="APP_SECRET_KEY is unset — using a fixed, INSECURE dev-only Fernet key. "
            "Set APP_SECRET_KEY before deploying.",
        )
        return _derive_dev_key()
    return None


def _fernet() -> Fernet:
    key = resolve_fernet_key()
    if key is None:
        raise RuntimeError(
            "APP_SECRET_KEY is required outside APP_ENV=dev "
            "(the 'database' capability reports unhealthy until it is set)."
        )
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` (a user's OpenRouter API key) to a storable ciphertext string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a value produced by :func:`encrypt`. Raises ``ValueError`` on a bad or foreign
    token so callers fail closed instead of proceeding with garbage."""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("could not decrypt stored value") from exc
