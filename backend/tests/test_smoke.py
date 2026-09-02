"""Smoke tests that validate the shared test harness (app mounting, DB override,
seed/login helpers) and the most basic public-surface contracts."""

from __future__ import annotations

# test_root_metadata removed: no root metadata route by design — default-deny 404 (PLAN §3.1), asserted by tests/test_healthz.py.


def test_login_success_sets_session_and_returns_user(client, seed_user, login):
    seed_user("alice", role="reviewer")
    r = login("alice")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "alice"
    assert body["role"] == "reviewer"
    # cookies set on the client jar
    assert client.cookies.get("sid")
    assert client.cookies.get("csrf")
    # effective permissions present
    assert set(body["permissions"]) == {
        "view_all_docs",
        "view_all_spend",
        "manage_permissions",
    }


def test_login_wrong_password_is_generic_401(client, seed_user, login):
    seed_user("alice")
    r = login("alice", password="wrong-password")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"


def test_login_unknown_user_is_same_generic_401(client, login):
    r = login("nobody", password="whatever")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"


def test_me_requires_authentication(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_returns_current_user_after_login(client, seed_user, login):
    seed_user("alice", role="admin")
    login("alice")
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user_id"] == "alice"
    assert r.json()["role"] == "admin"
