"""Reply service + Block Kit builders (PLAN §3.3 step 5, §3.7).

The Slack reply path is exercised against a stubbed WebClient (no network): text → threaded
chat.postMessage, file → threaded files_upload_v2, blocks → chat.postMessage with blocks + fallback.
The channel router picks the sink by ``Envelope.channel`` (exact match) and degrades fail-soft when no
sink is registered. The Block Kit builders assert the PRESERVED action_ids + option values (reference
§3.2, §8) that the interactivity handler routes on.
"""

from __future__ import annotations

import json
from typing import Any

from app.bot.blockkit import (
    ACTION_SELECT_COUNTERPARTY_TYPE,
    ACTION_SELECT_JURISDICTION,
    ACTION_SELECT_MUTUALITY,
    ACTION_TEMPLATE_SUBMIT,
    COUNTERPARTY_TYPE_OPTIONS,
    HELP_FALLBACK_TEXT,
    JURISDICTION_OPTIONS,
    MUTUALITY_OPTIONS,
    help_blocks,
    template_picker_blocks,
)
from app.bot.channels.protocol import Reply, ReplyResult
from app.bot.channels.replies import ReplyService, SlackReplySink
from app.bot.envelope import Envelope


class FakeWebClient:
    def __init__(self, *, raise_on: str | None = None) -> None:
        self.text_calls: list[dict] = []
        self.file_calls: list[dict] = []
        self.block_calls: list[dict] = []
        self._raise_on = raise_on

    def chat_postMessage(self, **kwargs: Any) -> dict:
        if self._raise_on == "text":
            raise RuntimeError("slack down")
        if "blocks" in kwargs:
            self.block_calls.append(kwargs)
        else:
            self.text_calls.append(kwargs)
        return {"ok": True, "ts": "1700.0001"}

    def files_upload_v2(self, **kwargs: Any) -> dict:
        self.file_calls.append(kwargs)
        return {"ok": True, "file": {"id": "F1"}, "ts": "1700.0002"}


class FakeEmailSink:
    channel = "email"

    def __init__(self) -> None:
        self.delivered: list[Reply] = []

    def deliver(self, envelope: Envelope, reply: Reply) -> ReplyResult:
        self.delivered.append(reply)
        return ReplyResult(ok=True, channel="email", detail="email")


def _slack_env(thread: str = "111.001") -> Envelope:
    return Envelope(
        channel="slack",
        event_key="slack:E1",
        slack_channel="C1",
        slack_thread_ts=thread,
        text="hi",
    )


def _email_env() -> Envelope:
    return Envelope(
        channel="email",
        event_key="email:<m1>",
        sender_address="jane@corp.com",
        email_message_id="<m1>",
    )


# ---- Slack sink ----------------------------------------------------------------------------------
def test_slack_send_text_is_threaded() -> None:
    fake = FakeWebClient()
    service = ReplyService([SlackReplySink(fake)])
    result = service.send_text(_slack_env(), "hello there")
    assert result.ok is True
    assert fake.text_calls[0]["channel"] == "C1"
    assert fake.text_calls[0]["thread_ts"] == "111.001"
    assert fake.text_calls[0]["text"] == "hello there"


def test_slack_thread_ts_none_when_no_thread() -> None:
    fake = FakeWebClient()
    SlackReplySink(fake).deliver(
        Envelope(channel="slack", event_key="slack:E2", slack_channel="C1"),
        Reply(text="root post"),
    )
    assert fake.text_calls[0]["thread_ts"] is None


def test_slack_send_file_uploads_threaded() -> None:
    fake = FakeWebClient()
    service = ReplyService([SlackReplySink(fake)])
    result = service.send_file(
        _slack_env(),
        filename="NDA-template.docx",
        content=b"PK\x03\x04",
        text="Here is your template.",
    )
    assert result.ok is True
    call = fake.file_calls[0]
    assert call["channel"] == "C1"
    assert call["thread_ts"] == "111.001"
    assert call["filename"] == "NDA-template.docx"
    assert call["file"] == b"PK\x03\x04"
    assert call["initial_comment"] == "Here is your template."


