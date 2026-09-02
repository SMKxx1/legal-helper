"""POST /api/auth/login and /api/auth/logout: success, wrong password, unknown user, throttle."""

from __future__ import annotations

# `conftest._reset_login_throttle` (autouse) resets the module-global throttle around every test.


def test_login_ok_returns_token_and_user(client, seed_user):
    seed_user(username="alice.tan", password="correct horse")
    resp = client.post(
        "/api/auth/login", json={"username": "alice.tan", "password": "correct horse"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["expires_at"]
    assert body["user"]["username"] == "alice.tan"
    assert body["user"]["has_key"] is False
    assert body["user"]["key_last4"] is None


def test_login_wrong_password_is_401(client, seed_user):
    seed_user(username="alice.tan", password="correct horse")
    resp = client.post(
        "/api/auth/login", json={"username": "alice.tan", "password": "wrong"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"


def test_login_unknown_user_is_401_same_as_wrong_password(client, seed_user):
    seed_user(username="alice.tan", password="correct horse")
    resp = client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"


def test_logout_deletes_the_session(client, seed_user, login):
    seed_user(username="alice.tan", password="correct horse")
    token = login()
    resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204

    # The now-revoked token no longer authenticates.
    resp2 = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 401


def test_logout_without_a_token_is_401(client):
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 401


def test_login_throttled_after_20_failures_per_ip(client, seed_user):
    seed_user(username="alice.tan", password="correct horse")

    for _ in range(20):
        resp = client.post(
            "/api/auth/login", json={"username": "alice.tan", "password": "wrong"}
        )
        assert resp.status_code == 401

    # The 21st attempt is throttled — even with the CORRECT password.
    resp = client.post(
        "/api/auth/login", json={"username": "alice.tan", "password": "correct horse"}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "too_many_attempts"
