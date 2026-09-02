"""The ``template`` intent (PLAN §3.2, reference §3.2): the selector matrix, the zero-row guard, the
Slack picker + the email ask, and end-to-end file delivery through the pipeline.

Zero network, zero real DB: ``resolve`` is a stub (records its ref-code args / raises the not-loaded
EngineError), the DB ``session_factory`` is a dummy context manager, and delivery is asserted against a
captured reply service. The point is (1) the ported ``Selectors Complete?`` gate + the documented
zero-row FIX (reference §9 gap 7 — a missing template is a friendly reply, never a broken .docx), and
(2) that a resolved .docx rides the reply's ``attachments`` all the way to the sink.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.errors import EngineError
from app.bot.blockkit import TEMPLATE_PICKER_FALLBACK_TEXT
from app.bot.envelope import Envelope
from app.bot.intents import IntentContext, IntentRegistry
from app.bot.intents.template import (
    ASK_SELECTORS_TEXT,
    TEMPLATE_FILE_CAPTION,
    TEMPLATE_FILENAME,
    TemplateIntent,
    selectors_complete,
)
from app.bot.router import Classification
from app.support_task.generator import DOCX_MIME

_DOCX = b"PK\x03\x04empty-template-bytes"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False


def _factory() -> _FakeSession:
    return _FakeSession()


class _StubResolver:
    """Records the ref-code args it's called with; returns canned bytes or raises a canned error."""

    def __init__(
        self,
        *,
        result: tuple[bytes, Any] | None = (_DOCX, object()),
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self._result = result
        self._raises = raises

    def __call__(self, db: Any, *args: Any, **kwargs: Any) -> tuple[bytes, Any]:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _intent(resolver: _StubResolver) -> TemplateIntent:
    return TemplateIntent(session_factory=_factory, resolve=resolver)


def _ctx(channel: str, **cls_over: Any) -> IntentContext:
    if channel == "slack":
        env = Envelope(
            channel="slack",
            event_key="slack:T1",
            text="template please",
            slack_channel="C1",
            slack_thread_ts="1.0",
        )
    else:
        env = Envelope(
            channel="email",
            event_key="email:<t1>",
            text="template please",
            sender_address="user@corp.com",
            email_message_id="<t1>",
        )
    cls = Classification(intent="template", **cls_over)
    return IntentContext(envelope=env, classification=cls)


# --------------------------------------------------------------------------- #
# selectors_complete (ported "Selectors Complete?" — mutuality only for individual)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "jur,cp,mut,expected",
    [
        ("US", "company", "", True),
        ("SG", "service_provider", "", True),
        ("US", "individual", "", False),  # individual REQUIRES mutuality
        ("US", "individual", "mutual", True),
        ("", "company", "", False),  # missing jurisdiction
        ("US", "", "", False),  # missing counterparty
        (
            "US",
            "company",
            "mutual",
            True,
        ),  # mutuality ignored for company (still complete)
    ],
)
def test_selectors_complete_matrix(jur: str, cp: str, mut: str, expected: bool) -> None:
    assert selectors_complete(jur, cp, mut) is expected


# --------------------------------------------------------------------------- #
# Complete → file reply (the happy path)
# --------------------------------------------------------------------------- #
def test_complete_company_delivers_docx_attachment() -> None:
    resolver = _StubResolver()
    reply = _intent(resolver)(
        _ctx("slack", jurisdiction="US", counterparty_type="company")
    )
    assert reply.text == TEMPLATE_FILE_CAPTION
    assert reply.slack_blocks is None
    assert len(reply.attachments) == 1
    att = reply.attachments[0]
    assert att.filename == TEMPLATE_FILENAME
    assert att.content == _DOCX
    assert att.content_type == DOCX_MIME


def test_complete_maps_bot_codes_to_ref_codes_service_provider() -> None:
    resolver = _StubResolver()
    _intent(resolver)(
        _ctx("email", jurisdiction="US", counterparty_type="service_provider")
    )
    # normalize_codes: service_provider → ServiceProvider; non-individual → NotApplicable.
    assert resolver.calls[0] == ("US", "ServiceProvider", "NotApplicable")
    assert resolver.kwargs[0] == {"variant": "empty"}


def test_complete_individual_keeps_mutuality_ref_code() -> None:
    resolver = _StubResolver()
    _intent(resolver)(
        _ctx(
            "slack",
            jurisdiction="SG",
            counterparty_type="individual",
            mutuality="unilateral",
        )
    )
    assert resolver.calls[0] == ("SG", "Individual", "Unilateral")


# --------------------------------------------------------------------------- #
# Zero-row guard (reference §9 gap 7 — the deliberate FIX)
# --------------------------------------------------------------------------- #
def test_zero_row_template_not_found_is_friendly_not_broken_docx() -> None:
    resolver = _StubResolver(
        raises=EngineError(404, "template_not_found", "no such template")
    )
    reply = _intent(resolver)(
        _ctx("slack", jurisdiction="US", counterparty_type="company")
    )
    assert reply.attachments == ()  # NO broken/empty document is ever shipped
    assert "loaded yet" in reply.text
    assert "US / Company" in reply.text  # names the exact selector combo


