"""Service-account principals for the machine ``/v1`` engine path (P0-12 scaffold).

The ``/v1`` review API is called by machines (the Word add-in, n8n, Slack/email bridges), not by a
logged-in human. Before P0-12 the only gate was a single shared ``ENGINE_API_KEY`` that was
*fail-open when unset*, and a successful call was attributed to nobody. This module closes that:

  * Every presented ``X-API-Key`` resolves to a NAMED :class:`ServicePrincipal`, so each engine
    run is attributable (persisted on ``EngineReview.actor_user_id``) and individually capped.
  * A *configured* engine REJECTS a missing/unknown key with 401 — no fail-open.
  * A *misconfigured* engine (a key var is set but blank/malformed) or a *strict* engine
    (``engine_require_key`` on) with no key fails CLOSED with 503 — it never reverts to open.
  * Only a *genuinely unconfigured* dev box (no key var set, strict off) binds an explicit,
    loudly-logged open ``svc:local`` principal: still attributable + capped, never silently
    anonymous. Production sets ``ENGINE_REQUIRE_KEY=true`` so this path can't be reached by a
    forgotten key — ``/v1`` IS internet-reachable through Caddy (the edge does not authenticate it).
  * A per-principal sliding-window request rate cap (429) protects the engine from a runaway
    caller; the per-principal monthly *cost* cap is enforced in the route (it needs the review
    store) right before the paid engine call.

This is the Phase-0 SCAFFOLD. Phase 1 (P1-3/P1-4) swaps the env-key map for a DB-backed
``ServiceAccountKey`` table (per-key entitlements; Postgres/Redis-backed counters for multi-replica
correctness) behind this same ``resolve_service_principal`` / ``enforce_rate_limit`` seam.
"""

from __future__ import annotations

import hmac
import logging
import threading
from dataclasses import dataclass

from app.api.errors import EngineError
from app.config import settings

log = logging.getLogger("nda.svc")

_DEFAULT_PRINCIPAL = "svc:default"  # the legacy single ENGINE_API_KEY
_LOCAL_PRINCIPAL = (
    "svc:local"  # unconfigured-engine dev principal (open, but attributed)
)
_PRINCIPAL_MAXLEN = 32  # EngineReview.actor_user_id is String(32)


@dataclass(frozen=True)
class ServicePrincipal:
    """A machine caller identity bound to an X-API-Key. ``id`` is persisted on EngineReview."""

    id: str  # e.g. "svc:default", "svc:wordaddin", "svc:local"
    name: str  # human label, e.g. "default", "wordaddin", "local"
    configured: bool  # False only for the unconfigured-dev svc:local principal


def _principal_id(name: str) -> str:
    return f"svc:{name}"


def _raw_config_present() -> bool:
    """True if the operator put SOMETHING in either key var (even whitespace/garbage). Distinguishes
    'present-but-unusable' (a misconfiguration -> fail closed) from a genuinely blank dev box."""
    return bool(getattr(settings, "engine_api_key", "") or "") or bool(
        getattr(settings, "engine_service_keys", "") or ""
    )


# Memoize the parsed key map keyed on the RAW config strings: _key_map runs on every /v1 request,
# so re-parsing (and re-emitting the misconfig warnings) per request is wasteful + log-flooding.
# Auto-invalidates when the config changes (e.g. a test monkeypatches settings), so warnings fire
# once per distinct config rather than once per request.
_KEY_MAP_CACHE: tuple[tuple[str, str], dict[str, str]] | None = None
_KEY_MAP_LOCK = threading.Lock()


def _key_map() -> dict[str, str]:
    """Memoized ``{api_key -> principal_id}`` from config (see ``_build_key_map`` for the parse)."""
    global _KEY_MAP_CACHE
    raw = (
        (getattr(settings, "engine_api_key", "") or ""),
        (getattr(settings, "engine_service_keys", "") or ""),
    )
    cached = _KEY_MAP_CACHE
    if cached is not None and cached[0] == raw:
        return cached[1]
    with _KEY_MAP_LOCK:
        cached = _KEY_MAP_CACHE
        if cached is not None and cached[0] == raw:
            return cached[1]
        out = _build_key_map()
        _KEY_MAP_CACHE = (raw, out)
        return out


