"""Ported data-layer models: registration, round-trips, and cross-dialect helpers on SQLite.

These exercise the kept schema end-to-end against a throwaway per-test SQLite DB (no network, no shared
state): identity/org, the /v1 engine-review chain (with the JSONB->JSON payload round-trip), the kept
idempotency table (LargeBinary body), and the seed catalog (templates/tokens/mapping via models_v2 +
refdata + seed_catalog).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.auth.models  # noqa: F401 - register identity/org tables on Base.metadata
import app.models  # noqa: F401 - register core engine tables on Base.metadata
from app.db import Base


@pytest.fixture
def session_factory(tmp_path):
    """A sessionmaker bound to a throwaway SQLite file with all ORM tables created."""
    engine = create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def test_expected_tables_registered_and_retired_dropped():
    tables = set(Base.metadata.tables)
    # A representative slice of the kept schema.
    for kept in (
        "orgs",
        "user_accounts",
        "identity_sessions",
        "service_account_keys",
        "service_account_usage",
        "contracts",
        "engine_reviews",
        "engine_review_jobs",
        "review_events",
        "document_blob",
        "template",
        "template_version",
        "token",
        "token_template",
        "ref_jurisdiction",
        "nda_idempotency_key",
    ):
        assert kept in tables, f"missing kept table: {kept}"
    # The retired SIGNED-plane + n8n-doorway tables must be gone.
    for dropped in (
        "principal_nonces",
        "allowed_accounts",
        "nda_bot_request",
        "nda_bot_event",
        "nda_bot_envelope",
    ):
        assert dropped not in tables, f"retired table still registered: {dropped}"


def test_identity_round_trip(session_factory):
    from app.auth.models import Org, UserAccount
    from app.schemas import DEFAULT_ORG_ID

    with session_factory() as s, s.begin():
        s.add(Org(id=DEFAULT_ORG_ID, name="Amperesand"))
        s.add(
            UserAccount(
                org_id=DEFAULT_ORG_ID,
                user_id="alice",
                password_hash="argon2-placeholder",
                role="reviewer",
            )
        )

    with session_factory() as s:
        user = s.query(UserAccount).filter_by(user_id="alice").one()
        assert user.org_id == DEFAULT_ORG_ID
        assert user.role == "reviewer"
        # server_default=false() booleans land as False, not None.
        assert user.can_view_all_docs is False
        assert user.session_epoch == 0


def test_engine_review_payload_json_round_trip(session_factory):
    """EngineReview.payload_json is JSON_VARIANT (JSONB on PG, JSON on SQLite) — a native dict
    round-trips without manual json.dumps/loads. Also exercises the Contract -> review -> event chain."""
    from app.models import Contract, EngineReview, ReviewEvent
    from app.schemas import DEFAULT_ORG_ID

    payload = {
        "risk_tier": "yellow",
        "findings": [{"title": "Term too long", "severity": "medium"}],
        "coverage": {"absent_required": []},
    }
    with session_factory() as s, s.begin():
        contract = Contract(org_id=DEFAULT_ORG_ID, counterparty_name="Acme")
        s.add(contract)
        s.flush()
        review = EngineReview(
            contract_id=contract.id,
            org_id=DEFAULT_ORG_ID,
            source_channel="api",
            mode="deep",
            risk_tier="yellow",
            doc_sha256="a" * 64,
            payload_json=payload,
        )
        s.add(review)
        s.flush()
        s.add(
            ReviewEvent(
                contract_id=contract.id,
                review_id=review.id,
                org_id=DEFAULT_ORG_ID,
                event_type="reviewed",
            )
        )
        review_id = review.id

    with session_factory() as s:
        got = s.get(EngineReview, review_id)
        assert got is not None
        assert got.payload_json == payload  # native dict, not a JSON string
        assert got.payload_json["findings"][0]["severity"] == "medium"
        assert got.contract.counterparty_name == "Acme"
        assert got.contract.events[0].event_type == "reviewed"


def test_idempotency_key_largebinary_round_trip(session_factory):
    """The kept generate-nda replay table stores the first response bytes verbatim (LargeBinary)."""
    from app.models_bot import NdaIdempotencyKey
    from app.schemas import DEFAULT_ORG_ID

    body = b"PK\x03\x04filled-docx-bytes"
    with session_factory() as s, s.begin():
        s.add(
            NdaIdempotencyKey(
                org_id=DEFAULT_ORG_ID,
                principal_id="svc:default",
                purpose="generate_nda",
                key="req-123",
                response_body=body,
                filename="nda.docx",
            )
        )

    with session_factory() as s:
        row = s.query(NdaIdempotencyKey).filter_by(key="req-123").one()
        assert row.response_body == body
        assert row.filename == "nda.docx"
        assert row.created_at is not None  # server_default=func.now() populated it


def test_seed_catalog_produces_eight_templates_and_sixteen_tokens(session_factory):
    """seed_lookups + seed_templates_tokens exercise refdata + models_v2 + the token_template mapping."""
    from sqlalchemy import func

    from app.models_v2 import Template, Token, TokenTemplate
    from app.refdata import seed_lookups
    from app.schemas import DEFAULT_ORG_ID
    from app.seed_catalog import seed_templates_tokens

    with session_factory() as s, s.begin():
        seed_lookups(s)
        s.flush()
        seed_templates_tokens(s, DEFAULT_ORG_ID)

    with session_factory() as s:
        assert s.query(func.count(Template.id)).scalar() == 8
        assert s.query(func.count(Token.id)).scalar() == 16
        # token_template is materialized from each token's scope rule; a non-empty mapping proves the
        # cross-join + scope logic ran and the FKs resolved.
        assert s.query(func.count()).select_from(TokenTemplate).scalar() > 0


def test_bigint_variant_and_uuid_defaults(session_factory):
    """Hex-UUID PK defaults fire on insert (cross-dialect convention)."""
    from app.models_v2 import DocumentBlob

    with session_factory() as s, s.begin():
        blob = DocumentBlob(sha256="b" * 64, byte_size=10, mime_type="application/pdf")
        s.add(blob)
        s.flush()
        assert blob.id and len(blob.id) == 32  # uuid4().hex default


def test_all_tables_have_a_columns(session_factory):
    """Smoke check that create_all actually built every registered table with columns."""
    engine = session_factory.kw["bind"]
    insp = inspect(engine)
    for name in Base.metadata.tables:
        assert insp.get_columns(name), f"table {name} created with no columns"
