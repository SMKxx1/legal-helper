"""HTTP tests for POST /v1/support_task/generate-nda — the Phase-1 security guards (entitlement gate,
OOXML PK-magic check, empty/bounded read) + the success path. The route was previously untested, so the
guards could silently regress. The engine is unconfigured in tests, so engine_principal binds the open
svc:local dev principal (full entitlements) — except where we override it to test the entitlement gate.

Ported from nda-review-cloud verbatim EXCEPT:
  * DELETED ``test_generate_nda_signed_principal_requires_body_binding`` — it exercised the retired
    SIGNED-principal (X-Principal-*) body-binding plane, which does not exist in this engine.
  * ``test_generate_nda_requires_an_engine_entitlement`` no longer does ``from app.main import app``
    (this engine has only a ``create_app`` factory, no module-level singleton) — it takes the shared
    ``app`` fixture (the same object the ``client`` fixture serves) and overrides ``engine_principal``
    on it.
"""

from __future__ import annotations

import io

from docx import Document

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(text: str = "Party: {{party}}.") -> bytes:
    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _post(
    client, *, values='{"party": "Acme Corp"}', file=("nda.docx", None, _DOCX_MIME)
):
    name, body, mime = file
    body = _docx_bytes() if body is None else body
    return client.post(
        "/v1/support_task/generate-nda",
        data={"values": values},
        files={"file": (name, io.BytesIO(body), mime)},
    )


def test_generate_nda_success_returns_a_filled_docx(client):
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.content[:2] == b"PK"  # a real OOXML .docx (zip container)


def test_generate_nda_rejects_non_docx_upload(client):
    # The OOXML PK-magic guard: a .txt (non-zip) upload -> clean 400, not a 500 from docx.Document.
    resp = _post(
        client, values="{}", file=("nda.txt", b"this is not a docx", "text/plain")
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_generate_nda_rejects_empty_file(client):
    resp = _post(
        client, values="{}", file=("nda.docx", b"", "application/octet-stream")
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_generate_nda_rejects_bad_values_json(client):
    resp = _post(client, values="not json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_generate_nda_requires_an_engine_entitlement(client, app):
    # A principal with NO engine entitlements (read-only viewer equivalent) must be rejected 403,
    # mirroring the /v1/reviews + /v1/redline authz. (svc:local has full entitlements, so override it.)
    # ADAPTED: the source did ``from app.main import app``; this engine has no module-level app
    # singleton, so we take the ``app`` fixture (the exact object ``client`` serves) and override on it.
    from app.auth.principal import ResolvedPrincipal, engine_principal
    from app.schemas import DEFAULT_ORG_ID

    app.dependency_overrides[engine_principal] = lambda: ResolvedPrincipal(
        principal_type="service",
        principal_id="noent",
        org_id=DEFAULT_ORG_ID,
        entitlements=frozenset(),
    )
    try:
        resp = _post(client, values="{}")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "not_entitled"
    finally:
        app.dependency_overrides.pop(engine_principal, None)
