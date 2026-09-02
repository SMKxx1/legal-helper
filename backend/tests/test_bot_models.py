"""Bot-core persistence: fail-closed dedup semantics + allowlist/pending/correlation round-trips.

Exercises the four P2 tables against a throwaway per-test SQLite DB (no network, no shared state).
The dedup test is the load-bearing one: the UNIQUE ``event_key`` insert IS the dedup (PLAN §3.3), so a
duplicate must raise ``IntegrityError`` — that is what makes reprocessing impossible (fail CLOSED).
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.bot.models import BotCorrelation, BotInbox, NdaAllowlist, NdaPendingRequest

# Shared bot-test fixtures live in tests/conftest_bot.py (conftest.py is frozen). Registering it as a
# pytest plugin exposes ``bot_session_factory`` to every test below without a module-level import that
# ruff would flag as shadowed by the identically-named fixture parameters (F811).
pytest_plugins = ("conftest_bot",)


def test_bot_inbox_defaults_and_payload_round_trip(bot_session_factory) -> None:
    with bot_session_factory() as s, s.begin():
        row = BotInbox(
            event_key="slack:E1",
            channel="slack",
            payload_json={"text": "review this", "attachments": []},
        )
        s.add(row)

    with bot_session_factory() as s:
        got = s.query(BotInbox).filter_by(event_key="slack:E1").one()
        # status/attempts land on their defaults; the JSON payload round-trips as a native dict.
        assert got.status == "pending"
        assert got.attempts == 0
        assert got.error is None
        assert got.payload_json == {"text": "review this", "attachments": []}
        assert got.created_at is not None
        assert got.updated_at is not None
        assert len(got.id) == 32  # hex-uuid PK default fired


def test_bot_inbox_unique_event_key_is_fail_closed_dedup(bot_session_factory) -> None:
    """The first insert wins; a duplicate event_key can NOT be inserted (dedup fails closed)."""
    with bot_session_factory() as s, s.begin():
        s.add(BotInbox(event_key="slack:DUP", channel="slack", payload_json={}))

    # A second event carrying the same key raises on flush — there is no "assume new" path.
    with bot_session_factory() as s:
        s.add(BotInbox(event_key="slack:DUP", channel="slack", payload_json={}))
        with pytest.raises(IntegrityError):
            s.commit()

    # And exactly one row survived.
    with bot_session_factory() as s:
        assert s.query(BotInbox).filter_by(event_key="slack:DUP").count() == 1


def test_bot_inbox_dedup_helper_pattern(bot_session_factory) -> None:
    """The insert-and-catch pattern a router uses: True == newly-claimed, False == already seen."""

    def claim(factory, event_key: str) -> bool:
        session = factory()
        try:
            session.add(BotInbox(event_key=event_key, channel="email", payload_json={}))
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False
        finally:
            session.close()

    assert claim(bot_session_factory, "email:<m1>") is True
    assert claim(bot_session_factory, "email:<m1>") is False  # duplicate -> not claimed


def test_bot_inbox_status_lifecycle(bot_session_factory) -> None:
    with bot_session_factory() as s, s.begin():
        s.add(BotInbox(event_key="slack:LC", channel="slack", payload_json={}))

    with bot_session_factory() as s, s.begin():
        row = s.query(BotInbox).filter_by(event_key="slack:LC").one()
        row.status = "processing"
        row.attempts = row.attempts + 1

    with bot_session_factory() as s, s.begin():
        row = s.query(BotInbox).filter_by(event_key="slack:LC").one()
        row.status = "failed"
        row.error = "engine timeout"

    with bot_session_factory() as s:
        row = s.query(BotInbox).filter_by(event_key="slack:LC").one()
        assert row.status == "failed"
        assert row.attempts == 1
        assert row.error == "engine timeout"


def test_allowlist_round_trip_and_unique_principal(bot_session_factory) -> None:
    with bot_session_factory() as s, s.begin():
        s.add(
            NdaAllowlist(principal_type="slack", principal_key="U123", added_by="admin")
        )
        # Same key under a DIFFERENT plane is allowed (identity is (type, key)).
        s.add(
            NdaAllowlist(
                principal_type="email",
                principal_key="jane@example.com",
                added_by="admin",
            )
        )

    with bot_session_factory() as s:
        assert s.query(NdaAllowlist).count() == 2
        row = s.query(NdaAllowlist).filter_by(principal_key="U123").one()
        assert row.principal_type == "slack"
        assert row.added_by == "admin"
        assert row.created_at is not None

    # Re-adding the exact (type, key) pair violates the uniqueness gate.
    with bot_session_factory() as s:
        s.add(NdaAllowlist(principal_type="slack", principal_key="U123"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_pending_request_round_trip_decision_and_unique_key(
    bot_session_factory,
) -> None:
    with bot_session_factory() as s, s.begin():
        s.add(
            NdaPendingRequest(
                requester="U123",
                channel="slack",
                intent="envelope",
                request_key="req_abc123",
            )
        )

    # A repeat ask collapses onto the same open request (request_key is unique).
    with bot_session_factory() as s:
        s.add(
            NdaPendingRequest(
                requester="U123",
                channel="slack",
                intent="envelope",
                request_key="req_abc123",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()

    # Admin decision: approve, stamping decided_by/decided_at.
    from datetime import UTC, datetime

    decided = datetime(2026, 7, 3, 15, 30, tzinfo=UTC)
    with bot_session_factory() as s, s.begin():
        row = s.query(NdaPendingRequest).filter_by(request_key="req_abc123").one()
        assert row.status == "pending"  # default
        row.status = "approved"
        row.decided_by = "admin"
        row.decided_at = decided

    with bot_session_factory() as s:
        row = s.query(NdaPendingRequest).filter_by(request_key="req_abc123").one()
        assert row.status == "approved"
        assert row.decided_by == "admin"
        assert row.decided_at is not None


def test_correlation_round_trip_and_unique_key(bot_session_factory) -> None:
    from datetime import UTC, datetime

    expires = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)
    with bot_session_factory() as s, s.begin():
        s.add(
            BotCorrelation(
                key="corr-token-1",
                kind="confirmation",
                payload_json={
                    "kind": "env",
                    "file_id": "F1",
                    "signer_emails": ["a@x.com", "b@y.com"],
                },
                expires_at=expires,
            )
        )

    with bot_session_factory() as s:
        row = s.query(BotCorrelation).filter_by(key="corr-token-1").one()
        assert row.kind == "confirmation"
        assert row.payload_json["signer_emails"] == ["a@x.com", "b@y.com"]
        assert row.expires_at is not None
        assert row.created_at is not None

    with bot_session_factory() as s:
        s.add(BotCorrelation(key="corr-token-1", kind="form", payload_json={}))
        with pytest.raises(IntegrityError):
            s.commit()
