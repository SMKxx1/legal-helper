"""DocuSign integration tests (PLAN §3.9) — fake httpx transport, ZERO network.

Covers the four deliverable areas: JWT-grant assertion shape + token caching, the envelope request
body / routing / cc golden, the ported idempotency-key derivation golden, idempotent
``save_envelope_attempt``, the error taxonomy matrix, and the disabled-capability path. Plus the
``nda_envelopes`` schema (create_all) and the PINNED 0004 revision metadata.

The RSA keypair is generated in-process (``cryptography``) so the RS256 assertion is really signed and
really verifiable, with no fixture key files and no network.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.integrations.docusign import (
    CC_AFTER,
    CC_BEFORE,
    EMAIL_SUBJECT,
    ROUTING_ALL_AT_ONCE,
    ROUTING_AMP_FIRST,
    ROUTING_CP_FIRST,
    DocuSignAuthError,
    DocuSignClient,
    DocuSignRetryableError,
    DocuSignTerminalError,
    DocuSignUnavailable,
    build_docusign_client,
    build_recipients,
    cc_routing_order,
    derive_idempotency_key,
    signer_name_from_email,
    signer_routing_orders,
)

# Persistence tests use the frozen conftest.py ``db`` fixture: its ``session_factory`` create_all now
# builds ``nda_envelopes`` too (importing ``app.models`` registers it via app.bot -> app.integrations),
# so no bespoke session fixture is needed here.

_TWO_SIGNERS = ("alice.smith@example.com", "bob@acme.com")
_DOCX = b"NDA-BYTES-\x00\x01fixture"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    """A real RSA-2048 keypair (PKCS8 PEM private, SubjectPublicKeyInfo PEM public)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


class _Recorder:
    """A recording ``httpx.MockTransport`` for the token + envelope endpoints."""

    def __init__(
        self,
        *,
        token_status: int = 200,
        token_body: dict | None = None,
        envelope_status: int = 201,
        envelope_body: dict | None = None,
        token_exc: Exception | None = None,
        envelope_exc: Exception | None = None,
    ) -> None:
        self.token_requests: list[httpx.Request] = []
        self.envelope_requests: list[httpx.Request] = []
        self.token_status = token_status
        self.token_body = (
            token_body
            if token_body is not None
            else {"access_token": "AT-1", "token_type": "Bearer", "expires_in": 3600}
        )
        self.envelope_status = envelope_status
        self.envelope_body = (
            envelope_body
            if envelope_body is not None
            else {"envelopeId": "env-123", "status": "sent"}
        )
        self.token_exc = token_exc
        self.envelope_exc = envelope_exc

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth/token"):
            self.token_requests.append(request)
            if self.token_exc is not None:
                raise self.token_exc
            return httpx.Response(self.token_status, json=self.token_body)
        if path.endswith("/envelopes"):
            self.envelope_requests.append(request)
            if self.envelope_exc is not None:
                raise self.envelope_exc
            return httpx.Response(self.envelope_status, json=self.envelope_body)
        return httpx.Response(404, json={"errorCode": "NOT_FOUND", "message": path})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


def _client(rsa_keys, recorder: _Recorder, *, clock=time.time, **kw) -> DocuSignClient:
    priv, _ = rsa_keys
    return DocuSignClient(
        base_uri="https://demo.docusign.net",
        oauth_host="account-d.docusign.com",
        account_id="acc-1",
        integration_key="ikey",
        user_id="uid",
        private_key=priv,
        transport=recorder.transport,
        clock=clock,
        **kw,
    )


def _send(client: DocuSignClient, **overrides):
    kw = {"docx_bytes": _DOCX, "filename": "NDA.docx", "signers": _TWO_SIGNERS}
    kw.update(overrides)
    return client.create_and_send_envelope(**kw)


# --------------------------------------------------------------------------- #
# JWT assertion shape + token caching
# --------------------------------------------------------------------------- #
def test_jwt_assertion_shape(rsa_keys) -> None:
    _, pub = rsa_keys
    client = _client(rsa_keys, _Recorder(), clock=lambda: 1_700_000_000.0)
    assertion = client.build_assertion()

    assert jwt.get_unverified_header(assertion)["alg"] == "RS256"
    decoded = jwt.decode(
        assertion,
        pub,
        algorithms=["RS256"],
        audience="account-d.docusign.com",
        options={"verify_exp": False},
    )
    assert decoded["iss"] == "ikey"  # integration key
    assert decoded["sub"] == "uid"  # impersonated user
    assert decoded["aud"] == "account-d.docusign.com"  # oauth host, no scheme
    assert decoded["scope"] == "signature impersonation"
    assert decoded["iat"] == 1_700_000_000
    assert decoded["exp"] == 1_700_000_000 + 3600  # DocuSign's 1h cap


