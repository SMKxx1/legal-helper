"""Characterization tests for the ``/v1`` review-engine API GATE/validation layer.

The ``/v1`` endpoints (``app.api.routes_v1``) wrap the provider-neutral review engine,
which calls Anthropic. These tests deliberately exercise ONLY the auth + request-validation
paths that resolve BEFORE any provider call:

  * the HARD engine gate (``engine_principal``): a configured engine rejects a missing/unknown
    ``X-API-Key`` with 401; a signed-header request with no signing key configured gets 503;
  * the per-action entitlement gate: a web ``viewer`` session is not entitled to spend engine
    budget -> 403;
  * the static request-validation gates raised in the route before the file is even read
    (``mode`` / ``scope`` / redlines-suffix -> 400) and the cheap pre-provider guards
    (empty upload -> 400, unsupported file type -> 415, FastAPI's required-file -> 422).

NONE of these reach ``_run_engine`` / Anthropic. The error envelope is asserted on every
EngineError path: ``{"error": {"code", "message", "details"}}`` with the raised HTTP status.

To make the auth gate deterministic we CONFIGURE the engine (set ``settings.engine_api_key``):
with no key configured at all the engine binds an open ``svc:local`` dev principal (fail-open
for local dev), so a missing-key request would proceed toward the provider rather than 401.
"""

from __future__ import annotations

import io

import pytest

API_KEY = "test-engine-key"


def _txt_file(name: str = "doc.txt", body: bytes = b"some contract text"):
    """A minimal multipart file part for ``POST /v1/reviews``."""
    return {"file": (name, io.BytesIO(body), "text/plain")}


def _assert_envelope(resp, *, status: int, code: str):
    """Assert the standard handled-error envelope shape + values."""
    assert resp.status_code == status
    body = resp.json()
    assert set(body.keys()) == {"error"}
    err = body["error"]
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["details"], dict)


@pytest.fixture
def configured_engine(client, monkeypatch):
    """Make the engine 'configured' so an unauthenticated /v1 call is rejected (401),
    not silently bound to the open svc:local dev principal. Returns the live client."""
    from app.config import settings

    monkeypatch.setattr(settings, "engine_api_key", API_KEY, raising=False)
    monkeypatch.setattr(settings, "engine_service_keys", "", raising=False)
    return client


# --------------------------------------------------------------------------- #
# Auth gate: a CONFIGURED engine rejects a missing / unknown X-API-Key (401).
# --------------------------------------------------------------------------- #
def test_create_review_missing_api_key_is_401(configured_engine):
    resp = configured_engine.post(
        "/v1/reviews", files=_txt_file(), data={"mode": "quick"}
    )
    _assert_envelope(resp, status=401, code="unauthorized")


def test_create_review_unknown_api_key_is_401(configured_engine):
    resp = configured_engine.post(
        "/v1/reviews",
        files=_txt_file(),
        data={"mode": "quick"},
        headers={"x-api-key": "not-the-configured-key"},
    )
    _assert_envelope(resp, status=401, code="unauthorized")


def test_list_reviews_missing_api_key_is_401(configured_engine):
    # The GET history endpoint is gated by the same engine_principal dependency.
    resp = configured_engine.get("/v1/reviews")
    _assert_envelope(resp, status=401, code="unauthorized")


def test_get_review_missing_api_key_is_401(configured_engine):
    resp = configured_engine.get("/v1/reviews/" + "a" * 32)
    _assert_envelope(resp, status=401, code="unauthorized")


def test_redline_missing_api_key_is_401(configured_engine):
    resp = configured_engine.post("/v1/redline", data={"review_id": "a" * 32})
    _assert_envelope(resp, status=401, code="unauthorized")


# --------------------------------------------------------------------------- #
# (Retired) Signed-principal gate: the source had
# ``test_signed_headers_without_configured_hmac_key_is_503`` here, exercising the
# SIGNED X-Principal-* plane (settings.auth_principal_hmac_key + principal_sig.verify).
# That plane is RETIRED in this engine (no app/auth/principal_sig.py), so the test was
# deleted during the port. See ``skipped``.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Entitlement gate: a web 'viewer' is read-only and may not spend engine budget.
# Resolves via the session cookie (no X-API-Key); never reaches the provider.
# --------------------------------------------------------------------------- #
def test_viewer_cannot_create_review_403(client, seed_user, login):
    seed_user("vic", role="viewer")
    assert login("vic").status_code == 200
    resp = client.post("/v1/reviews", files=_txt_file(), data={"mode": "quick"})
    _assert_envelope(resp, status=403, code="not_entitled")
    assert resp.json()["error"]["details"].get("action") == "review.quick"


