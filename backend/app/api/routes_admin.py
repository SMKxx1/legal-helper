"""Admin user + audit management (P0-8) — prefix /api/admin, all behind require_role('admin').

Create user accounts (with a one-time temp password + forced change), set role/status, reset a
password, delete, and read the auth audit log. Every query is scoped to the caller's org_id and
every mutation writes an AuthAuditEvent. CSRF protection for these state-changing POSTs is added
centrally in P0-10 (middleware over /api/auth + /api/admin).
"""

from __future__ import annotations

import re as _re

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.api.errors import EngineError
from app.api.routes_auth import _audit, _client_ip
from app.auth.deps import require_admin
from app.auth.entitlement import SERVICE_KEY_SCOPES
from app.auth.models import (
    AuthAuditEvent,
    IdentitySession,
    PasswordResetToken,
    ServiceAccountKey,
    UserAccount,
)
from app.auth.security import hash_password, new_token
from app.auth.service_account import hash_key
from app.auth.sessions import Principal, revoke_all_for_user
from app.bot.models import NdaAllowlist, NdaPendingRequest
from app.db import get_db
from app.schemas import (
    ServiceKeyIn,
    ServiceKeyPatch,
    UserRole,
    UserStatus,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

_ROLES = {r.value for r in UserRole}
_STATUSES = {s.value for s in UserStatus}


def _temp_password() -> str:
    return new_token(
        12
    )  # ~16 url-safe chars, relayed once; the user must change it on first login


class UserCreateIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    role: str = Field(default="viewer")
    team: str | None = Field(default=None, max_length=64)
    # Optional admin-set temporary password (must be >=8). Omitted -> the server generates one and
    # returns it once. Either way the new user must change it on first sign-in.
    temp_password: str | None = Field(default=None, min_length=8, max_length=128)


class UserPatchIn(BaseModel):
    name: str | None = None
    role: str | None = None
    status: str | None = None
    team: str | None = None
    can_view_all_docs: bool | None = None
    can_view_all_spend: bool | None = None
    can_manage_permissions: bool | None = None
    # Bridge a Slack member id to this web account so the account's role drives the bot approval gate
    # (empty string clears it). Unique across accounts — a clash is rejected 409.
    slack_user_id: str | None = Field(default=None, max_length=64)


class ResetPasswordIn(BaseModel):
    # Optional admin-set temp password; omitted -> the server generates one.
    temp_password: str | None = Field(default=None, min_length=8, max_length=128)


def _user_out(u: UserAccount) -> dict:
    return {
        "id": u.id,
        "user_id": u.user_id,
        "name": u.name,
        "email": u.email,
        "role": u.role,
        "status": u.status,
        "team": u.team,
        "slack_user_id": u.slack_user_id,
        "permissions": {
            "view_all_docs": bool(u.can_view_all_docs),
            "view_all_spend": bool(u.can_view_all_spend),
            "manage_permissions": bool(u.can_manage_permissions),
        },
        "must_change_password": bool(u.must_change_password),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


def _active_admin_count(db, org_id: str) -> int:
    return db.execute(
        select(func.count())
        .select_from(UserAccount)
        .where(
            UserAccount.org_id == org_id,
            UserAccount.role == "admin",
            UserAccount.status == "active",
        )
    ).scalar_one()


def _load_in_org(db, user_pk: str, org_id: str) -> UserAccount:
    u = db.get(UserAccount, user_pk)
    if (
        u is None or u.org_id != org_id
    ):  # org scoping: never reveal/touch another org's user
        raise EngineError(404, "not_found", "No such user.")
    return u


@router.get("/users")
def list_users(
    admin: Principal = Depends(require_admin), db=Depends(get_db)
) -> list[dict]:
    rows = (
        db.execute(
            select(UserAccount)
            .where(UserAccount.org_id == admin.org_id)
            .order_by(UserAccount.created_at.desc(), UserAccount.id.desc())
        )
        .scalars()
        .all()
    )
    return [_user_out(u) for u in rows]


@router.post("/users", status_code=201)
def create_user(
    body: UserCreateIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    if body.role not in _ROLES:
        raise EngineError(400, "bad_request", f"role must be one of {sorted(_ROLES)}.")
    temp = body.temp_password or _temp_password()
    u = UserAccount(
        org_id=admin.org_id,
        user_id=body.user_id,
        name=(body.name or None),
        email=(body.email or None),
        password_hash=hash_password(temp),
        role=body.role,
        status="active",
        team=(body.team or None),
        must_change_password=True,
        created_by=admin.user_pk,
    )
    db.add(u)
    try:
        db.flush()
    except IntegrityError as err:
        db.rollback()
        raise EngineError(
            409, "conflict", "A user with that user_id or email already exists."
        ) from err
    _audit(
        db,
        action="user_create",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=body.user_id,
        detail=f"role={body.role}",
        ip=_client_ip(request),
    )
    db.commit()
    return {
        "user": _user_out(u),
        "temp_password": temp,
    }  # the temp password is returned ONCE


@router.patch("/users/{user_pk}")
def patch_user(
    user_pk: str,
    body: UserPatchIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    u = _load_in_org(db, user_pk, admin.org_id)
    new_role = body.role if body.role is not None else u.role
    new_status = body.status if body.status is not None else u.status
    if new_role not in _ROLES:
        raise EngineError(400, "bad_request", f"role must be one of {sorted(_ROLES)}.")
    if new_status not in _STATUSES:
        raise EngineError(
            400, "bad_request", f"status must be one of {sorted(_STATUSES)}."
        )

    # Last-admin guard: never demote/disable the final active admin (avoids locking out the org).
    # Only an ACTIVE admin counts toward that floor, so demoting/disabling an already-disabled admin
    # must not trip it (it can't drop the active-admin count) — else cleanup of an ex-admin is blocked.
    if (
        u.role == "admin"
        and u.status == "active"
        and (new_role != "admin" or new_status != "active")
        and _active_admin_count(db, admin.org_id) <= 1
    ):
        raise EngineError(400, "last_admin", "Cannot remove the last active admin.")

    role_changed = new_role != u.role
    u.role, u.status = new_role, new_status
    # Profile + granular permissions (independent of the role/status state machine above).
    if body.name is not None:
        u.name = body.name or None
    if body.team is not None:
        u.team = body.team or None
    grants_changed = False
    if body.can_view_all_docs is not None:
        grants_changed |= bool(body.can_view_all_docs) != u.can_view_all_docs
        u.can_view_all_docs = bool(body.can_view_all_docs)
    if body.can_view_all_spend is not None:
        grants_changed |= bool(body.can_view_all_spend) != u.can_view_all_spend
        u.can_view_all_spend = bool(body.can_view_all_spend)
    if body.can_manage_permissions is not None:
        grants_changed |= bool(body.can_manage_permissions) != u.can_manage_permissions
        u.can_manage_permissions = bool(body.can_manage_permissions)
    if body.slack_user_id is not None:
        sid = body.slack_user_id.strip() or None
        if sid is not None:
            clash = db.execute(
                select(UserAccount.id).where(
                    UserAccount.slack_user_id == sid, UserAccount.id != u.id
                )
            ).first()
            if clash is not None:
                raise EngineError(
                    409,
                    "conflict",
                    "That Slack user id is already linked to another account.",
                )
        u.slack_user_id = sid
    if new_status == "active":
        u.locked_until = None  # reactivating clears a brute-force lockout so the
        u.failed_login_count = 0  # admin can deterministically restore sign-in
    # Invalidate sessions on a disable, a role change, OR a granular-permission change: a downgrade
    # must take effect immediately, not after the 30s validation-cache TTL (the cached Principal
    # snapshots the OLD role/grants). revoke_all bumps the epoch + clears the cache, forcing re-auth.
    if new_status != "active" or role_changed or grants_changed:
        revoke_all_for_user(db, u)
    _audit(
        db,
        action="user_update",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=u.user_id,
        detail=f"role={new_role} status={new_status}",
        ip=_client_ip(request),
    )
    db.commit()
    return _user_out(u)


@router.delete("/users/{user_pk}")
def delete_user(
    user_pk: str,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    u = _load_in_org(db, user_pk, admin.org_id)
    if (
        u.role == "admin"
        and u.status == "active"
        and _active_admin_count(db, admin.org_id) <= 1
    ):
        raise EngineError(400, "last_admin", "Cannot delete the last active admin.")
    uid = u.user_id
    # Clear the session cache (immediate + after-commit) BEFORE deleting, like every other
    # user-invalidating path — else the deleted user's cached Principal keeps authenticating (with
    # full role authority) for up to the cache TTL, since validate()'s cache-hit path never re-reads
    # the now-gone DB rows.
    revoke_all_for_user(db, u)
    db.execute(delete(IdentitySession).where(IdentitySession.user_id == u.id))
    db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == u.id))
    db.delete(u)
    _audit(
        db,
        action="user_delete",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=uid,
        ip=_client_ip(request),
    )
    db.commit()
    return {"ok": True}


@router.post("/users/{user_pk}/reset-password")
def reset_user_password(
    user_pk: str,
    request: Request,
    body: ResetPasswordIn | None = Body(default=None),
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    u = _load_in_org(db, user_pk, admin.org_id)
    temp = (body.temp_password if body else None) or _temp_password()
    u.password_hash = hash_password(temp)
    u.must_change_password = True
    u.locked_until = None  # a password reset also clears any brute-force lockout
    u.failed_login_count = 0
    revoke_all_for_user(db, u)  # force re-login with the temp password
    _audit(
        db,
        action="user_reset_password",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=u.user_id,
        ip=_client_ip(request),
    )
    db.commit()
    return {"temp_password": temp}


@router.get("/teams")
def list_teams(
    admin: Principal = Depends(require_admin), db=Depends(get_db)
) -> list[str]:
    """Distinct, non-empty team labels in the org — drives the team <select> in the Settings
    permissions panel and the spend-breakdown team filter."""
    rows = (
        db.execute(
            select(UserAccount.team)
            .where(
                UserAccount.org_id == admin.org_id,
                UserAccount.team.is_not(None),
                UserAccount.team != "",
            )
            .distinct()
            .order_by(UserAccount.team)
        )
        .scalars()
        .all()
    )
    return [t for t in rows if t]


@router.get("/capabilities")
def get_capabilities(
    request: Request,
    admin: Principal = Depends(require_admin),
) -> dict:
    """The detailed capability report — the admin-gated counterpart of the shallow public ``/healthz``
    (PLAN §6, §2 decision 4: "the detailed capability report moves behind admin auth").

    Returns the boot-time :meth:`app.capabilities.CapabilityRegistry.report` (per-capability
    ``{name, state, reason, summary, critical}``) plus the running app version + environment, so the
    admin home page can render one status card per integration (enabled / disabled(missing config) /
    unhealthy) without the public liveness probe ever leaking config state. No secrets are returned —
    only capability NAMES and STATES, never the underlying values.
    """
    reg = getattr(request.app.state, "capabilities", None)
    settings = getattr(request.app.state, "settings", None)
    capabilities = reg.report() if reg is not None else []
    return {
        "app": {
            "version": request.app.version,
            "env": settings.app_env if settings is not None else "",
        },
        "capabilities": capabilities,
    }


@router.get("/audit")
def get_audit(
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    rows = (
        db.execute(
            select(AuthAuditEvent)
            .where(
                (AuthAuditEvent.org_id == admin.org_id)
                | (AuthAuditEvent.org_id.is_(None))
            )
            .order_by(AuthAuditEvent.created_at.desc(), AuthAuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": e.id,
            "action": e.action,
            "actor_principal": e.actor_principal,
            "actor_type": e.actor_type,
            "target": e.target,
            "detail": e.detail,
            "ip": e.ip,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in rows
    ]


# --------------------------------------------------------------------------- #
# Service-account key CRUD (P1-4) — DB-backed machine keys for /v1 (n8n, Word add-in,
# partners). Replaces the coarse ENGINE_SERVICE_KEYS env pairs, which grant ALL engine
# actions and rotate only by redeploy: a DB key carries its own scoped entitlements +
# rate/monthly-cost caps, and rotation is active=false + a fresh insert — no redeploy.
# The raw key is returned ONCE (create/rotate); only its sha256 is stored (hash_key).
# --------------------------------------------------------------------------- #
_PRINCIPAL_ID_RE = _re.compile(r"[a-z0-9][a-z0-9:._-]{0,31}")
_SLUG_RE = _re.compile(r"[^a-z0-9._-]+")


def _validated_scopes(entitlements: list[str]) -> list[str]:
    """Service-key scopes are validated at WRITE time (resolve_service_key reads them verbatim —
    unlike the signed path there is no read-time clamp), so a typo can't mint an inert scope."""
    ents = sorted({e.strip() for e in entitlements if e and e.strip()})
    bad = [e for e in ents if e not in SERVICE_KEY_SCOPES]
    if bad or not ents:
        raise EngineError(
            400,
            "bad_request",
            f"Invalid entitlements {bad}; valid scopes: {sorted(SERVICE_KEY_SCOPES)}.",
        )
    return ents


def _derive_principal_id(name: str, explicit: str | None) -> str:
    pid = (explicit or "").strip().lower()
    if not pid:
        pid = ("svc:" + _SLUG_RE.sub("-", name.strip().lower()).strip("-"))[:32]
    if not _PRINCIPAL_ID_RE.fullmatch(pid):
        raise EngineError(
            400,
            "bad_request",
            "principal_id must be 1-32 chars of [a-z0-9:._-] (e.g. 'svc:n8n').",
        )
    return pid


def _month_spend_by_principal(db, org_id: str) -> dict[str, float]:
    """Current-UTC-month engine spend grouped by principal — PRINCIPAL-level, not per-key (the
    live cost cap sums EngineReview.actor_user_id the same way; keys sharing a principal share
    the bucket). Reported on the list so revocation decisions see real usage."""
    from datetime import UTC, datetime

    from app.models import EngineReview

    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(
            EngineReview.actor_user_id,
            func.coalesce(func.sum(EngineReview.cost_usd), 0.0),
        )
        .where(
            EngineReview.org_id == org_id,
            EngineReview.created_at >= start,
            EngineReview.actor_user_id.is_not(None),
        )
        .group_by(EngineReview.actor_user_id)
    ).all()
    return {pid: float(total or 0.0) for pid, total in rows}


def _service_key_out(
    row: ServiceAccountKey, spend: dict[str, float] | None = None
) -> dict:
    import json

    try:
        ents = sorted(set(json.loads(row.entitlements_json or "[]")))
    except (ValueError, TypeError):
        ents = []
    out = {
        "id": row.id,
        "name": row.name,
        "principal_id": row.principal_id,
        "entitlements": ents,
        "rate_per_min": row.rate_per_min,
        "monthly_cost_cap_usd": row.monthly_cost_cap_usd,
        "active": bool(row.active),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }
    if spend is not None:
        out["monthly_spend_usd"] = round(spend.get(row.principal_id, 0.0), 4)
    return out


def _load_service_key(db, key_id: str, org_id: str) -> ServiceAccountKey:
    row = db.execute(
        select(ServiceAccountKey).where(
            ServiceAccountKey.id == key_id, ServiceAccountKey.org_id == org_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise EngineError(404, "not_found", "Service key not found.")
    return row


def _insert_service_key(
    db,
    *,
    org_id: str,
    name: str,
    principal_id: str,
    entitlements: list[str],
    rate_per_min: int | None,
    monthly_cost_cap_usd: float | None,
) -> tuple[ServiceAccountKey, str]:
    """Insert an ACTIVE key row and return (row, raw_key). The raw key exists only in this
    response; the row stores sha256(raw) (an IntegrityError on the hash is a collision-grade
    accident -> 409, mirroring the allowed-accounts template)."""
    import json

    raw = new_token(32)  # 256-bit, URL-safe; shown once
    row = ServiceAccountKey(
        org_id=org_id,
        name=name.strip(),
        key_hash=hash_key(raw),
        principal_id=principal_id,
        entitlements_json=json.dumps(entitlements),
        rate_per_min=(rate_per_min or None),
        monthly_cost_cap_usd=(monthly_cost_cap_usd or None),
        active=True,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as err:
        db.rollback()
        raise EngineError(409, "conflict", "Key hash collision; retry.") from err
    return row, raw


@router.get("/service-keys")
def list_service_keys(
    admin: Principal = Depends(require_admin), db=Depends(get_db)
) -> list[dict]:
    rows = (
        db.execute(
            select(ServiceAccountKey)
            .where(ServiceAccountKey.org_id == admin.org_id)
            .order_by(ServiceAccountKey.created_at.desc(), ServiceAccountKey.id.desc())
        )
        .scalars()
        .all()
    )
    spend = _month_spend_by_principal(db, admin.org_id)
    return [_service_key_out(r, spend) for r in rows]


@router.post("/service-keys", status_code=201)
def create_service_key(
    body: ServiceKeyIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    ents = _validated_scopes(body.entitlements)
    pid = _derive_principal_id(body.name, body.principal_id)
    row, raw = _insert_service_key(
        db,
        org_id=admin.org_id,
        name=body.name,
        principal_id=pid,
        entitlements=ents,
        rate_per_min=body.rate_per_min,
        monthly_cost_cap_usd=body.monthly_cost_cap_usd,
    )
    _audit(
        db,
        action="service_key_create",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=row.id,
        detail=f"principal={pid} entitlements={ents}",
        ip=_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return {"key": _service_key_out(row), "raw_key": raw}  # raw_key shown ONCE


@router.post("/service-keys/{key_id}/rotate", status_code=201)
def rotate_service_key(
    key_id: str,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    """Revoke the old key and mint a fresh one with the SAME principal/entitlements/caps.
    Same principal_id -> the caller's spend/rate buckets carry over (rotation, not a new
    identity). The old row stays for audit (active=false)."""
    import json

    old = _load_service_key(db, key_id, admin.org_id)
    old.active = False
    try:
        ents = sorted(set(json.loads(old.entitlements_json or "[]")))
    except (ValueError, TypeError):
        ents = []
    if not ents:
        raise EngineError(
            400,
            "bad_request",
            "Cannot rotate a key with no entitlements; create a new one.",
        )
    row, raw = _insert_service_key(
        db,
        org_id=admin.org_id,
        name=old.name,
        principal_id=old.principal_id,
        entitlements=ents,
        rate_per_min=old.rate_per_min,
        monthly_cost_cap_usd=old.monthly_cost_cap_usd,
    )
    _audit(
        db,
        action="service_key_rotate",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=row.id,
        detail=f"rotated from {old.id} (principal={old.principal_id})",
        ip=_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return {"key": _service_key_out(row), "raw_key": raw}  # raw_key shown ONCE


@router.patch("/service-keys/{key_id}")
def patch_service_key(
    key_id: str,
    body: ServiceKeyPatch,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    import json

    row = _load_service_key(db, key_id, admin.org_id)
    if body.name is not None:
        row.name = body.name.strip()
    if body.entitlements is not None:
        row.entitlements_json = json.dumps(_validated_scopes(body.entitlements))
    if body.rate_per_min is not None:
        row.rate_per_min = body.rate_per_min or None  # 0 clears to the global default
    if body.monthly_cost_cap_usd is not None:
        row.monthly_cost_cap_usd = body.monthly_cost_cap_usd or None  # 0 clears
    if body.active is not None:
        row.active = bool(
            body.active
        )  # false = immediate revocation (auth reads active only)
    _audit(
        db,
        action="service_key_update",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=row.id,
        detail=f"active={row.active}",
        ip=_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return _service_key_out(row)


# --------------------------------------------------------------------------- #
# Access control (PLAN §3.4 rework): the bot approval allowlist + roles, the
# pending-request queue, and the dashboard-managed admin routing. All behind
# require_admin; every write is audited. FAIL-CLOSED by construction — nothing
# here can grant an intent to run, it only manages who is exempt / who approves.
# --------------------------------------------------------------------------- #
_ALLOWLIST_TYPES = {"slack", "email"}
_ALLOWLIST_ROLES = {"member", "admin"}


class AllowlistIn(BaseModel):
    principal_type: str = Field(min_length=1, max_length=16)  # slack | email
    principal_key: str = Field(min_length=1, max_length=255)
    role: str = Field(default="member")  # member | admin
    label: str | None = Field(default=None, max_length=255)


class AdminRoutingIn(BaseModel):
    nda_admin_slack_channel: str | None = None
    nda_admin_email: str | None = None


def _allowlist_out(r: NdaAllowlist) -> dict:
    return {
        "id": r.id,
        "principal_type": r.principal_type,
        "principal_key": r.principal_key,
        "role": r.role,
        "label": r.label,
        "added_by": r.added_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _pending_out(r: NdaPendingRequest) -> dict:
    return {
        "id": r.id,
        "requester": r.requester,
        "channel": r.channel,
        "intent": r.intent,
        "status": r.status,
        "request_key": r.request_key,
        "has_document": bool(r.document_blob_id),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "decided_by": r.decided_by,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
    }


@router.get("/allowlist")
def list_allowlist(
    admin: Principal = Depends(require_admin), db=Depends(get_db)
) -> dict:
    rows = (
        db.execute(select(NdaAllowlist).order_by(NdaAllowlist.created_at))
        .scalars()
        .all()
    )
    return {"allowlist": [_allowlist_out(r) for r in rows]}


@router.post("/allowlist", status_code=201)
def upsert_allowlist(
    body: AllowlistIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    ptype = body.principal_type.strip().lower()
    if ptype not in _ALLOWLIST_TYPES:
        raise EngineError(
            400,
            "bad_request",
            f"principal_type must be one of {sorted(_ALLOWLIST_TYPES)}.",
        )
    role = body.role.strip().lower()
    if role not in _ALLOWLIST_ROLES:
        raise EngineError(
            400, "bad_request", f"role must be one of {sorted(_ALLOWLIST_ROLES)}."
        )
    key = body.principal_key.strip()
    if not key:
        raise EngineError(400, "bad_request", "principal_key is required.")
    if ptype == "email":
        key = key.lower()  # emails are matched case-insensitively by the gate
    label = (body.label or "").strip() or None

    existing = db.execute(
        select(NdaAllowlist).where(
            NdaAllowlist.principal_type == ptype, NdaAllowlist.principal_key == key
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role, existing.label = role, label
        row = existing
    else:
        row = NdaAllowlist(
            principal_type=ptype,
            principal_key=key,
            role=role,
            label=label,
            added_by=admin.user_id,
        )
        db.add(row)
    _audit(
        db,
        action="allowlist_upsert",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=f"{ptype}:{key}",
        detail=f"role={role}",
        ip=_client_ip(request),
    )
    try:
        db.commit()
    except IntegrityError as err:
        db.rollback()
        raise EngineError(
            409, "conflict", "That principal is already on the allowlist."
        ) from err
    db.refresh(row)
    return _allowlist_out(row)


@router.delete("/allowlist/{row_id}")
def delete_allowlist(
    row_id: str,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    row = db.get(NdaAllowlist, row_id)
    if row is None:
        raise EngineError(404, "not_found", "No such allowlist entry.")
    target = f"{row.principal_type}:{row.principal_key}"
    db.delete(row)
    _audit(
        db,
        action="allowlist_delete",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target=target,
        ip=_client_ip(request),
    )
    db.commit()
    return {"ok": True}


@router.get("/pending")
def list_pending(admin: Principal = Depends(require_admin), db=Depends(get_db)) -> dict:
    rows = (
        db.execute(
            select(NdaPendingRequest).order_by(NdaPendingRequest.created_at.desc())
        )
        .scalars()
        .all()
    )
    return {"pending": [_pending_out(r) for r in rows]}


@router.get("/admin-routing")
def get_admin_routing(
    admin: Principal = Depends(require_admin), db=Depends(get_db)
) -> dict:
    from app.settings_store import admin_routing

    channel, email = admin_routing(db=db)
    return {"nda_admin_slack_channel": channel, "nda_admin_email": email}


@router.put("/admin-routing")
def put_admin_routing(
    body: AdminRoutingIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    from app.settings_store import admin_routing, set_overrides

    updates: dict[str, str | None] = {}
    if body.nda_admin_slack_channel is not None:
        updates["nda_admin_slack_channel"] = body.nda_admin_slack_channel.strip()
    if body.nda_admin_email is not None:
        updates["nda_admin_email"] = body.nda_admin_email.strip()
    set_overrides(updates)  # its own session + commit (mirrors /api/settings)
    _audit(
        db,
        action="admin_routing_update",
        actor_principal=admin.user_id,
        org_id=admin.org_id,
        target="admin_routing",
        detail=",".join(sorted(updates.keys())),
        ip=_client_ip(request),
    )
    db.commit()
    channel, email = admin_routing(db=db)
    return {"nda_admin_slack_channel": channel, "nda_admin_email": email}