def test_zero_row_blob_missing_is_friendly() -> None:
    resolver = _StubResolver(
        raises=EngineError(409, "template_blob_missing", "bytes not loaded")
    )
    reply = _intent(resolver)(
        _ctx(
            "email",
            jurisdiction="SG",
            counterparty_type="individual",
            mutuality="mutual",
        )
    )
    assert reply.attachments == ()
    assert "loaded yet" in reply.text
    assert "SG / Individual / Mutual" in reply.text


def test_empty_bytes_without_raise_still_guarded() -> None:
    # A resolver that returns empty bytes without raising must ALSO not ship a broken document.
    resolver = _StubResolver(result=(b"", object()))
    reply = _intent(resolver)(
        _ctx("slack", jurisdiction="US", counterparty_type="company")
    )
    assert reply.attachments == ()
    assert "loaded yet" in reply.text


def test_other_engine_error_propagates_to_pipeline() -> None:
    # A NON "not-loaded" EngineError is not swallowed here — the pipeline turns it into the friendly
    # error reply (and records failed for the sweep). Only the not-loaded family is caught.
    resolver = _StubResolver(raises=EngineError(500, "boom", "kaboom"))
    with pytest.raises(EngineError):
        _intent(resolver)(_ctx("slack", jurisdiction="US", counterparty_type="company"))


# --------------------------------------------------------------------------- #
# Incomplete → Slack picker / email ask (resolver never consulted)
# --------------------------------------------------------------------------- #
def test_incomplete_on_slack_posts_the_picker() -> None:
    resolver = _StubResolver()
    reply = _intent(resolver)(_ctx("slack", jurisdiction="US"))  # no counterparty
    assert reply.attachments == ()
    assert reply.slack_blocks is not None
    assert reply.slack_blocks[0]["type"] == "header"
    assert reply.slack_blocks[0]["text"]["text"] == "📄 Template selection"
    assert reply.fallback_text == TEMPLATE_PICKER_FALLBACK_TEXT
    assert resolver.calls == []  # no DB read when selectors are missing


def test_incomplete_individual_without_mutuality_on_slack_posts_picker() -> None:
    resolver = _StubResolver()
    reply = _intent(resolver)(
        _ctx("slack", jurisdiction="US", counterparty_type="individual")
    )
    assert reply.slack_blocks is not None
    assert resolver.calls == []


def test_incomplete_on_email_sends_the_ask() -> None:
    resolver = _StubResolver()
    reply = _intent(resolver)(
        _ctx("email", counterparty_type="company")
    )  # no jurisdiction
    assert reply.slack_blocks is None
    assert reply.attachments == ()
    assert reply.text == ASK_SELECTORS_TEXT
    assert resolver.calls == []


# --------------------------------------------------------------------------- #
# End-to-end: the .docx rides attachments through the pipeline to the sink
# --------------------------------------------------------------------------- #
class _RecordingService:
    channel = "recording"

    def __init__(self) -> None:
        self.delivered: list[Any] = []

    def deliver(self, envelope: Envelope, reply: Any) -> str:
        self.delivered.append(reply)
        return "ok"


def test_pipeline_delivers_template_file_through_service() -> None:
    import json

    from app.ai.gateway import Gateway, RawResult, Usage
    from app.bot.intents import help_intent
    from app.bot.router import AllowAllGate, process

    class _FakeAdapter:
        name = "fake"
        model_id = "fake/classifier"

        def __init__(self, obj: dict) -> None:
            self._text = json.dumps(obj)

        def complete(self, req: Any) -> RawResult:
            return RawResult(
                text=self._text, usage=Usage(), model_version=self.model_id
            )

    gw = Gateway(
        _FakeAdapter(
            {
                "reasoning": "template asked",
                "intent": "template",
                "jurisdiction": "US",
                "counterparty_type": "company",
                "mutuality": None,
                "signer_emails": [],
                "sequential": False,
                "cc_emails": [],
                "cc_timing": "after",
            }
        )
    )

    reg = IntentRegistry()
    reg.register("help", help_intent)
    reg.register(
        "template", TemplateIntent(session_factory=_factory, resolve=_StubResolver())
    )

    env = Envelope(
        channel="slack",
        event_key="slack:TT",
        text="can I get the US company nda template",
        slack_channel="C9",
        slack_thread_ts="9.0",
    )
    svc = _RecordingService()
    res = process(env, registry=reg, gate=AllowAllGate(), gateway=gw, service=svc)

    assert res.outcome == "handled"
    assert res.intent == "template"
    assert res.delivery == "ok"
    assert len(svc.delivered) == 1
    delivered = svc.delivered[0]
    # A channels.protocol.Reply carrying the .docx (the router forwards IntentReply.attachments).
    assert delivered.text == TEMPLATE_FILE_CAPTION
    assert len(delivered.attachments) == 1
    assert delivered.attachments[0].filename == TEMPLATE_FILENAME
    assert delivered.attachments[0].content == _DOCX
