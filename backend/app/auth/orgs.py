"""Org (tenant) provisioning (PL-7).

A single bootstrap org ships by default (``db._seed_default_org``). Multi-tenancy is now schema-ready
— every contract AND its child rows carry ``org_id`` — so additional tenants are seedable through
this helper (used by an admin provisioning path / tests). The engine remains single-default today;
this is the seam that makes a second org a one-row insert rather than a migration.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.auth.models import Org


def create_org(db, *, name: str, org_id: str | None = None) -> Org:
    """Create (or return the existing) org. ``org_id`` defaults to a fresh 32-hex id. Idempotent on a
    supplied id — re-seeding the same org returns the existing row rather than raising. Does NOT
    commit (the caller owns the transaction)."""
    oid = (org_id or uuid.uuid4().hex)[:32]
    existing = db.get(Org, oid)
    if existing is not None:
        return existing
    org = Org(id=oid, name=(name or "")[:255])
    db.add(org)
    db.flush()
    return org


def list_orgs(db) -> list[Org]:
    """All tenants, oldest first — backs an admin org list / future org switcher."""
    return db.execute(select(Org).order_by(Org.created_at)).scalars().all()
