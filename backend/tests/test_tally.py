"""Tally intake — signature verification, submission→token mapping, requester routing, webhook dedup.

Field-mapping cases mirror the live "NDA Generator" form's branches (Company / Individual /
ServiceProvider × US/SG × Mutual/Unilateral); the mapping table itself was validated against all 27
real submissions. Payloads here are shaped like the Tally
``FORM_RESPONSE`` webhook: ``data.fields[]`` of ``{key,label,type,value,options?}``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from app.integrations.models import TallySubmission, claim_tally_submission
from app.integrations.tally import (
    map_submission,
    mint_routing_token,
    origin_context,
    verify_routing_token,
    verify_signature,
)

pytest_plugins = ("conftest_bot",)


def _sign(secret: str, body: bytes) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


def _text(label: str, value: str, key: str | None = None, type_: str = "INPUT_TEXT"):
    return {"key": key or label, "label": label, "type": type_, "value": value}


def _dropdown(label: str, text: str, key: str | None = None):
    return {
        "key": key or label,
        "label": label,
        "type": "DROPDOWN",
        "value": ["opt1"],
        "options": [{"id": "opt1", "text": text}, {"id": "opt2", "text": "other"}],
    }


def _payload(fields: list[dict], submission_id: str = "sub-1") -> dict:
    return {
        "eventType": "FORM_RESPONSE",
        "data": {"submissionId": submission_id, "formId": "jagDPJ", "fields": fields},
    }


# --------------------------------------------------------------------------- #
# Signature
# --------------------------------------------------------------------------- #
def test_verify_signature_roundtrip() -> None:
    body = b'{"hello":"world"}'
    secret = "top-secret"
    assert verify_signature(secret, body, _sign(secret, body)) is True


def test_verify_signature_rejects_tampered_body() -> None:
    secret = "top-secret"
    sig = _sign(secret, b"original")
    assert verify_signature(secret, b"tampered", sig) is False


def test_verify_signature_fails_closed_on_missing_secret_or_header() -> None:
    body = b"x"
    assert verify_signature("", body, _sign("k", body)) is False
    assert verify_signature("k", body, None) is False
    assert verify_signature("k", body, "") is False


# --------------------------------------------------------------------------- #
# map_submission
# --------------------------------------------------------------------------- #
def test_map_company_us_email() -> None:
    fields = [
        _dropdown("US / SG", "US"),
        _dropdown("Counterparty Entitity Type", "Company"),
        _text("Party B legal entity name", "Acme Corp"),
        _text("Jurisdiction of incorporation", "Delaware"),
        _text("Company registration number", "EIN-12"),
        _text("Registered address", "1 Main St"),
        _text("City, state/province", "San Jose, CA"),
        _text("Country, zip/postal code", "USA, 95110"),
        _text("name@company.com", "legal@acme.com", type_="INPUT_EMAIL"),
        _text("Amperesand Signer Full Name", "Alice Tan"),
        _text("Amperesand Signer Title", "Director"),
        _text("Party B Signer Full Name", "Bob Lee"),
        _text("Party B Signer Title", "CEO"),
        _text("Attn: e.g., Jane Doe, General Counsel", "Bob Lee, CEO"),
        {"key": "channel", "type": "HIDDEN_FIELDS", "value": "email||req@corp.com"},
        {"key": "thread_ts", "type": "HIDDEN_FIELDS", "value": ""},
    ]
    m = map_submission(_payload(fields))
    assert m.jurisdiction == "US"
    assert m.counterparty_type == "company"
    assert m.values["counterparty_name"] == "Acme Corp"
    assert m.values["jurisdiction"] == "Delaware"
    assert m.values["counterparty_company_registration_number"] == "EIN-12"
    assert m.values["street_address"] == "1 Main St"
    assert m.values["city_zip"] == "San Jose, CA"
    assert m.values["country"] == "USA, 95110"
    assert m.values["notice_email"] == "legal@acme.com"
    assert m.values["amperesand_signer_name"] == "Alice Tan"
    assert m.values["counterparty_signer_title"] == "CEO"
    assert m.values["attn"] == "Bob Lee, CEO"
    assert m.channel_raw == "email||req@corp.com"


def test_map_individual_sg_slack_hidden_object() -> None:
    fields = [
        _dropdown("US / SG", "SG"),
        _dropdown("Counterparty Entity Type", "Individual"),
        _dropdown("Mutual / Unilateral", "Mutual"),
        _text("Individual full name", "John Smith"),
        _text("Last four digits only", "1234"),
        _text("Street address", "1 Orchard Rd"),
        _text("City, state/province", "Singapore"),
        _text("Country, zip/postal code", "Singapore 238801"),
        _text("name@example.com", "john@example.com", type_="INPUT_EMAIL"),
        _text("Amperesand Signer Full Name", "Sarah Wong"),
        {
            "key": "routing",
            "type": "HIDDEN_FIELDS",
            "value": {"channel": "C0B9CJ5EVUL", "thread_ts": "1700.9"},
        },
    ]
    m = map_submission(_payload(fields))
    assert m.jurisdiction == "SG"
    assert m.counterparty_type == "individual"
    assert m.mutuality == "mutual"
    assert m.values["counterparty_name"] == "John Smith"
    assert m.values["individual_id_number"] == "1234"
    assert m.values["notice_email"] == "john@example.com"
    assert m.channel_raw == "C0B9CJ5EVUL"
    assert m.thread_ts == "1700.9"


def test_map_service_provider_detected_by_description_of_services() -> None:
    fields = [
        _dropdown("US / SG", "SG"),
        _dropdown("Counterparty Entity Type", "Company"),
        _text("Receiving Party legal name", "Bosch Pte Ltd"),
        _text("Description of services", "Cleaning services"),
    ]
    m = map_submission(_payload(fields))
    # "Description of services" answered => ServiceProvider branch, regardless of the entity dropdown.
    assert m.counterparty_type == "service_provider"
    assert m.values["counterparty_name"] == "Bosch Pte Ltd"
    assert m.values["services"] == "Cleaning services"


def test_map_recurring_dropdowns_ignore_unanswered_branch_copies() -> None:
    # The real webhook sends EVERY branch's fields (unanswered ones with value=None), so the routing
    # and per-branch "Purpose of disclosure" dropdowns RECUR. The answered value must not be clobbered
    # by a null copy that appears after it (this is the bug that dropped `purpose` in production).
    fields = [
        _dropdown("US / SG", "SG"),
        _dropdown("Counterparty Entity Type", "Company"),
        {  # US-branch purpose dropdown — unanswered → null, appears BEFORE the answered one
            "key": "p_us",
            "label": "Purpose of disclosure",
            "type": "DROPDOWN",
            "value": None,
            "options": [{"id": "a", "text": "a potential business relationship"}],
        },
        _text("Party B legal entity name", "Bosch"),
        {  # SG-branch purpose dropdown — the ANSWERED one
            "key": "p_sg",
            "label": "Purpose of disclosure",
            "type": "DROPDOWN",
            "value": ["pid"],
            "options": [
                {"id": "pid", "text": "a possible investment by Party B into Party A"}
            ],
        },
        {  # another branch's purpose dropdown — unanswered → null, appears AFTER the answered one
            "key": "p_x",
            "label": "Purpose of disclosure",
            "type": "DROPDOWN",
            "value": None,
            "options": [{"id": "b", "text": "explore an employment relationship"}],
        },
    ]
    m = map_submission(_payload(fields))
    assert m.jurisdiction == "SG"
    assert m.counterparty_type == "company"
    assert m.values["purpose"] == "a possible investment by Party B into Party A"


def test_map_purpose_dropdown_used_when_no_freetext() -> None:
    fields = [
        _dropdown("US / SG", "US"),
        _dropdown("Counterparty Entity Type", "Company"),
        _dropdown(
            "Purpose of disclosure", "a possible investment by Party B into Party A"
        ),
        _text("Party B legal entity name", "Acme"),
    ]
    m = map_submission(_payload(fields))
    assert m.values["purpose"] == "a possible investment by Party B into Party A"


# --------------------------------------------------------------------------- #
# origin_context
# --------------------------------------------------------------------------- #
def test_origin_context_email() -> None:
    assert origin_context("email||a@b.com") == {"channel": "email", "sender": "a@b.com"}


def test_origin_context_slack() -> None:
    assert origin_context("C123", "9.9") == {
        "channel": "slack",
        "slack_channel": "C123",
        "slack_thread_ts": "9.9",
    }


def test_origin_context_empty_or_malformed_is_none() -> None:
    assert origin_context("") is None
    assert origin_context("email||") is None
    assert origin_context("not-a-channel") is None


# --------------------------------------------------------------------------- #
# Webhook dedup
# --------------------------------------------------------------------------- #
def test_claim_tally_submission_dedups(bot_session_factory) -> None:
    with bot_session_factory() as db:
        assert claim_tally_submission(db, submission_id="s1", form_id="jagDPJ") is True
    with bot_session_factory() as db:
        assert claim_tally_submission(db, submission_id="s1", form_id="jagDPJ") is False
    with bot_session_factory() as db:
        assert db.get(TallySubmission, "s1") is not None


def test_claim_blank_submission_id_not_deduped(bot_session_factory) -> None:
    with bot_session_factory() as db:
        assert claim_tally_submission(db, submission_id="") is True


def test_mark_tally_submission_updates_status(bot_session_factory) -> None:
    from app.integrations.models import mark_tally_submission

    with bot_session_factory() as db:
        claim_tally_submission(db, submission_id="s9", form_id="jagDPJ")
    with bot_session_factory() as db:
        mark_tally_submission(db, submission_id="s9", status="delivered")
    with bot_session_factory() as db:
        assert db.get(TallySubmission, "s9").status == "delivered"


# --------------------------------------------------------------------------- #
# Signed routing token — the anti-redirection binding (a respondent must not be
# able to edit the ?channel= URL param to send the NDA to an arbitrary place).
# --------------------------------------------------------------------------- #
def test_routing_token_roundtrip() -> None:
    tok = mint_routing_token("s3cr3t", "email||req@corp.com", "1700.5")
    assert verify_routing_token("s3cr3t", tok) == {
        "channel": "email||req@corp.com",
        "thread_ts": "1700.5",
    }


def test_routing_token_rejects_unsigned_forged_and_wrong_secret() -> None:
    # A raw, respondent-editable value (no signature) is NOT trusted.
    assert verify_routing_token("s3cr3t", "email||attacker@evil.com") is None
    # A tampered token body no longer matches its signature.
    tok = mint_routing_token("s3cr3t", "email||req@corp.com", "")
    body, _, sig = tok.partition(".")
    forged = mint_routing_token("s3cr3t", "email||attacker@evil.com", "").split(".")[0]
    assert verify_routing_token("s3cr3t", f"{forged}.{sig}") is None
    # Minted under a different secret → rejected. Empty token/secret → None.
    assert verify_routing_token("other-secret", tok) is None
    assert verify_routing_token("s3cr3t", "") is None
    assert verify_routing_token("", tok) is None


def test_generate_link_channel_token_roundtrips_email_and_slack() -> None:
    from app.bot.intents.generate import _tally_form_url

    settings = SimpleNamespace(
        tally_base_url="https://tally.so",
        tally_form_id="jagDPJ",
        tally_signing_secret="link-key",
    )
    # Email requester: the minted ?channel= token verifies back to the exact origin.
    env = SimpleNamespace(
        channel="email",
        sender_address="req@corp.com",
        slack_channel="",
        slack_thread_ts="",
    )
    token = parse_qs(urlparse(_tally_form_url(env, settings)).query)["channel"][0]
    assert verify_routing_token("link-key", token) == {
        "channel": "email||req@corp.com",
        "thread_ts": "",
    }
    # Slack requester: channel id + thread survive the round-trip.
    env_s = SimpleNamespace(
        channel="slack",
        sender_address="",
        slack_channel="C0B9CJ5EVUL",
        slack_thread_ts="1700.9",
    )
    token_s = parse_qs(urlparse(_tally_form_url(env_s, settings)).query)["channel"][0]
    assert verify_routing_token("link-key", token_s) == {
        "channel": "C0B9CJ5EVUL",
        "thread_ts": "1700.9",
    }


# --------------------------------------------------------------------------- #
# Webhook end-to-end: only a signed channel routes a reply; a forged raw channel
# is accepted (200) but delivers to NOBODY (no origin) — the redirection is dead.
# --------------------------------------------------------------------------- #
def _post_webhook(client, secret: str, fields: list[dict], submission_id: str):
    body = json.dumps(_payload(fields, submission_id)).encode()
    return client.post(
        "/integrations/tally/webhook",
        content=body,
        headers={
            "tally-signature": _sign(secret, body),
            "content-type": "application/json",
        },
    )


def test_webhook_trusts_only_signed_channel(monkeypatch) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    import app.bot.flows as flows
    from app.api import routes_tally
    from app.bot.flows.generate_completion import CompletionResult
    from app.config import Settings

    captured: list[dict] = []

    def fake_run_generation(**kw):
        captured.append(kw.get("origin_context"))
        return CompletionResult(ok=True, reason="delivered", delivered=True)

    monkeypatch.setattr(flows, "run_generation", fake_run_generation)

    secret = "whsecret"
    settings = Settings(_env_file=None, tally_signing_secret=secret)  # type: ignore[call-arg]
    fastapi_app = FastAPI()
    routes_tally.register(fastapi_app, settings)
    client = TestClient(fastapi_app)

    base = [_dropdown("US / SG", "US"), _text("Party B legal entity name", "Acme")]

    # 1) Bot-issued SIGNED channel token → the reply routes to the intended requester.
    signed = mint_routing_token(secret, "email||req@corp.com", "")
    r1 = _post_webhook(
        client,
        secret,
        base + [{"key": "channel", "type": "HIDDEN_FIELDS", "value": signed}],
        "wsig",
    )
    assert r1.status_code == 200
    assert captured[-1] == {"channel": "email", "sender": "req@corp.com"}

    # 2) Respondent-forged RAW channel → accepted, but NO origin (delivers to nobody).
    r2 = _post_webhook(
        client,
        secret,
        base
        + [
            {
                "key": "channel",
                "type": "HIDDEN_FIELDS",
                "value": "email||attacker@evil.com",
            }
        ],
        "wforge",
    )
    assert r2.status_code == 200
    assert captured[-1] == {}  # origin was None → run_generation got no destination

    # 3) A bad signature is rejected outright.
    bad = client.post(
        "/integrations/tally/webhook",
        content=b"{}",
        headers={"tally-signature": "not-valid", "content-type": "application/json"},
    )
    assert bad.status_code == 401
