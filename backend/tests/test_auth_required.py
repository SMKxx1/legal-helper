"""Every ``/api/*`` route except ``POST /api/auth/login`` (and the not-yet-built ``/api/status``)
answers 401 with no token and with a garbage token (plan §6 Phase 1 tests)."""

from __future__ import annotations

import pytest

# (method, path, json body) for every currently-registered authenticated route.
PROTECTED_ROUTES = [
    ("POST", "/api/auth/logout", None),
    ("GET", "/api/me", None),
    ("PUT", "/api/me/openrouter-key", {"api_key": "sk-or-anything"}),
    ("DELETE", "/api/me/openrouter-key", None),
    ("PUT", "/api/me/models", {"quick": "anthropic/claude-sonnet-4-6"}),
    ("GET", "/api/models/zdr", None),
]


@pytest.mark.parametrize(
    "method,path,body", PROTECTED_ROUTES, ids=[r[1] for r in PROTECTED_ROUTES]
)
def test_401_with_no_token(client, method, path, body):
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "method,path,body", PROTECTED_ROUTES, ids=[r[1] for r in PROTECTED_ROUTES]
)
def test_401_with_a_garbage_token(client, method, path, body):
    resp = client.request(
        method, path, json=body, headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


def test_login_itself_needs_no_token(client, seed_user):
    seed_user(username="alice.tan", password="correct horse")
    resp = client.post(
        "/api/auth/login", json={"username": "alice.tan", "password": "correct horse"}
    )
    assert resp.status_code == 200
