"""Airtable expiration tracker — upsert shape, capability gate, error taxonomy, list pagination.

NO network: every call rides ``httpx.MockTransport``. Pins the PLAN §3.10/§6 contract: native upsert
keyed on the file-reference field, MINIMAL fields (display name + date), fail-soft capability gate, and
the retryable/terminal taxonomy.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.capabilities import build_registry
from app.config import Settings
from app.integrations.airtable import (
    FIELD_DISPLAY_NAME,
    FIELD_EXPIRATION_DATE,
    FIELD_FILE_REF,
    AirtableClient,
    AirtableRetryableError,
    AirtableTerminalError,
    AirtableUnavailable,
    build_airtable_client,
    build_expiration_fields,
    upsert_expiration,
)


def mk_settings(configured: bool = True, **kw) -> Settings:
    base: dict = {}
    if configured:
        base = {
            "airtable_pat": "pat_test",
            "airtable_base_id": "appTEST",
            "airtable_table": "Expirations",
        }
    base.update(kw)
    return Settings(_env_file=None, **base)


def capture(responses: list):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    return handler, seen


def client(
    responses: list, *, settings: Settings | None = None
) -> tuple[AirtableClient, list]:
    handler, seen = capture(responses)
    settings = settings or mk_settings()
    c = build_airtable_client(settings, transport=httpx.MockTransport(handler))
    return c, seen


# --------------------------------------------------------------------------- #
# Upsert shape
# --------------------------------------------------------------------------- #
def test_upsert_expiration_shape() -> None:
    rec = {"id": "recABC", "fields": {FIELD_FILE_REF: "file123"}}
    c, seen = client([{"records": [rec]}])
    fields = build_expiration_fields("ACME NDA (signed).pdf", "2027-01-15")
    out = c.upsert_expiration("file123", fields)
    assert out == rec

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "PATCH"
    assert req.url.path == "/v0/appTEST/Expirations"
    assert req.headers["authorization"] == "Bearer pat_test"
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content.decode())
    assert body["performUpsert"] == {"fieldsToMergeOn": [FIELD_FILE_REF]}
    assert body["typecast"] is True
    sent = body["records"][0]["fields"]
    # The merge key is injected from file_ref; the display name + date are the minimal payload.
    assert sent[FIELD_FILE_REF] == "file123"
    assert sent[FIELD_DISPLAY_NAME] == "ACME NDA (signed).pdf"
    assert sent[FIELD_EXPIRATION_DATE] == "2027-01-15"
    # MINIMAL: exactly the three fields, nothing else (no party payloads — PLAN §6).
    assert set(sent) == {FIELD_FILE_REF, FIELD_DISPLAY_NAME, FIELD_EXPIRATION_DATE}


def test_build_expiration_fields_is_minimal_and_excludes_merge_key() -> None:
    fields = build_expiration_fields("name.pdf", "2030-06-01")
    assert fields == {
        FIELD_DISPLAY_NAME: "name.pdf",
        FIELD_EXPIRATION_DATE: "2030-06-01",
    }
    assert FIELD_FILE_REF not in fields  # injected by the client, not here


def test_table_name_with_spaces_is_url_encoded() -> None:
    c, seen = client(
        [{"records": [{"id": "r1"}]}],
        settings=mk_settings(airtable_table="NDA Expirations"),
    )
    c.upsert_expiration("f1", build_expiration_fields("n", "2027-01-01"))
    assert (
        seen[0].url.path == "/v0/appTEST/NDA Expirations"
    )  # httpx decodes %20 in .path
    assert "NDA%20Expirations" in str(seen[0].url)


def test_module_upsert_expiration_convenience() -> None:
    handler, seen = capture([{"records": [{"id": "recZ"}]}])
    out = upsert_expiration(
        "file9",
        build_expiration_fields("x.pdf", "2028-02-02"),
        settings=mk_settings(),
        transport=httpx.MockTransport(handler),
    )
    assert out == {"id": "recZ"}
    assert seen[0].method == "PATCH"


# --------------------------------------------------------------------------- #
# Capability gate (fail soft)
# --------------------------------------------------------------------------- #
def test_disabled_capability_raises_unavailable_via_config() -> None:
    with pytest.raises(AirtableUnavailable):
        build_airtable_client(mk_settings(configured=False))


def test_disabled_capability_raises_unavailable_via_registry() -> None:
    registry = build_registry(mk_settings(configured=False))
    with pytest.raises(AirtableUnavailable):
        build_airtable_client(mk_settings(), registry)


def test_module_upsert_raises_unavailable_when_off() -> None:
    with pytest.raises(AirtableUnavailable):
        upsert_expiration("f", {"a": 1}, settings=mk_settings(configured=False))


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
def test_429_is_retryable() -> None:
    c, _ = client([httpx.Response(429, json={"error": {"type": "RATE_LIMIT"}})])
    with pytest.raises(AirtableRetryableError):
        c.upsert_expiration("f", build_expiration_fields("n", "2027-01-01"))


def test_500_is_retryable() -> None:
    c, _ = client([httpx.Response(503, text="upstream down")])
    with pytest.raises(AirtableRetryableError):
        c.upsert_expiration("f", build_expiration_fields("n", "2027-01-01"))


def test_422_unknown_field_is_terminal_with_type() -> None:
    c, _ = client(
        [
            httpx.Response(
                422,
                json={
                    "error": {
                        "type": "INVALID_REQUEST_UNKNOWN",
                        "message": "Unknown field name",
                    }
                },
            )
        ]
    )
    with pytest.raises(AirtableTerminalError) as ei:
        c.upsert_expiration("f", build_expiration_fields("n", "2027-01-01"))
    assert ei.value.status_code == 422
    assert ei.value.error_type == "INVALID_REQUEST_UNKNOWN"


def test_timeout_is_retryable() -> None:
    c, _ = client([httpx.ReadTimeout("slow")])
    with pytest.raises(AirtableRetryableError):
        c.upsert_expiration("f", build_expiration_fields("n", "2027-01-01"))


# --------------------------------------------------------------------------- #
# list_tracked — the sweep's dedup source (with pagination)
# --------------------------------------------------------------------------- #
def test_list_tracked_paginates_and_skips_refless_rows() -> None:
    page1 = {
        "records": [
            {
                "id": "r1",
                "fields": {
                    FIELD_FILE_REF: "fileA",
                    FIELD_EXPIRATION_DATE: "2027-01-01",
                },
            },
            {
                "id": "r2",
                "fields": {FIELD_EXPIRATION_DATE: "2027-02-02"},
            },  # no file ref -> skipped
        ],
        "offset": "tok1",
    }
    page2 = {
        "records": [
            {
                "id": "r3",
                "fields": {FIELD_FILE_REF: "fileB"},
            },  # tracked but no date yet
        ]
    }
    c, seen = client([page1, page2])
    rows = c.list_tracked()
    assert [(r.file_ref, r.expiration_date) for r in rows] == [
        ("fileA", "2027-01-01"),
        ("fileB", ""),
    ]
    # Two GETs — the second carries the offset.
    assert len(seen) == 2
    assert seen[0].method == "GET"
    assert "offset" not in dict(seen[0].url.params)
    assert dict(seen[1].url.params)["offset"] == "tok1"
    # Only the two relevant fields are selected.
    assert seen[0].url.params.get_list("fields[]") == [
        FIELD_FILE_REF,
        FIELD_EXPIRATION_DATE,
    ]
