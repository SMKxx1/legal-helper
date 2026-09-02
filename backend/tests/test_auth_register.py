"""``POST /api/auth/register`` — self-service sign-up with the user's own OpenRouter key.

OpenRouter is mocked with ``httpx.MockTransport``: no test may spend money or need a real key.
The property that matters most here is that a REJECTED key creates no account at all — a
half-created user whose first review is guaranteed to fail is worse than a failed sign-up.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.api import routes_auth, routes_me
from app.models import User


def _mock_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.ai/api/v1"
    )


def _key_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"label": "workshop key", "limit": 10.0, "usage": 2.5}}
    )


def _key_rejected(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "No auth credentials found"}})


@pytest.fixture(autouse=True)
def _reset_signup_throttle():
    routes_auth.reset_signup_throttle()
    yield
    routes_auth.reset_signup_throttle()


@pytest.fixture
def openrouter_ok(monkeypatch):
    monkeypatch.setattr(routes_me, "_openrouter_client", lambda: _mock_client(_key_ok))


def _payload(**over):
    body = {
        "username": "jane.tan",
        "password": "correct-horse-battery",
        "api_key": "sk-or-v1-abcd1234",
    }
    body.update(over)
    return body


def test_register_creates_user_signs_in_and_stores_the_key(client, db, openrouter_ok):
    resp = client.post("/api/auth/register", json=_payload(display_name="Jane Tan"))
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # signed in immediately, and already past the add-key step
    assert body["token"]
    assert body["user"]["username"] == "jane.tan"
    assert body["user"]["display_name"] == "Jane Tan"
    assert body["user"]["role"] == "user"
    assert body["user"]["has_key"] is True
    assert body["user"]["key_last4"] == "1234"

    # the plaintext key is never echoed back
    assert "sk-or-v1-abcd1234" not in resp.text

    # ...and is stored encrypted, not in the clear
    user = db.execute(select(User).where(User.username == "jane.tan")).scalar_one()
    assert user.openrouter_key_enc
    assert "sk-or-v1-abcd1234" not in user.openrouter_key_enc

    # the returned token actually works
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["username"] == "jane.tan"


def test_register_rejects_a_key_openrouter_refuses_and_creates_nothing(
    client, db, monkeypatch
):
    monkeypatch.setattr(
        routes_me, "_openrouter_client", lambda: _mock_client(_key_rejected)
    )
    resp = client.post("/api/auth/register", json=_payload())
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_openrouter_key"
    assert db.execute(select(User).where(User.username == "jane.tan")).first() is None


def test_register_rejects_a_duplicate_username(client, seed_user, openrouter_ok):
    first = client.post("/api/auth/register", json=_payload())
    assert first.status_code == 201
    again = client.post(
        "/api/auth/register", json=_payload(password="another-password")
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "username_taken"


def test_register_normalises_username_case(client, db, openrouter_ok):
    resp = client.post("/api/auth/register", json=_payload(username="Jane.TAN"))
    assert resp.status_code == 201
    assert resp.json()["user"]["username"] == "jane.tan"


@pytest.mark.parametrize(
    "username", ["ab", "has space", "UPPER!", "-leading", "trailing-", "x" * 40]
)
def test_register_rejects_bad_usernames(client, openrouter_ok, username):
    resp = client.post("/api/auth/register", json=_payload(username=username))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_username"


def test_register_rejects_a_short_password(client, openrouter_ok):
    resp = client.post("/api/auth/register", json=_payload(password="short"))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "weak_password"


def test_register_can_be_closed_off(client, openrouter_ok, monkeypatch):
    """SIGNUP_ENABLED=false locks the door after the workshop without breaking existing accounts."""
    from app.config import settings

    monkeypatch.setattr(settings, "signup_enabled", False)
    resp = client.post("/api/auth/register", json=_payload())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "signup_disabled"


def test_register_is_rate_limited_per_ip(client, openrouter_ok):
    for i in range(routes_auth._SIGNUP_LIMIT):
        r = client.post("/api/auth/register", json=_payload(username=f"user.{i:02d}"))
        assert r.status_code == 201, r.text
    blocked = client.post("/api/auth/register", json=_payload(username="one.too.many"))
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_signups"