def test_token_minted_once_and_cached_across_sends(rsa_keys) -> None:
    rec = _Recorder()
    client = _client(rsa_keys, rec)
    _send(client)
    _send(client)
    assert len(rec.token_requests) == 1  # cached — not re-minted per send
    assert len(rec.envelope_requests) == 2


def test_token_reminted_after_expiry(rsa_keys) -> None:
    rec = _Recorder(token_body={"access_token": "AT", "expires_in": 100})
    now = {"t": 0.0}
    client = _client(rsa_keys, rec, clock=lambda: now["t"])
    _send(client)  # mint at t=0; cache valid until 0 + 100 - 60 = 40
    now["t"] = 41.0  # past expiry-skew
    _send(client)
    assert len(rec.token_requests) == 2


# --------------------------------------------------------------------------- #
# Envelope request body / routing / cc — golden
# --------------------------------------------------------------------------- #
def test_envelope_request_body_golden(rsa_keys) -> None:
    rec = _Recorder()
    client = _client(rsa_keys, rec)
    result = _send(
        client,
        routing=ROUTING_AMP_FIRST,
        cc=["legal@example.com"],
        cc_timing=CC_AFTER,
    )

    req = rec.envelope_requests[0]
    assert req.url.path == "/restapi/v2.1/accounts/acc-1/envelopes"
    assert req.headers["Authorization"] == "Bearer AT-1"
    body = json.loads(req.content)

    assert body["emailSubject"] == EMAIL_SUBJECT
    assert body["status"] == "sent"
    doc = body["documents"][0]
    assert doc["documentBase64"] == base64.b64encode(_DOCX).decode("ascii")
    assert doc["fileExtension"] == "docx"
    assert doc["documentId"] == "1"

    signers = body["recipients"]["signers"]
    assert [s["email"] for s in signers] == list(_TWO_SIGNERS)
    assert [s["recipientId"] for s in signers] == ["1", "2"]
    assert [s["routingOrder"] for s in signers] == ["1", "2"]  # amp_first
    assert signers[0]["name"] == "alice smith"  # local-part derivation

    ccs = body["recipients"]["carbonCopies"]
    assert ccs[0]["recipientId"] == "3"  # unique index past the signers
    assert ccs[0]["routingOrder"] == "3"  # after the last signer (2) + 1

    # header idempotency key == returned key == derived key
    expected_key = derive_idempotency_key(
        base64.b64encode(_DOCX).decode("ascii"), body["recipients"]
    )
    assert req.headers["X-DocuSign-Idempotency-Key"] == expected_key
    assert result.idempotency_key == expected_key
    assert result.envelope_id == "env-123"
    assert result.status == "sent"


@pytest.mark.parametrize(
    "routing,expected",
    [
        (ROUTING_ALL_AT_ONCE, ["1", "1"]),
        (ROUTING_AMP_FIRST, ["1", "2"]),
        (ROUTING_CP_FIRST, ["2", "1"]),  # cp signs first, amp last
    ],
)
def test_signer_routing_order_golden(routing, expected) -> None:
    recips = build_recipients(["amp@example.com", "cp@acme.com"], routing)
    assert [s["routingOrder"] for s in recips["signers"]] == expected


@pytest.mark.parametrize("timing,expected", [(CC_BEFORE, "1"), (CC_AFTER, "3")])
def test_cc_timing_golden(timing, expected) -> None:
    recips = build_recipients(
        ["amp@example.com", "cp@acme.com"], ROUTING_AMP_FIRST, ["cc@x.com"], timing
    )
    assert recips["carbonCopies"][0]["routingOrder"] == expected


