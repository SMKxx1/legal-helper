"""Engine entitlement resolution.

Phase 0.5 baseline: a signed-in web ``Principal``'s ROLE maps to the engine actions it may perform.
Phase 1 extends entitlement resolution with per-service-account-key entitlements; the
``require_engine_entitlement`` choke point stays the same so callers do not change.

``viewer`` is read-only — it can browse the dashboard but never spend engine budget.
"""

from __future__ import annotations

import json

from app.schemas import EngineAction, UserRole

_ENGINE = {
    EngineAction.review_quick.value,
    EngineAction.review_deep.value,
    EngineAction.redline.value,
}

#: Valid engine action keys — anything outside this set in an entitlements_json is ignored (the
#: minting ceiling: an allow-list/key entitlement can never grant a non-engine power like admin).
ENGINE_ACTIONS = frozenset(_ENGINE)

#: Valid SERVICE-KEY scopes (P1-4 CRUD): the engine actions plus the support_task de-facto scopes.
#: The support_task routes gate on "holds >= 1 entitlement" (not a named action), so a key carrying
#: ONLY e.g. "support.bot" can drive the bot DAL plane while review.quick/deep/redline stay denied —
#: the minimal footprint for an integration that never runs reviews. Unlike the signed-principal
#: path (parse_entitlements), resolve_service_key stores/reads entitlements verbatim; the CRUD
#: validates against THIS set at write time so typos can't silently mint an inert scope.
SERVICE_KEY_SCOPES = frozenset(_ENGINE | {"support.bot", "support.generate"})

#: Web role -> the engine actions it is entitled to.
_ROLE_ACTIONS: dict[str, set[str]] = {
    UserRole.admin.value: set(_ENGINE),
    UserRole.reviewer.value: set(_ENGINE),
    UserRole.viewer.value: set(),
}


class NotEntitled(Exception):
    """The principal may not perform the requested engine action. Routers map this to a 403."""

    code = "not_entitled"
    http_status = 403

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def action_for_mode(mode: str) -> str:
    """The engine action a review mode requires. Three tiers (quick|deep|max): `quick` needs the
    cheap review entitlement; `deep` and `max` both require the deep entitlement (max ⊇ deep)."""
    return (
        EngineAction.review_quick.value
        if mode == "quick"
        else EngineAction.review_deep.value
    )


def actions_for_role(role: str) -> set[str]:
    return set(_ROLE_ACTIONS.get(role, set()))


def role_entitled(role: str, action: str) -> bool:
    return action in actions_for_role(role)


def require_engine_entitlement(principal, action: str) -> None:
    """Raise :class:`NotEntitled` (403) if ``principal`` may not perform ``action``.

    Accepts the web ``Principal`` (a ``role`` attribute) OR a unified principal exposing an
    ``entitlements`` set (the Phase-1 ``app.auth.principal.ResolvedPrincipal``). The entitlements set
    takes precedence when present.
    """
    ents = getattr(principal, "entitlements", None)
    if ents is not None:
        if action not in ents:
            raise NotEntitled(
                f"principal is not entitled to {action}", {"action": action}
            )
        return
    role = getattr(principal, "role", "") or ""
    if not role_entitled(role, action):
        raise NotEntitled(
            f"role {role!r} is not entitled to {action}",
            {"action": action, "role": role},
        )


def parse_entitlements(entitlements_json: str | None) -> set[str]:
    """Parse a JSON string[] of action keys, keeping ONLY valid engine actions — the minting ceiling:
    an allow-list / service-key entitlement can never grant a non-engine power (e.g. role=admin)."""
    try:
        raw = json.loads(entitlements_json or "[]")
    except (ValueError, TypeError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {a for a in raw if isinstance(a, str) and a in ENGINE_ACTIONS}