def _build_key_map() -> dict[str, str]:
    """``{api_key -> principal_id}`` from config. The legacy ``engine_api_key`` -> ``svc:default``;
    extra ``engine_service_keys`` ``"name:secret,name:secret"`` pairs -> ``svc:<name>``.

    Blank/malformed entries are skipped. A name whose ``svc:<name>`` id would exceed the
    ``actor_user_id`` column width is SKIPPED with a loud warn (never silently truncated — two long
    names sharing a prefix would otherwise collapse to one principal, silently merging their caps +
    audit identity). Two distinct secrets that derive the SAME principal id are also warned (they
    share one rate/cost bucket); the same secret in two slots is last-wins (key rotation)."""
    out: dict[str, str] = {}
    claimed: dict[
        str, str
    ] = {}  # principal_id -> the FIRST secret that claimed it (collision warn)

    def _claim(key: str, pid: str) -> None:
        prior = claimed.get(pid)
        if prior is not None and prior != key:
            log.warning(
                "service-account principal %r is claimed by two DISTINCT keys; they will "
                "share one rate/cost bucket and audit identity — give each its own name.",
                pid,
            )
        else:
            claimed.setdefault(pid, key)
        if key in out and out[key] != pid:
            log.warning(
                "the same service-account secret is mapped to both %r and %r; using %r.",
                out[key],
                pid,
                pid,
            )
        out[key] = pid

    legacy = (getattr(settings, "engine_api_key", "") or "").strip()
    if legacy:
        _claim(legacy, _DEFAULT_PRINCIPAL)
    raw = (getattr(settings, "engine_service_keys", "") or "").strip()
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, key = pair.partition(":")
        name, key = name.strip(), key.strip()
        if not (name and key):
            continue
        pid = _principal_id(name)
        if len(pid) > _PRINCIPAL_MAXLEN:
            log.warning(
                "service-account name %r is too long (%r exceeds the %d-char principal id "
                "limit); SKIPPING this key to avoid a silent attribution collision.",
                name,
                pid,
                _PRINCIPAL_MAXLEN,
            )
            continue
        _claim(key, pid)
    return out


# --- unconfigured-engine warning (emit once, thread-safe) ----------------------------------- #
_warned_unconfigured = False
_warn_lock = threading.Lock()


def _warn_unconfigured_once() -> None:
    global _warned_unconfigured
    if _warned_unconfigured:
        return
    with _warn_lock:
        if not _warned_unconfigured:
            _warned_unconfigured = True
            log.warning(
                "/v1 engine is UNAUTHENTICATED (no ENGINE_API_KEY / ENGINE_SERVICE_KEYS set): "
                "binding the open 'svc:local' dev principal. Configure a key in production — the "
                "engine path must be attributable."
            )


def resolve_service_principal(x_api_key: str | None) -> ServicePrincipal:
    """Resolve the presented ``X-API-Key`` to a :class:`ServicePrincipal`, or fail.

    CONFIGURED engine (>=1 usable key): the key MUST match (constant-time) -> its principal; else 401.
    MISCONFIGURED engine (a key var is PRESENT but resolves to nothing — blank/whitespace/malformed):
        503 — fail CLOSED, never bind an open principal off a config the operator *thought* set a key.
    STRICT, unconfigured (``engine_require_key`` on, no key at all): 503 — production fails closed.
    DEV, genuinely unconfigured (no key, strict off): bind the open ``svc:local`` principal (loud warn).
    """
    keys = _key_map()
    if not keys:
        if _raw_config_present():
            # A key var was set but produced no usable key (e.g. ENGINE_API_KEY="   "): treat as a
            # misconfiguration and refuse to serve, rather than silently reverting to fail-open.
            raise EngineError(
                503,
                "engine_misconfigured",
                "Engine API key is configured but unusable (blank or malformed). "
                "The engine refuses to serve unauthenticated.",
            )
        if bool(getattr(settings, "engine_require_key", False)):
            raise EngineError(
                503,
                "engine_unconfigured",
                "No engine API key is configured and ENGINE_REQUIRE_KEY is set.",
            )
        _warn_unconfigured_once()
        return ServicePrincipal(id=_LOCAL_PRINCIPAL, name="local", configured=False)
    presented = x_api_key or ""
    # Compare against EVERY configured key without early-exit so wall-time is independent of which
    # key (or none) matched; ``compare_digest`` already removes the per-character timing channel.
    matched: str | None = None
    for key, pid in keys.items():
        if hmac.compare_digest(presented, key):
            matched = pid
    if matched is None:
        raise EngineError(401, "unauthorized", "Invalid or missing X-API-Key.")
    name = matched.split(":", 1)[1] if ":" in matched else matched
    return ServicePrincipal(id=matched, name=name, configured=True)


# --- per-principal request rate cap (sliding window) ---------------------------------------- #
# The limiter now lives in ``rate_store`` (PL-6) so a Redis backend can share the window across
# replicas, falling back to this exact in-process limiter when Redis is absent. Re-exported here for
# back-compat (existing imports + tests reference ``service_keys._SlidingWindowLimiter``).
from app.auth import rate_store  # noqa: E402
from app.auth.rate_store import _SlidingWindowLimiter  # noqa: E402,F401  (re-export)


def enforce_rate_limit(principal_id: str) -> None:
    """Raise 429 if ``principal_id`` is over the configured per-key request rate (no-op when 0).
    Routes through the shared rate store (Redis when configured, else in-process)."""
    rate_store.enforce(
        principal_id, int(getattr(settings, "engine_rate_limit_per_min", 0) or 0)
    )


def reset_rate_limiter() -> None:
    """Test hook: drop all rate-limit state + the store-selection cache."""
    rate_store.reset()