def test_routing_order_helpers_generalize() -> None:
    # Amperesand is index 0; counterparties follow.
    assert signer_routing_orders(ROUTING_ALL_AT_ONCE, 3) == [1, 1, 1]
    assert signer_routing_orders(ROUTING_AMP_FIRST, 3) == [1, 2, 3]
    assert signer_routing_orders(ROUTING_CP_FIRST, 3) == [3, 1, 2]  # amp last
    assert signer_routing_orders(ROUTING_CP_FIRST, 1) == [1]  # lone amp signer
    assert signer_routing_orders("bogus", 2) == [1, 1]  # unknown -> parallel
    # CC after == one past the largest signer order.
    assert cc_routing_order(CC_AFTER, [1, 2, 3]) == 4
    assert cc_routing_order(CC_BEFORE, [1, 2, 3]) == 1


def test_signer_name_derivation() -> None:
    assert signer_name_from_email("alice.smith@example.com") == "alice smith"
    assert signer_name_from_email("bob@acme.com") == "bob"
    assert signer_name_from_email("j_doe-x@z.com") == "j doe x"
    assert (
        signer_name_from_email("@weird.com") == "@weird.com"
    )  # empty local -> fallback


def test_filename_extension_defaults(rsa_keys) -> None:
    rec = _Recorder()
    client = _client(rsa_keys, rec)
    _send(client, filename="agreement.PDF")
    body = json.loads(rec.envelope_requests[0].content)
    assert body["documents"][0]["fileExtension"] == "pdf"  # lower-cased from the name
    _send(client, filename="noext")
    body2 = json.loads(rec.envelope_requests[1].content)
    assert body2["documents"][0]["fileExtension"] == "docx"  # default


# --------------------------------------------------------------------------- #
# Idempotency-key derivation — golden vs the reference rule
# --------------------------------------------------------------------------- #
def test_idempotency_key_matches_reference_rule() -> None:
    b64 = base64.b64encode(_DOCX).decode("ascii")
    recips = build_recipients(
        list(_TWO_SIGNERS), ROUTING_AMP_FIRST, ["legal@example.com"], CC_AFTER
    )
    # Independent replication of the ported rule: sha1(b64 + "|" + JSON(recipients))[:40], with
    # compact JSON (JS JSON.stringify parity).
    material = b64 + "|" + json.dumps(recips, separators=(",", ":"))
    expected = hashlib.sha1(material.encode("utf-8")).hexdigest()[:40]  # noqa: S324
    assert derive_idempotency_key(b64, recips) == expected
    # Frozen golden (recomputation guards against silent construction drift).
    assert (
        derive_idempotency_key(b64, recips)
        == "1fbeca32a4a5402ca262411b5dca1d01e057f5d2"
    )


def test_idempotency_key_stable_and_recipient_sensitive() -> None:
    b64 = base64.b64encode(_DOCX).decode("ascii")
    a = build_recipients(list(_TWO_SIGNERS), ROUTING_AMP_FIRST)
    b = build_recipients(
        list(_TWO_SIGNERS), ROUTING_CP_FIRST
    )  # different routing order
    assert derive_idempotency_key(b64, a) == derive_idempotency_key(b64, a)  # stable
    assert derive_idempotency_key(b64, a) != derive_idempotency_key(
        b64, b
    )  # order matters
    assert len(derive_idempotency_key(b64, a)) == 40


# --------------------------------------------------------------------------- #
# save_envelope_attempt — idempotent persistence (models + create_all schema)
# --------------------------------------------------------------------------- #
def test_save_envelope_attempt_idempotent(db) -> None:
    from sqlalchemy import func, select

    from app.integrations.models import NdaEnvelope, save_envelope_attempt

    first = save_envelope_attempt(
        db,
        idempotency_key="K1",
        status="sent",
        channel="slack",
        routing="amp_first",
        requested_by="U-REQ",
        signer_emails=["a@example.com", "b@acme.com"],
        cc_emails=["c@x.com"],
        envelope_id="env-1",
        slack_channel="C1",
        slack_thread_ts="123.45",
    )
    # A redelivered send with the SAME key must NOT create a second row or overwrite the first.
    second = save_envelope_attempt(
        db,
        idempotency_key="K1",
        status="failed",
        channel="slack",
        routing="all_at_once",
        envelope_id=None,
    )
    assert second.id == first.id
    assert second.envelope_id == "env-1"  # first-writer-wins
    assert second.status == "sent"
    count = db.execute(select(func.count()).select_from(NdaEnvelope)).scalar_one()
    assert count == 1
    # JSON lists round-trip natively.
    assert first.signer_emails == ["a@example.com", "b@acme.com"]
    assert first.cc_emails == ["c@x.com"]
    assert first.requested_by == "U-REQ"


