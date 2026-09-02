"""Service-account key admin CRUD (P1-4) + end-to-end key auth.

The ServiceAccountKey table/resolver were live but had NO create/rotate path (rows only via raw
SQL). These tests lock the CRUD contract (raw key shown once, sha256 at rest, scope validation,
org scoping) and the end-to-end behavior: a minted key authenticates /v1 with EXACTLY its scoped
entitlements, revocation is immediate, and rotation carries the principal over.

Port note (P1 wave 2.5): ``test_bot_scope_key_drives_dal_plane_but_never_reviews`` was DROPPED —
it hit the RETIRED ``/v1/support_task/bot/event`` n8n DAL plane, which no longer exists. The CRUD +
key-auth contract below (``/api/admin/service-keys`` GET/POST/{id}/rotate/PATCH) is ported verbatim.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from app.api import routes_v1


@pytest.fixture
def admin(client, seed_user, login):
    seed_user("root", role="admin")
    assert login("root").status_code == 200
    return client


def _create_key(client, **overrides) -> dict:
    body = {"name": "n8n bridge", "entitlements": ["review.quick"]}
    body.update(overrides)
    resp = client.post("/api/admin/service-keys", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stub_engine(monkeypatch):
    result = SimpleNamespace(
        risk_tier="green",
        adherence_score=100.0,
        perspective="mutual",
        playbook_version="test",
        routing={},
        counts={},
        cost_usd=0.001,
        input_tokens=1,
        output_tokens=1,
        findings=[],
        cross_clause_flags=[],
        coverage=SimpleNamespace(absent_required=[]),
    )
    monkeypatch.setattr(routes_v1, "_run_engine", lambda *a, **k: result)


def _post_review(client, key: str, mode: str = "quick", body: bytes = b"doc text"):
    return client.post(
        "/v1/reviews",
        data={"mode": mode},
        files={"file": ("nda.txt", io.BytesIO(body), "text/plain")},
        headers={"X-API-Key": key},
    )


def test_create_returns_raw_key_once_and_stores_only_the_hash(admin, db):
    from app.auth.models import ServiceAccountKey
    from app.auth.service_account import hash_key

    out = _create_key(admin)
    raw = out["raw_key"]
    assert raw and len(raw) >= 32
    assert out["key"]["principal_id"] == "svc:n8n-bridge"  # derived from the name
    assert out["key"]["entitlements"] == ["review.quick"]

    row = db.query(ServiceAccountKey).one()
    assert row.key_hash == hash_key(raw)
    assert raw not in (row.key_hash, row.name, row.entitlements_json)

    # The list never re-exposes the raw key (or the hash).
    listed = admin.get("/api/admin/service-keys").json()
    assert len(listed) == 1
    assert "raw_key" not in listed[0] and "key_hash" not in listed[0]
    assert "monthly_spend_usd" in listed[0]


def test_invalid_entitlement_scope_is_rejected(admin):
    resp = admin.post(
        "/api/admin/service-keys",
        json={"name": "typo", "entitlements": ["review.qiuck"]},
    )
    assert resp.status_code == 400
    assert "review.qiuck" in resp.json()["error"]["message"]


def _drop_session(client) -> None:
    """The /v1 calls must authenticate with the X-API-Key ALONE — the admin's web session
    cookie would win in the principal resolution order (WEB before SERVICE) and mask the
    key's scoped entitlements."""
    client.cookies.clear()


def test_minted_key_authenticates_v1_with_exactly_its_scopes(admin, monkeypatch):
    _stub_engine(monkeypatch)
    raw = _create_key(admin, entitlements=["review.quick"])["raw_key"]
    _drop_session(admin)

    ok = _post_review(admin, raw, mode="quick")
    assert ok.status_code == 201, ok.text

    denied = _post_review(admin, raw, mode="deep", body=b"other doc")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "not_entitled"


def test_revocation_is_immediate_and_keyless_fails_closed(admin, monkeypatch):
    _stub_engine(monkeypatch)
    out = _create_key(admin)
    raw, key_id = out["raw_key"], out["key"]["id"]

    patched = admin.patch(f"/api/admin/service-keys/{key_id}", json={"active": False})
    assert patched.status_code == 200 and patched.json()["active"] is False

    # Revoked key -> 401; and with a DB-keys-only deployment a keyless request must also 401
    # (fail closed), never bind the open svc:local dev principal. An ACTIVE row must exist for
    # the engine to count as configured, so mint (and keep) a second active key first.
    _create_key(admin, name="other")
    _drop_session(admin)
    assert _post_review(admin, raw, body=b"another").status_code == 401
    keyless = admin.post(
        "/v1/reviews",
        data={"mode": "quick"},
        files={"file": ("nda.txt", io.BytesIO(b"doc"), "text/plain")},
    )
    assert keyless.status_code == 401


def test_rotate_revokes_old_key_and_carries_the_principal(admin, monkeypatch):
    _stub_engine(monkeypatch)
    out = _create_key(admin)
    old_raw, key_id = out["raw_key"], out["key"]["id"]

    rotated = admin.post(f"/api/admin/service-keys/{key_id}/rotate")
    assert rotated.status_code == 201, rotated.text
    new = rotated.json()
    assert new["raw_key"] != old_raw
    assert new["key"]["principal_id"] == out["key"]["principal_id"]
    assert new["key"]["entitlements"] == out["key"]["entitlements"]

    _drop_session(admin)
    assert _post_review(admin, old_raw).status_code == 401  # old key dead
    assert _post_review(admin, new["raw_key"], body=b"fresh").status_code == 201


def test_service_keys_require_admin(client, seed_user, login):
    seed_user("bob", role="reviewer")
    assert login("bob").status_code == 200
    assert client.get("/api/admin/service-keys").status_code == 403
    assert (
        client.post(
            "/api/admin/service-keys",
            json={"name": "x", "entitlements": ["review.quick"]},
        ).status_code
        == 403
    )
