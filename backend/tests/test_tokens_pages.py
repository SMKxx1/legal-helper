"""Token-registry admin page + API tests (P5 wave B — PLAN §3.7).

Mount the REAL app (auth deps, CSRF middleware, error envelope) on a throwaway SQLite DB. Cover the
admin/header gates on every route, the CRUD surface (create/list/edit/detail), the usage-gated delete
with its typed force-confirmation, and the drift emitted on create/delete. No network: the drift
notifier degrades to a no-op with no reply sink wired.
"""

from __future__ import annotations


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _create_token(client, **over):
    body = {
        "name": over.get("name", "counterparty_signer_name"),
        "label": over.get("label", "Counterparty signer name"),
        "help_text": over.get("help_text", ""),
        "data_type": over.get("data_type", "text"),
        "party": over.get("party", "counterparty"),
        "fallback_text": over.get("fallback_text", ""),
    }
    return client.post("/api/admin/tokens", json=body)


# --------------------------------------------------------------------------- #
# Auth / header gates on every route
# --------------------------------------------------------------------------- #
def test_tokens_list_page_anonymous_is_401(client):
    resp = client.get("/admin/tokens")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


def test_tokens_list_page_reviewer_is_403(client, seed_user, login):
    seed_user("rev", role="reviewer")
    assert login("rev").status_code == 200
    resp = client.get("/admin/tokens")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_create_token_anonymous_is_401(client):
    # No session cookie -> auth 401 (CSRF double-submit only fires when a session cookie is present).
    resp = client.post("/api/admin/tokens", json={"name": "foo"})
    assert resp.status_code == 401


def test_delete_token_reviewer_is_403(client, seed_user, login):
    seed_user("rev", role="reviewer")
    assert login("rev").status_code == 200
    resp = client.post("/api/admin/tokens/foo/delete", json={})
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Create / list / detail
# --------------------------------------------------------------------------- #
def test_create_then_list_and_detail(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200

    resp = _create_token(client, name="purpose")
    assert resp.status_code == 201
    tok = resp.json()["token"]
    assert tok["name"] == "purpose"
    assert tok["placeholder"] == "{{purpose}}"
    assert tok["party"] == "counterparty"

    listing = client.get("/admin/tokens")
    assert listing.status_code == 200
    assert "purpose" in listing.text

    detail = client.get("/admin/tokens/purpose")
    assert detail.status_code == 200
    assert "{{purpose}}" in detail.text
    # An unused token reports no usage.
    assert "not used by any" in detail.text.lower() or "unused" in detail.text.lower()


def test_create_invalid_name_is_400(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = _create_token(client, name="Bad Name")  # spaces/caps: not snake_case
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_create_duplicate_name_is_409(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    assert _create_token(client, name="effective_date").status_code == 201
    dupe = _create_token(client, name="effective_date")
    assert dupe.status_code == 409
    assert dupe.json()["error"]["code"] == "conflict"


def test_update_meta(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    assert _create_token(client, name="city_zip", label="City/ZIP").status_code == 201

    resp = client.patch(
        "/api/admin/tokens/city_zip",
        json={"label": "City and ZIP", "data_type": "text", "party": "internal"},
    )
    assert resp.status_code == 200
    assert resp.json()["token"]["label"] == "City and ZIP"
    assert resp.json()["token"]["party"] == "internal"


def test_update_unknown_token_is_404(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.patch("/api/admin/tokens/nope", json={"label": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Usage-gated delete + typed force-confirmation
# --------------------------------------------------------------------------- #
def test_delete_unused_token_succeeds(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    assert _create_token(client, name="lonely").status_code == 201

    resp = client.post("/api/admin/tokens/lonely/delete", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["usage"]["in_use"] is False
    # Gone from the registry.
    assert client.get("/admin/tokens/lonely").status_code == 404


# --------------------------------------------------------------------------- #
# Drift emitted on create / delete (NDA forms flagged needs_update)
# --------------------------------------------------------------------------- #