def test_save_failed_attempt_persists(db) -> None:
    from app.integrations.models import save_envelope_attempt

    row = save_envelope_attempt(
        db,
        idempotency_key="F1",
        status="failed",
        channel="email",
        routing="all_at_once",
        email_message_id="<mid@example.com>",
        signer_emails=["a@example.com"],
        cc_emails=[],
    )
    assert row.envelope_id is None
    assert row.status == "failed"
    assert row.email_message_id == "<mid@example.com>"


def test_nda_envelopes_in_create_all_schema(tmp_path) -> None:
    from sqlalchemy import create_engine, inspect

    import app.models  # noqa: F401  registers nda_envelopes (via app.bot -> app.integrations)
    from app.db import Base

    eng = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    Base.metadata.create_all(eng)
    insp = inspect(eng)
    assert "nda_envelopes" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("nda_envelopes")}
    assert {
        "id",
        "envelope_id",
        "idempotency_key",
        "status",
        "channel",
        "slack_channel",
        "slack_thread_ts",
        "email_message_id",
        "requested_by",
        "signer_emails",
        "cc_emails",
        "routing",
        "created_at",
    } <= cols
    uniques = insp.get_unique_constraints("nda_envelopes")
    assert any(u["column_names"] == ["idempotency_key"] for u in uniques)
    eng.dispose()


# --------------------------------------------------------------------------- #
# Error taxonomy matrix
# --------------------------------------------------------------------------- #
def test_envelope_4xx_terminal_surfaces_error_code(rsa_keys) -> None:
    rec = _Recorder(
        envelope_status=400,
        envelope_body={
            "errorCode": "INVALID_EMAIL_ADDRESS_FOR_RECIPIENT",
            "message": "bad recipient email",
        },
    )
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignTerminalError) as ei:
        _send(client)
    assert ei.value.status_code == 400
    assert ei.value.error_code == "INVALID_EMAIL_ADDRESS_FOR_RECIPIENT"
    assert "bad recipient email" in str(ei.value)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_envelope_5xx_and_429_retryable(rsa_keys, status) -> None:
    rec = _Recorder(
        envelope_status=status, envelope_body={"errorCode": "X", "message": "down"}
    )
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignRetryableError):
        _send(client)


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("no route")]
)
def test_envelope_transport_failures_retryable(rsa_keys, exc) -> None:
    rec = _Recorder(envelope_exc=exc)
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignRetryableError):
        _send(client)


def test_envelope_2xx_without_sent_status_is_terminal(rsa_keys) -> None:
    rec = _Recorder(envelope_body={"envelopeId": "e-1", "status": "created"})
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignTerminalError):
        _send(client)


def test_envelope_2xx_without_envelope_id_is_terminal(rsa_keys) -> None:
    rec = _Recorder(envelope_body={"status": "sent"})  # no envelopeId
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignTerminalError):
        _send(client)


def test_token_consent_required_is_terminal_auth_error(rsa_keys) -> None:
    rec = _Recorder(
        token_status=400,
        token_body={"error": "consent_required", "error_description": "grant consent"},
    )
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignAuthError) as ei:
        _send(client)
    assert ei.value.error_code == "consent_required"
    assert "consent" in str(ei.value).lower()
    assert len(rec.envelope_requests) == 0  # never reached the envelope call


def test_token_5xx_is_retryable(rsa_keys) -> None:
    rec = _Recorder(token_status=502, token_body={"error": "bad_gateway"})
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignRetryableError):
        _send(client)


def test_token_timeout_is_retryable(rsa_keys) -> None:
    rec = _Recorder(token_exc=httpx.ConnectTimeout("slow"))
    client = _client(rsa_keys, rec)
    with pytest.raises(DocuSignRetryableError):
        _send(client)


def test_empty_signers_rejected(rsa_keys) -> None:
    client = _client(rsa_keys, _Recorder())
    with pytest.raises(DocuSignTerminalError):
        client.create_and_send_envelope(docx_bytes=_DOCX, filename="a.docx", signers=[])


