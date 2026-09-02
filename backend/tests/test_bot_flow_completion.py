"""``run_generation`` — the generation seam the Tally webhook calls (PLAN §3.6, reference §3.4).

Runs against a throwaway per-test SQLite DB (shared ``conftest_bot`` fixture) with a STUBBED template
resolve + fill (no python-docx work, no template rows needed) and CAPTURING reply sinks — so the
assertions are on: the right document lands in the right conversation, the DocuSign button carries the
typed value the envelope agent will read, engine/routing errors map to the ported friendly replies, and
a channel-less origin degrades cleanly. Zero network, no LLM.
"""

from __future__ import annotations

import json
from typing import Any

from app.api.errors import EngineError
from app.bot.flows.generate_completion import (
    ACTION_SEND_DOCUSIGN,
    GENERATED_NDA_CAPTION,
    GENERATED_NDA_FILENAME,
    run_generation,
)
from app.bot.router import reset_delivery
from app.support_task.generator import DOCX_MIME

pytest_plugins = ("conftest_bot",)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _CaptureService:
    """A ReplyService stand-in: records every ``deliver(envelope, reply)`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def deliver(self, envelope: Any, reply: Any) -> str:
        self.calls.append((envelope, reply))
        return "ok"


class _CapturePostBlocks:
    """A ``SlackReplySink.post_blocks`` stand-in: records (envelope, blocks, fallback)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, list[dict], str]] = []

    def __call__(self, envelope: Any, blocks: list[dict], fallback: str) -> str:
        self.calls.append((envelope, blocks, fallback))
        return "ok"


class _StubResolve:
    """Records the ref-code args; returns canned template bytes or raises a canned EngineError."""

    def __init__(
        self, *, docx: bytes = b"TEMPLATE-BYTES", raises: Exception | None = None
    ) -> None:
        self.calls: list[tuple] = []
        self._docx = docx
        self._raises = raises

    def __call__(
        self, db: Any, jur: str, cp: str, mut: str, *, variant: str = "tokenised"
    ) -> tuple[bytes, Any]:
        self.calls.append((jur, cp, mut, variant))
        if self._raises is not None:
            raise self._raises
        return self._docx, object()


class _StubFill:
    """Records (template_bytes, values, strip_unfilled); returns canned filled bytes."""

    def __init__(self, out: bytes = b"FILLED-NDA-DOCX") -> None:
        self.calls: list[tuple] = []
        self._out = out

    def __call__(
        self, template_bytes: bytes, values: dict, *, strip_unfilled: bool = True
    ) -> bytes:
        self.calls.append((template_bytes, dict(values), strip_unfilled))
        return self._out


_VALUES = {"counterparty_name": "Acme Pte Ltd", "purpose": "evaluating a partnership"}
_SLACK_CTX = {
    "channel": "slack",
    "slack_channel": "C9",
    "slack_thread_ts": "1700.5",
    "sender": "U1",
    "from_email": "nda-bot@example.com",
}
_EMAIL_CTX = {
    "channel": "email",
    "sender": "user@corp.com",
    "email_message_id": "<m1@corp.com>",
    "email_subject": "NDA please",
    "from_email": "nda-bot@example.com",
}


def _button(blocks: list[dict]) -> dict:
    actions = next(b for b in blocks if b["type"] == "actions")
    return actions["elements"][0]


