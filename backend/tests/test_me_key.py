"""PUT/DELETE /api/me/openrouter-key, PUT /api/me/models, GET /api/models/zdr — OpenRouter and its
ZDR endpoint list are mocked via ``httpx.MockTransport`` (plan §6 Phase 1 tests)."""

from __future__ import annotations

import httpx
import pytest

from app import crypto
from app.ai import zdr
from app.api import routes_me


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.ai/api/v1"
    )


@pytest.fixture(autouse=True)
def _clear_zdr_cache():
    zdr._cache.clear()
    yield
    zdr._cache.clear()


def _key_ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/v1/key"
    assert request.headers["authorization"] == "Bearer sk-or-real-key-abc123"
    return httpx.Response(
        200, json={"data": {"label": "presenter-demo", "limit": 20.0, "usage": 1.5}}
    )


def _key_rejected_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "invalid key"}})


def test_save_key_stores_ciphertext_not_plaintext_and_returns_only_last4(
    client, seed_user, login, monkeypatch
):
    seed_user(username="alice.tan", password="correct horse")
    token = login()
    monkeypatch.setattr(
        routes_me, "_openrouter_client", lambda: _mock_client(_key_ok_handler)
    )

    resp = client.put(
        "/api/me/openrouter-key",
        json={"api_key": "sk-or-real-key-abc123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_last4"] == "c123"
    assert body["key_label"] == "presenter-demo"
    assert body["limit_remaining"] == pytest.approx(18.5)

    # GET /api/me reflects has_key=true but never the raw key.
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["has_key"] is True
    assert me["key_last4"] == "c123"
    assert "sk-or-real-key-abc123" not in str(me)


def test_saved_key_ciphertext_is_not_the_plaintext(
    client, seed_user, login, monkeypatch, db
):
    seed_user(username="alice.tan", password="correct horse")
    token = login()
    monkeypatch.setattr(
        routes_me, "_openrouter_client", lambda: _mock_client(_key_ok_handler)
    )
    client.put(
        "/api/me/openrouter-key",
        json={"api_key": "sk-or-real-key-abc123"},
        headers={"Authorization": f"Bearer {token}"},
    )

    from app.models import User

    user = db.query(User).filter_by(username="alice.tan").one()
    assert user.openrouter_key_enc is not None
    assert user.openrouter_key_enc != "sk-or-real-key-abc123"
    assert "sk-or-real-key-abc123" not in user.openrouter_key_enc
    assert crypto.decrypt(user.openrouter_key_enc) == "sk-or-real-key-abc123"


def test_save_key_rejected_by_openrouter_is_422(client, seed_user, login, monkeypatch):
    seed_user(username="alice.tan", password="correct horse")
    token = login()
    monkeypatch.setattr(
        routes_me, "_openrouter_client", lambda: _mock_client(_key_rejected_handler)
    )

    resp = client.put(
        "/api/me/openrouter-key",
        json={"api_key": "sk-or-bad-key"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_openrouter_key"


def test_delete_key_clears_it(client, seed_user, login, monkeypatch):
    seed_user(username="alice.tan", password="correct horse")
    token = login()
    monkeypatch.setattr(
        routes_me, "_openrouter_client", lambda: _mock_client(_key_ok_handler)
    )
    client.put(
        "/api/me/openrouter-key",
        json={"api_key": "sk-or-real-key-abc123"},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = client.delete(
        "/api/me/openrouter-key", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 204

    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["has_key"] is False
    assert me["key_last4"] is None


_ZDR_ROWS = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-4-6",
            "name": "Claude Sonnet 4.6",
            "status": "healthy",
            "supported_parameters": ["response_format", "reasoning"],
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
        {
            # not ZDR-usable: no response_format support -> filtered out
            "id": "some/legacy-model",
            "name": "Legacy",
            "status": "healthy",
            "supported_parameters": [],
            "context_length": 8000,
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        },
    ]
}


def _zdr_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/v1/endpoints/zdr"
    return httpx.Response(200, json=_ZDR_ROWS)


def _with_key(client, login, monkeypatch):
    token = login()
    monkeypatch.setattr(
        routes_me, "_openrouter_client", lambda: _mock_client(_key_ok_handler)
    )
    client.put(
        "/api/me/openrouter-key",
        json={"api_key": "sk-or-real-key-abc123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    return token


def test_get_zdr_models_filters_to_response_format_capable(
    client, seed_user, login, monkeypatch
):
    seed_user(username="alice.tan", password="correct horse")
    token = _with_key(client, login, monkeypatch)
    monkeypatch.setattr(zdr, "_openrouter_client", lambda: _mock_client(_zdr_handler))

    resp = client.get("/api/models/zdr", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert ids == ["anthropic/claude-sonnet-4-6"]


def test_zdr_models_requires_a_saved_key(client, seed_user, login):
    seed_user(username="alice.tan", password="correct horse")
    token = login()
    resp = client.get("/api/models/zdr", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_openrouter_key"


def test_model_choice_accepted_when_in_zdr_list(client, seed_user, login, monkeypatch):
    seed_user(username="alice.tan", password="correct horse")
    token = _with_key(client, login, monkeypatch)
    monkeypatch.setattr(zdr, "_openrouter_client", lambda: _mock_client(_zdr_handler))

    resp = client.put(
        "/api/me/models",
        json={"quick": "anthropic/claude-sonnet-4-6"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["preferred_model_quick"] == "anthropic/claude-sonnet-4-6"


def test_model_choice_rejected_when_not_in_zdr_list(
    client, seed_user, login, monkeypatch
):
    seed_user(username="alice.tan", password="correct horse")
    token = _with_key(client, login, monkeypatch)
    monkeypatch.setattr(zdr, "_openrouter_client", lambda: _mock_client(_zdr_handler))

    resp = client.put(
        "/api/me/models",
        json={"quick": "not/a-zdr-model"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "model_not_zdr"


def test_blank_model_clears_preference(client, seed_user, login, monkeypatch, db):
    seed_user(username="alice.tan", password="correct horse")
    token = _with_key(client, login, monkeypatch)
    monkeypatch.setattr(zdr, "_openrouter_client", lambda: _mock_client(_zdr_handler))
    client.put(
        "/api/me/models",
        json={"quick": "anthropic/claude-sonnet-4-6"},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = client.put(
        "/api/me/models",
        json={"quick": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["preferred_model_quick"] is None
