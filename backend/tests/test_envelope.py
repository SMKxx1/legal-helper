"""The canonical normalized envelope: construction, defaults, validation, immutability, parity helpers.

Pure-logic tests — the envelope is a frozen pydantic model with no I/O, so these import it directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.bot.envelope import AttachmentRef, Envelope


def test_minimal_slack_envelope_applies_defaults() -> None:
    env = Envelope(channel="slack", event_key="slack:E123")
    assert env.channel == "slack"
    assert env.event_key == "slack:E123"
    # Every optional field has a benign default (mirrors the n8n Normalize Set-node behavior).
    assert env.text == ""
    assert env.sender_id == ""
    assert env.sender_address == ""
    assert env.verified_sender is False  # untrusted until a channel proves otherwise
    assert env.slack_channel == ""
    assert env.slack_thread_ts == ""
    assert env.email_message_id == ""
    assert env.email_subject == ""
    assert env.from_email == ""
    assert env.attachments == ()
    assert isinstance(env.received_at, datetime)
    assert env.received_at.tzinfo is not None  # tz-aware UTC


def test_full_email_envelope_round_trip() -> None:
    ts = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    env = Envelope(
        channel="email",
        event_key="email:<msg-1@example.com>",
        text="please review the attached NDA",
        sender_id="Jane Doe",
        sender_address="jane@counterparty.com",
        verified_sender=True,
        email_message_id="<msg-1@example.com>",
        email_subject="NDA for signature",
        from_email="nda-bot@example.com",
        attachments=[AttachmentRef(filename="nda.docx", size=2048)],
        received_at=ts,
    )
    assert env.verified_sender is True
    assert env.email_subject == "NDA for signature"
    assert env.received_at == ts
    assert len(env.attachments) == 1
    assert env.attachments[0].filename == "nda.docx"


def test_attachments_list_is_coerced_to_immutable_tuple() -> None:
    # Callers may pass a list; a frozen model stores it as a tuple (iteration/len behave like files[]).
    env = Envelope(
        channel="slack",
        event_key="slack:E1",
        attachments=[
            AttachmentRef(filename="a.pdf"),
            AttachmentRef(filename="b.docx"),
        ],
    )
    assert isinstance(env.attachments, tuple)
    assert [a.filename for a in env.attachments] == ["a.pdf", "b.docx"]
    assert len(env.attachments) == 2


def test_envelope_is_frozen() -> None:
    env = Envelope(channel="slack", event_key="slack:E1")
    with pytest.raises(ValidationError):
        env.text = "mutated"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        env.verified_sender = True  # type: ignore[misc]


def test_unknown_field_is_forbidden() -> None:
    with pytest.raises(ValidationError):
        Envelope(channel="slack", event_key="slack:E1", intent="review")  # type: ignore[call-arg]


def test_event_key_must_be_non_empty() -> None:
    # Fail-closed dedup depends on a real key — a blank one is rejected at construction.
    with pytest.raises(ValidationError):
        Envelope(channel="slack", event_key="")
    with pytest.raises(ValidationError):
        Envelope(channel="slack", event_key="   ")


def test_invalid_channel_rejected() -> None:
    with pytest.raises(ValidationError):
        Envelope(channel="teams", event_key="x:1")  # type: ignore[arg-type]


def test_has_content_matches_ported_guard() -> None:
    # Non-empty text OR any attachment => has content; whitespace-only + no files => no content.
    assert Envelope(channel="slack", event_key="s:1", text="hi").has_content is True
    assert (
        Envelope(
            channel="slack",
            event_key="s:2",
            attachments=[AttachmentRef(filename="a.docx")],
        ).has_content
        is True
    )
    assert Envelope(channel="slack", event_key="s:3", text="   ").has_content is False
    assert Envelope(channel="email", event_key="e:1").has_content is False


def test_attachment_ref_defaults_and_negative_size_clamped() -> None:
    a = AttachmentRef()
    assert a.filename == ""
    assert a.content_type == ""
    assert a.size == 0
    assert a.source_ref == ""
    # A negative reported size is clamped to zero (never trusted to be sane).
    assert AttachmentRef(filename="x", size=-5).size == 0


def test_attachment_ref_is_frozen() -> None:
    a = AttachmentRef(filename="a.docx")
    with pytest.raises(ValidationError):
        a.filename = "b.docx"  # type: ignore[misc]


def test_model_dump_round_trips_through_a_dict() -> None:
    # bot_inbox persists the envelope via model_dump; it must reconstruct 1:1.
    env = Envelope(
        channel="slack",
        event_key="slack:E9",
        text="review",
        slack_channel="C1",
        slack_thread_ts="1720000000.0001",
        attachments=[AttachmentRef(filename="nda.docx", source_ref="F123")],
    )
    data = env.model_dump(mode="json")
    rebuilt = Envelope.model_validate(data)
    assert rebuilt == env
    assert rebuilt.attachments[0].source_ref == "F123"