# --------------------------------------------------------------------------- #
# Slack: file delivered + DocuSign offer
# --------------------------------------------------------------------------- #
def test_slack_generation_delivers_docx_and_offers_docusign(
    bot_session_factory,
) -> None:
    reset_delivery()
    svc, pb, resolve, fill = (
        _CaptureService(),
        _CapturePostBlocks(),
        _StubResolve(),
        _StubFill(),
    )
    result = run_generation(
        values=_VALUES,
        jurisdiction="SG",
        counterparty_type="company",
        mutuality="",
        origin_context=_SLACK_CTX,
        ref="tally:sub1",
        service=svc,
        post_blocks=pb,
        resolve_template=resolve,
        fill=fill,
        session_factory=bot_session_factory,
    )
    assert result.ok and result.delivered and result.docusign_offered
    # Routing canonicalized to ref codes; company => mutuality NotApplicable; tokenised variant.
    assert resolve.calls == [("SG", "Company", "NotApplicable", "tokenised")]
    assert fill.calls == [(b"TEMPLATE-BYTES", _VALUES, True)]

    # The file landed in the origin thread, with the ported caption + filename + mime.
    assert len(svc.calls) == 1
    env, reply = svc.calls[0]
    assert env.channel == "slack" and env.slack_channel == "C9"
    assert reply.text == GENERATED_NDA_CAPTION
    att = reply.attachments[0]
    assert att.filename == GENERATED_NDA_FILENAME
    assert att.content == b"FILLED-NDA-DOCX"
    assert att.content_type == DOCX_MIME

    # The DocuSign offer button carries the typed {v,kind,ref} value the envelope handler reads.
    assert len(pb.calls) == 1
    btn = _button(pb.calls[0][1])
    assert btn["action_id"] == ACTION_SEND_DOCUSIGN
    payload = json.loads(btn["value"])
    assert payload["kind"] == "send_docusign" and payload["ref"]


# --------------------------------------------------------------------------- #
# Email: file delivered, no Slack DocuSign button
# --------------------------------------------------------------------------- #
def test_email_generation_delivers_docx_without_docusign_button(
    bot_session_factory,
) -> None:
    reset_delivery()
    svc, resolve, fill = _CaptureService(), _StubResolve(), _StubFill()
    result = run_generation(
        values=_VALUES,
        jurisdiction="US",
        counterparty_type="company",
        mutuality="",
        origin_context=_EMAIL_CTX,
        service=svc,
        post_blocks=None,
        resolve_template=resolve,
        fill=fill,
        session_factory=bot_session_factory,
    )
    assert result.ok and result.delivered and not result.docusign_offered
    env, reply = svc.calls[0]
    assert env.channel == "email" and env.sender_address == "user@corp.com"
    assert reply.attachments[0].filename == GENERATED_NDA_FILENAME


# --------------------------------------------------------------------------- #
# Friendly degrades
# --------------------------------------------------------------------------- #
def test_missing_template_is_friendly_zero_row_guard(bot_session_factory) -> None:
    reset_delivery()
    svc = _CaptureService()
    resolve = _StubResolve(
        raises=EngineError(404, "template_not_found", "no such template")
    )
    result = run_generation(
        values=_VALUES,
        jurisdiction="SG",
        counterparty_type="individual",
        mutuality="mutual",
        origin_context=_SLACK_CTX,
        service=svc,
        post_blocks=_CapturePostBlocks(),
        resolve_template=resolve,
        fill=_StubFill(),
        session_factory=bot_session_factory,
    )
    assert not result.ok and result.reason.startswith("engine_error")
    # A friendly text reply (naming the combo) went out — never a broken document.
    assert len(svc.calls) == 1
    text = svc.calls[0][1].text.lower()
    assert "template" in text and "individual" in text


def test_bad_routing_is_friendly(bot_session_factory) -> None:
    reset_delivery()
    svc = _CaptureService()
    result = run_generation(
        values=_VALUES,
        jurisdiction="ZZ",  # unknown jurisdiction => normalize_codes raises
        counterparty_type="company",
        mutuality="",
        origin_context=_SLACK_CTX,
        service=svc,
        resolve_template=_StubResolve(),
        fill=_StubFill(),
        session_factory=bot_session_factory,
    )
    assert not result.ok and result.reason.startswith("engine_error")
    assert len(svc.calls) == 1  # a friendly reply, no document


def test_no_origin_channel_is_no_delivery(bot_session_factory) -> None:
    reset_delivery()
    svc, resolve, fill = _CaptureService(), _StubResolve(), _StubFill()
    result = run_generation(
        values=_VALUES,
        jurisdiction="SG",
        counterparty_type="company",
        mutuality="",
        origin_context={},  # no requester conversation to deliver into
        service=svc,
        resolve_template=resolve,
        fill=fill,
        session_factory=bot_session_factory,
    )
    assert not result.ok and result.reason == "no_delivery"
    assert svc.calls == []  # generated but nowhere to deliver