def test_viewer_cannot_redline_403(client, seed_user, login):
    seed_user("vic", role="viewer")
    assert login("vic").status_code == 200
    resp = client.post("/v1/redline", data={"review_id": "a" * 32})
    _assert_envelope(resp, status=403, code="not_entitled")
    assert resp.json()["error"]["details"].get("action") == "redline"


# --------------------------------------------------------------------------- #
# Static request-validation gates: raised in the route BEFORE the file is read,
# so an authenticated caller (valid X-API-Key) still gets 400 — no provider call.
# --------------------------------------------------------------------------- #
def test_invalid_mode_is_400(configured_engine):
    resp = configured_engine.post(
        "/v1/reviews",
        files=_txt_file(),
        data={"mode": "ultra"},  # only 'quick' | 'deep' are valid
        headers={"x-api-key": API_KEY},
    )
    _assert_envelope(resp, status=400, code="bad_request")
    assert "mode" in resp.json()["error"]["message"]


def test_invalid_scope_is_400(configured_engine):
    resp = configured_engine.post(
        "/v1/reviews",
        files=_txt_file(),
        data={"mode": "quick", "scope": "sideways"},  # only 'whole' | 'redlines'
        headers={"x-api-key": API_KEY},
    )
    _assert_envelope(resp, status=400, code="bad_request")
    assert "scope" in resp.json()["error"]["message"]


def test_redlines_scope_requires_docx_is_400(configured_engine):
    # scope='redlines' reconstructs original-vs-accepted from OOXML tracked changes -> .docx only.
    resp = configured_engine.post(
        "/v1/reviews",
        files=_txt_file("doc.txt"),
        data={"mode": "quick", "scope": "redlines"},
        headers={"x-api-key": API_KEY},
    )
    _assert_envelope(resp, status=400, code="bad_request")
    assert ".docx" in resp.json()["error"]["message"]


def test_empty_upload_is_400(configured_engine):
    resp = configured_engine.post(
        "/v1/reviews",
        files=_txt_file("doc.txt", b""),  # zero bytes
        data={"mode": "quick"},
        headers={"x-api-key": API_KEY},
    )
    _assert_envelope(resp, status=400, code="bad_request")


def test_unsupported_file_type_is_415(configured_engine):
    # An authenticated caller uploading a disallowed suffix is rejected in _extract_text,
    # before parsing or any provider call. force=true skips the cache lookups so this stays
    # a pure type-guard assertion.
    resp = configured_engine.post(
        "/v1/reviews",
        files=_txt_file("malware.exe", b"MZ\x00\x00not a document"),
        data={"mode": "quick", "force": "true"},
        headers={"x-api-key": API_KEY},
    )
    _assert_envelope(resp, status=415, code="unsupported_media_type")


def test_missing_file_part_is_422_unified_envelope(configured_engine):
    # The `file` form field is required (File(...)). A missing required field now surfaces the SAME
    # unified {"error": {code, message, details}} envelope as every other error path (via the
    # RequestValidationError handler) — with the submitted input STRIPPED from the details.
    resp = configured_engine.post(
        "/v1/reviews",
        data={"mode": "quick"},
        headers={"x-api-key": API_KEY},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "unprocessable"
    errors = body["error"]["details"]["errors"]
    assert any(e.get("loc", [])[-1:] == ["file"] for e in errors)
    # Defense-in-depth: the submitted payload is never reflected back in a 422.
    assert all("input" not in e for e in errors)


def test_keyless_request_401s_when_db_service_keys_exist(client, db):
    # DB-keys-only deployment (no env keys): an ACTIVE ServiceAccountKey means the engine IS
    # configured, so a KEYLESS request must 401 — NOT fall through to the open svc:local dev
    # principal with full entitlements (the fail-open the re-audit found).
    from app.auth.models import Org, ServiceAccountKey
    from app.schemas import DEFAULT_ORG_ID

    if db.get(Org, DEFAULT_ORG_ID) is None:
        db.add(Org(id=DEFAULT_ORG_ID, name="Org"))
    db.add(
        ServiceAccountKey(
            org_id=DEFAULT_ORG_ID, key_hash="a" * 64, principal_id="svc", active=True
        )
    )
    db.commit()

    resp = client.post("/v1/reviews", data={"mode": "quick"}, files=_txt_file())
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_redline_docx_non_uuid_id_is_404(client, seed_user, login):
    # redline.docx explicitly rejects anything that isn't a uuid4-hex review id BEFORE any
    # build/lookup, so a traversal-ish path can never reach the filesystem. Authenticated via
    # a reviewer session so the gate under test is the id-shape guard, not auth.
    seed_user("rita", role="reviewer")
    assert login("rita").status_code == 200
    resp = client.get("/v1/reviews/not-a-valid-id/redline.docx")
    _assert_envelope(resp, status=404, code="not_found")
