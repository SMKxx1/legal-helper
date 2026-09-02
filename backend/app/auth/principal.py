"""Unified engine-principal resolution + the HARD engine gate (P1-1).

Closes the red-team BLOCKER: /v1 is no longer fail-open. ``resolve_principal`` resolves a caller in
STRICT order and DENIES (403) if nothing resolves:

  1. WEB — a valid IdentitySession cookie -> a UserAccount principal (entitlements from its role).
  2. SERVICE — a DB ServiceAccountKey (P1-4) bound to the X-API-Key; else the legacy env key
     (back-compat, full engine entitlements); else (genuinely unconfigured dev) the open svc:local.
  3. Otherwise -> 403 not_entitled.

The route then asks ``require_engine_entitlement(principal, action)`` for the SPECIFIC action (which
depends on the review mode) and enforces the per-key rate + monthly cost caps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Cookie, Depends, Header, Request

from app.api.errors import EngineError
from app.auth import service_account, sessions
from app.auth.entitlement import (
    NotEntitled,
    actions_for_role,
    require_engine_entitlement,
)
from app.auth.service_keys import resolve_service_principal
from app.config import settings
from app.db import get_db
from app.schemas import DEFAULT_ORG_ID, EngineAction


@dataclass(frozen=True)
class ResolvedPrincipal:
    principal_type: str  # "user" | "service"
    principal_id: str  # <=32, persisted on EngineReview.actor_user_id
    org_id: str
    entitlements: frozenset[str] = field(default_factory=frozenset)
    role: str | None = None  # web role (None for machine principals)
    key_id: str | None = None  # service_account_keys.id (cost-cap accounting)
    rate_per_min: int | None = None
    monthly_cost_cap_usd: float | None = None
    workspace_id: str | None = None
    signed_action: str | None = (
        None  # the action the X-Principal-* signature was issued for
    )


def _resolve_service(db, x_api_key: str) -> ResolvedPrincipal:
    """DB ServiceAccountKey -> legacy env key -> (unconfigured dev) svc:local. Raises 401/503 only if
    the engine is configured and the key is wrong/blank/strict-unconfigured."""
    auth = service_account.resolve_service_key(db, x_api_key)
    if auth is not None:
        return ResolvedPrincipal(
            principal_type="service",
            principal_id=auth.principal_id,
            org_id=auth.org_id,
            entitlements=frozenset(auth.entitlements),
            key_id=auth.key_id,
            rate_per_min=auth.rate_per_min,
            monthly_cost_cap_usd=auth.monthly_cost_cap_usd,
        )
    # When there are no ENV keys, the engine may still be configured via the DB key table. If so, a
    # presented-but-unmatched key OR a KEYLESS request must 401 — never fall through to the open
    # svc:local dev principal (fail-CLOSED). Only a GENUINELY unconfigured engine (no env keys AND no
    # active DB keys) binds svc:local for local dev. (Previously a KEYLESS request in a DB-keys-only
    # deployment fell through to svc:local with full entitlements — a fail-open.)
    if not (settings.engine_api_key or settings.engine_service_keys) and (
        x_api_key or service_account.any_active_service_key(db)
    ):
        raise EngineError(401, "unauthorized", "Invalid or missing X-API-Key.")
    # Else defer to the legacy env-key resolver (env match / unconfigured-dev svc:local / strict 503).
    sp = resolve_service_principal(x_api_key)
    return ResolvedPrincipal(
        principal_type="service",
        principal_id=sp.id[:32],
        org_id=DEFAULT_ORG_ID,
        entitlements=frozenset(a.value for a in EngineAction),
    )


def resolve_principal(
    request: Request, db, *, x_api_key: str | None = None, sid: str | None = None
) -> ResolvedPrincipal:
    """Resolve the engine principal (web/service) or raise (403/401). DENY-IF-UNRESOLVED."""
    # 1. WEB session cookie
    if sid:
        p = sessions.validate(db, sid)
        if p is not None:
            if getattr(p, "must_change_password", False):
                raise EngineError(
                    403,
                    "password_change_required",
                    "Change your password before using the engine.",
                )
            return ResolvedPrincipal(
                principal_type="user",
                principal_id=p.user_pk,
                org_id=p.org_id,
                entitlements=frozenset(actions_for_role(p.role)),
                role=p.role,
            )
    # 2. SERVICE-ACCOUNT key (DB -> legacy env -> svc:local)
    if x_api_key or settings.engine_api_key or settings.engine_service_keys:
        return _resolve_service(db, x_api_key or "")
    # An unconfigured dev engine still binds svc:local (so local dev/tests serve); strict prod 503s
    # inside resolve_service_principal.
    return _resolve_service(db, "")


def _key_auth(p: ResolvedPrincipal) -> service_account.ServiceKeyAuth:
    return service_account.ServiceKeyAuth(
        key_id=p.key_id or "",
        principal_id=p.principal_id,
        org_id=p.org_id,
        entitlements=p.entitlements,
        rate_per_min=p.rate_per_min,
        monthly_cost_cap_usd=p.monthly_cost_cap_usd,
    )


def engine_principal(
    request: Request,
    x_api_key: str | None = Header(default=None),
    sid: str | None = Cookie(default=None),
    db=Depends(get_db),
) -> ResolvedPrincipal:
    """FastAPI dependency for the /v1 engine routes (the HARD gate): resolve the principal (or deny)
    and enforce the per-key REQUEST rate cap. The per-action entitlement + monthly COST cap are
    checked in the route once the review mode/cost are known (``require_engine_action`` /
    ``over_cost_cap`` / ``record_engine_cost``)."""
    p = resolve_principal(request, db, x_api_key=x_api_key, sid=sid)
    if p.principal_type == "service" and p.key_id:
        service_account.enforce_rate(_key_auth(p))  # per-key rate cap (P1-4)
    else:
        # Legacy env service keys AND web principals share the global request-rate cap. (The
        # per-action entitlement + monthly cost cap are still checked in the route.)
        from app.auth.service_keys import enforce_rate_limit

        enforce_rate_limit(p.principal_id)  # global rate cap (P0-12)
    return p


def require_engine_action(principal: ResolvedPrincipal, mode: str) -> str:
    """Authorize a review in ``mode`` for ``principal`` and return the resolved action. Raises
    NotEntitled (403) if not entitled; for a SIGNED principal the request mode's action MUST equal
    the signed action (a quick-signed header cannot authorize a deep run)."""
    from app.auth.entitlement import action_for_mode

    action = action_for_mode(mode)
    try:
        require_engine_entitlement(principal, action)
    except NotEntitled as e:
        raise EngineError(403, "not_entitled", e.message, e.details) from e
    if principal.signed_action is not None and principal.signed_action != action:
        raise EngineError(
            403,
            "action_mismatch",
            "Signed principal action does not match the requested review mode.",
        )
    return action


def over_cost_cap(db, principal: ResolvedPrincipal) -> bool:
    """Pre-flight: has a service-account key reached its monthly cost cap? (web -> False)."""
    return bool(principal.key_id) and service_account.over_monthly_cap(
        db, _key_auth(principal)
    )


def record_engine_cost(db, principal: ResolvedPrincipal, cost_usd: float) -> None:
    """Accrue a paid run's cost against a service-account key's monthly counter (no-op otherwise)."""
    if principal.key_id:
        service_account.record_usage(db, principal.key_id, cost_usd)