def test_slack_post_blocks_carries_fallback_text() -> None:
    fake = FakeWebClient()
    sink = SlackReplySink(fake)
    result = sink.post_blocks(_slack_env(), help_blocks(), HELP_FALLBACK_TEXT)
    assert result.ok is True
    assert fake.block_calls[0]["text"] == HELP_FALLBACK_TEXT
    assert fake.block_calls[0]["blocks"][0]["type"] == "header"


def test_slack_delivery_is_fail_soft() -> None:
    fake = FakeWebClient(raise_on="text")
    result = SlackReplySink(fake).deliver(_slack_env(), Reply(text="x"))
    assert result.ok is False
    assert "slack down" in result.error


# ---- channel routing -----------------------------------------------------------------------------
def test_service_routes_by_channel() -> None:
    fake = FakeWebClient()
    email = FakeEmailSink()
    service = ReplyService([SlackReplySink(fake), email])

    assert service.send_text(_slack_env(), "to slack").ok is True
    assert service.deliver(_email_env(), Reply(text="to email")).ok is True
    assert len(fake.text_calls) == 1
    assert len(email.delivered) == 1


def test_service_missing_sink_is_fail_soft() -> None:
    service = ReplyService([SlackReplySink(FakeWebClient())])  # no email sink
    result = service.deliver(_email_env(), Reply(text="unroutable"))
    assert result.ok is False
    assert result.channel == "email"
    assert "no reply sink" in result.error


# ---- Block Kit contract --------------------------------------------------------------------------
def _action_ids(blocks: list[dict]) -> set[str]:
    ids: set[str] = set()
    for b in blocks:
        acc = b.get("accessory")
        if isinstance(acc, dict) and "action_id" in acc:
            ids.add(acc["action_id"])
        for el in b.get("elements", []) or []:
            if isinstance(el, dict) and "action_id" in el:
                ids.add(el["action_id"])
    return ids


def _select_values(blocks: list[dict], action_id: str) -> list[str]:
    for b in blocks:
        acc = b.get("accessory")
        if isinstance(acc, dict) and acc.get("action_id") == action_id:
            return [o["value"] for o in acc["options"]]
    return []


def test_template_picker_action_ids_and_option_values() -> None:
    blocks = template_picker_blocks()
    ids = _action_ids(blocks)
    assert {
        ACTION_SELECT_JURISDICTION,
        ACTION_SELECT_COUNTERPARTY_TYPE,
        ACTION_SELECT_MUTUALITY,
        ACTION_TEMPLATE_SUBMIT,
    } <= ids
    # Option values are the preserved contract (exact codes, in order).
    assert _select_values(blocks, ACTION_SELECT_JURISDICTION) == list(
        JURISDICTION_OPTIONS
    )
    assert _select_values(blocks, ACTION_SELECT_COUNTERPARTY_TYPE) == list(
        COUNTERPARTY_TYPE_OPTIONS
    )
    assert _select_values(blocks, ACTION_SELECT_MUTUALITY) == list(MUTUALITY_OPTIONS)


def test_preserved_action_id_literals() -> None:
    # These strings are a wire contract — pin them so a rename can't slip through.
    assert ACTION_TEMPLATE_SUBMIT == "template_submit"
    assert ACTION_SELECT_JURISDICTION == "select_jurisdiction"
    assert ACTION_SELECT_COUNTERPARTY_TYPE == "select_counterparty_type"
    assert ACTION_SELECT_MUTUALITY == "select_mutuality"


def test_help_card_has_header_and_commands() -> None:
    blocks = help_blocks()
    assert blocks[0]["type"] == "header"
    body = json.dumps(blocks)
    for keyword in ("Template", "Generate", "Review", "Envelope", "Archive"):
        assert keyword in body