# --------------------------------------------------------------------------- #
# Capability gate (read-only) — disabled -> typed DocuSignUnavailable
# --------------------------------------------------------------------------- #
def test_disabled_capability_raises_unavailable() -> None:
    from app.capabilities import DOCUSIGN, CapabilityState, build_registry
    from app.config import Settings

    settings = Settings(_env_file=None)  # no docusign_* -> capability DISABLED
    registry = build_registry(settings)
    assert registry.state(DOCUSIGN) is CapabilityState.DISABLED
    with pytest.raises(DocuSignUnavailable):
        build_docusign_client(settings, registry)


def test_unhealthy_capability_raises_unavailable(rsa_keys) -> None:
    from app.capabilities import DOCUSIGN, CapabilityState, build_registry
    from app.config import Settings

    priv, _ = rsa_keys
    settings = Settings(
        _env_file=None,
        docusign_account_id="acc",
        docusign_integration_key="ik",
        docusign_user_id="uid",
        docusign_private_key=priv,
    )
    registry = build_registry(settings)
    assert registry.state(DOCUSIGN) is CapabilityState.ENABLED
    registry.mark_unhealthy(DOCUSIGN, "provider probe failed")
    assert registry.state(DOCUSIGN) is CapabilityState.UNHEALTHY
    with pytest.raises(DocuSignUnavailable):
        build_docusign_client(settings, registry)


def test_enabled_capability_builds_client(rsa_keys) -> None:
    from app.capabilities import DOCUSIGN, CapabilityState, build_registry
    from app.config import Settings

    priv, _ = rsa_keys
    settings = Settings(
        _env_file=None,
        docusign_account_id="acc",
        docusign_integration_key="ik",
        docusign_user_id="uid",
        docusign_private_key=priv,
    )
    registry = build_registry(settings)
    assert registry.state(DOCUSIGN) is CapabilityState.ENABLED
    client = build_docusign_client(settings, registry)  # no network call made
    assert isinstance(client, DocuSignClient)
    client.close()


def test_no_registry_skips_gate(rsa_keys) -> None:
    from app.config import Settings

    priv, _ = rsa_keys
    settings = Settings(_env_file=None, docusign_private_key=priv)
    client = build_docusign_client(settings, None)  # gate skipped for direct unit use
    assert isinstance(client, DocuSignClient)
    client.close()


# --------------------------------------------------------------------------- #
# Migration 0004 — PINNED revision metadata + (chain-complete) head check
# --------------------------------------------------------------------------- #
_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"
# Match a real ``revision = "0003_forms"`` declaration — the negative lookbehind excludes the
# ``down_revision = "0003_forms"`` line in THIS agent's 0004 file (whose "revision" substring would
# otherwise falsely signal that the forms migration is present).
_REV_0003_RE = re.compile(
    r"""(?<!\w)revision(?:\s*:[^=\n]+)?\s*=\s*['"]0003_forms['"]"""
)


def _forms_migration_present() -> bool:
    return any(_REV_0003_RE.search(p.read_text()) for p in _VERSIONS_DIR.glob("*.py"))


def test_0004_revision_metadata_pinned() -> None:
    """The 0004 revision id + down_revision are exactly as assigned (loaded straight from the file,
    so this never depends on the cross-agent chain resolving)."""
    path = _VERSIONS_DIR / "0004_envelopes.py"
    spec = importlib.util.spec_from_file_location("_mig_0004", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0004_envelopes"
    assert module.down_revision == "0003_forms"  # pinned to the forms agent's revision


def test_0004_creates_nda_envelopes_at_head_when_chain_complete(
    tmp_path, monkeypatch
) -> None:
    """When the forms migration (0003) has landed, ``upgrade head`` builds ``nda_envelopes``.

    Skipped while 0003 is absent (parallel P3 authoring): the dangling down_revision makes the whole
    chain unresolvable, which the existing tests/test_migrations* already surface — this test asserts
    0004's own effect once the chain is complete, without duplicating that expected-failure noise.
    """
    if not _forms_migration_present():
        pytest.skip("forms migration 0003_forms not yet landed (parallel P3 wave)")

    from sqlalchemy import create_engine, inspect

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "head.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    command.upgrade(db_migrate._alembic_config(), "head")
    eng = create_engine(f"sqlite:///{db}")
    assert "nda_envelopes" in set(inspect(eng).get_table_names())
    eng.dispose()
