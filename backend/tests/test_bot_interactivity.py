"""Typed, versioned Slack interactivity (PLAN §3.3 step 6, reference §3.7) — parse/authz/route matrix.

No network, no Bolt: :func:`app.bot.interactivity.dispatch_interaction` (and the
``app.bot.dispatch.process_interaction`` seam) are driven directly with hand-built Slack bodies and a
captured Slack sink (a stub ``WebClient``). The approval path runs against the REAL approvals contract
(agent 1): its ``approval_button_value`` / ``admin_notice_blocks`` producer, its ``approve_request`` /
``deny_request`` transitions, and its persisted ``NdaPendingRequest`` row (seeded per test) — so the
cross-agent contract is verified end-to-end, not mocked. Covers the payload parse/version matrix
(hostile / expired / unknown values), ``template_submit`` into the RIGHT thread (payload +
``bot_correlation``), the approval authorization matrix + idempotent double-click, and the "expired" reply.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.bot import dispatch
from app.bot import interactivity as interactivity_mod
from app.bot.approvals import (
    ACTION_APPROVAL_APPROVE,
    ACTION_APPROVAL_DENY,
    admin_notice_blocks,
    approval_button_value,
)
from app.bot.blockkit import ACTION_SELECT_JURISDICTION, ACTION_TEMPLATE_SUBMIT
from app.bot.channels import slack as slackmod
from app.bot.channels.replies import ReplyService, SlackReplySink
from app.bot.intents import IntentContext, IntentRegistry, IntentReply, help_intent
from app.bot.interactivity import (
    EXPIRED_TEXT,
    TEMPLATE_UNAVAILABLE_TEXT,
    ApprovalPayload,
    InteractivityDeps,
    InteractivityError,
    _parse_payload,
    dispatch_interaction,
)
from app.bot.models import BotCorrelation, NdaPendingRequest
from app.config import Settings

# conftest.py is frozen; the shared bot fixtures (bot_session_factory) live in conftest_bot.py.
pytest_plugins = ("conftest_bot",)


# =================================================================================================
# Test doubles + body builders
# =================================================================================================
class StubWebClient:
    """Captures the Slack Web API calls the reply sink makes — no network."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []

    def chat_postMessage(self, **kw: Any) -> dict[str, Any]:
        self.messages.append(kw)
        return {"ts": "resp.ts"}

    def files_upload_v2(self, **kw: Any) -> dict[str, Any]:
        self.uploads.append(kw)
        return {"ts": "resp.ts"}


def _deps(
    session_factory: Any,
    *,
    approvals: Any = None,
    intent_registry: IntentRegistry | None = None,
    is_admin: Any = None,
    admin_channel: str = "CADMIN",
) -> tuple[InteractivityDeps, StubWebClient]:
    stub = StubWebClient()
    sink = SlackReplySink(stub)
    service = ReplyService([sink])
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        nda_admin_slack_channel=admin_channel,
        nda_bot_from_email="nda-bot@example.com",
    )
    deps = InteractivityDeps(
        session_factory=session_factory,
        service=service,
        post_blocks=sink.post_blocks,
        settings=settings,
        approvals=approvals,
        intent_registry=intent_registry,
        is_admin=is_admin,
    )
    return deps, stub


def _for_channel(stub: StubWebClient, channel: str) -> list[dict[str, Any]]:
    return [m for m in stub.messages if m.get("channel") == channel]


def _template_body(
    *,
    jurisdiction: str = "US",
    counterparty_type: str = "company",
    mutuality: str = "",
    channel: str = "CPICKER",
    thread_ts: str = "TPICKER",
    message_ts: str = "MSG1",
    clicker: str = "U1",
) -> dict[str, Any]:
    def _sel(action_id: str, value: str) -> dict[str, Any]:
        el: dict[str, Any] = {"type": "static_select"}
        if (
            value
        ):  # an unselected static-select has no selected_option (mutuality often blank)
            el["selected_option"] = {
                "value": value,
                "text": {"type": "plain_text", "text": value},
            }
        return {action_id: el}

    state = {
        "b_j": _sel(ACTION_SELECT_JURISDICTION, jurisdiction),
        "b_c": _sel("select_counterparty_type", counterparty_type),
        "b_m": _sel("select_mutuality", mutuality),
    }
    return {
        "type": "block_actions",
        "user": {"id": clicker},
        "channel": {"id": channel},
        "container": {
            "type": "message",
            "message_ts": message_ts,
            "thread_ts": thread_ts,
            "channel_id": channel,
        },
        "message": {"ts": message_ts, "thread_ts": thread_ts},
        "trigger_id": "trig1",
        "actions": [{"action_id": ACTION_TEMPLATE_SUBMIT, "type": "button"}],
        "state": {"values": state},
    }


def _approval_body(
    *,
    action_id: str,
    value: str,
    channel: str = "CADMIN",
    thread_ts: str = "TADMIN",
    message_ts: str = "MADMIN",
    clicker: str = "UADMIN",
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": clicker},
        "channel": {"id": channel},
        "container": {
            "type": "message",
            "message_ts": message_ts,
            "thread_ts": thread_ts,
            "channel_id": channel,
        },
        "message": {"ts": message_ts, "thread_ts": thread_ts},
        "trigger_id": "trigA",
        "actions": [{"action_id": action_id, "type": "button", "value": value}],
        "state": {"values": {}},
    }


def _approve_body(request_key: str = "req_test", **over: Any) -> dict[str, Any]:
    return _approval_body(
        action_id=ACTION_APPROVAL_APPROVE,
        value=approval_button_value(request_key, "approve"),
        **over,
    )


def _deny_body(request_key: str = "req_test", **over: Any) -> dict[str, Any]:
    return _approval_body(
        action_id=ACTION_APPROVAL_DENY,
        value=approval_button_value(request_key, "deny"),
        **over,
    )


def _template_registry(handler: Any) -> IntentRegistry:
    reg = IntentRegistry()
    reg.register("help", help_intent)
    if handler is not None:
        reg.register("template", handler)
    return reg


def _seed_pending(
    session_factory: Any,
    *,
    request_key: str,
    requester: str,
    channel: str = "slack",
    status: str = "pending",
    intent: str = "review",
) -> None:
    with session_factory() as s, s.begin():
        s.add(
            NdaPendingRequest(
                requester=requester,
                channel=channel,
                intent=intent,
                request_key=request_key,
                status=status,
            )
        )


def _pending_status(session_factory: Any, request_key: str) -> str | None:
    with session_factory() as s:
        row = (
            s.query(NdaPendingRequest)
            .filter(NdaPendingRequest.request_key == request_key)
            .one_or_none()
        )
        return None if row is None else row.status


def _seed_correlation(
    session_factory: Any, *, key: str, kind: str, payload: dict[str, Any]
) -> None:
    with session_factory() as s, s.begin():
        s.add(BotCorrelation(key=key, kind=kind, payload_json=payload))


# =================================================================================================
# 1) Payload parse / version matrix (hostile / expired / unknown values)
# =================================================================================================
def test_approval_payload_valid_round_trip() -> None:
    # The REAL producer's value parses against our model (the cross-agent value contract).
    p = _parse_payload(approval_button_value("req_x", "approve"), ApprovalPayload)
    assert isinstance(p, ApprovalPayload)
    assert (p.v, p.kind, p.request_key, p.action) == (1, "approval", "req_x", "approve")


def test_admin_notice_blocks_values_parse_both_buttons() -> None:
    blocks = admin_notice_blocks("U9", "review", "req_x")
    buttons = blocks[1]["elements"]
    approve = _parse_payload(buttons[0]["value"], ApprovalPayload)
    deny = _parse_payload(buttons[1]["value"], ApprovalPayload)
    assert approve.action == "approve"
    assert deny.action == "deny"
    assert approve.request_key == deny.request_key == "req_x"


def test_parse_rejects_unsupported_version() -> None:
    bad = json.dumps({"v": 2, "kind": "approval", "request_key": "r1"})
    with pytest.raises(InteractivityError):
        _parse_payload(bad, ApprovalPayload)


def test_parse_rejects_wrong_kind() -> None:
    bad = json.dumps({"v": 1, "kind": "not_approval", "request_key": "r1"})
    with pytest.raises(InteractivityError):
        _parse_payload(bad, ApprovalPayload)


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(InteractivityError):
        _parse_payload("{not valid json", ApprovalPayload)


def test_parse_rejects_missing_request_key() -> None:
    bad = json.dumps(
        {"v": 1, "kind": "approval"}
    )  # request_key required (min_length=1)
    with pytest.raises(InteractivityError):
        _parse_payload(bad, ApprovalPayload)


def test_parse_rejects_empty_request_key() -> None:
    bad = json.dumps({"v": 1, "kind": "approval", "request_key": ""})
    with pytest.raises(InteractivityError):
        _parse_payload(bad, ApprovalPayload)


def test_parse_rejects_missing_value() -> None:
    with pytest.raises(InteractivityError):
        _parse_payload(None, ApprovalPayload)


def test_parse_ignores_extra_fields() -> None:
    # extra='ignore' → forward-compatible with same-version fields a future producer adds.
    ok = json.dumps({"v": 1, "kind": "approval", "request_key": "r1", "future": "x"})
    p = _parse_payload(ok, ApprovalPayload)
    assert isinstance(p, ApprovalPayload)
    assert p.request_key == "r1"


# =================================================================================================
# 2) Unknown / benign / bad-version dispatch → friendly "expired" reply or silent ignore
# =================================================================================================
def test_unknown_action_id_replies_expired(bot_session_factory) -> None:
    deps, stub = _deps(bot_session_factory)
    body = _approval_body(
        action_id="totally_unknown", value="{}", channel="CX", thread_ts="TX"
    )
    dispatch_interaction(body, deps=deps)
    assert len(stub.messages) == 1
    assert stub.messages[0]["text"] == EXPIRED_TEXT
    assert stub.messages[0]["channel"] == "CX"
    assert stub.messages[0]["thread_ts"] == "TX"


def test_bad_version_value_replies_expired_and_does_not_decide(
    bot_session_factory,
) -> None:
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(bot_session_factory)
    bad = json.dumps({"v": 2, "kind": "approval", "request_key": "req_test"})
    body = _approval_body(action_id=ACTION_APPROVAL_APPROVE, value=bad)
    dispatch_interaction(body, deps=deps)
    # No decision taken; the admin thread gets the friendly expired reply.
    assert _pending_status(bot_session_factory, "req_test") == "pending"
    assert [m["text"] for m in stub.messages] == [EXPIRED_TEXT]


def test_malformed_value_replies_expired(bot_session_factory) -> None:
    deps, stub = _deps(bot_session_factory)
    body = _approval_body(action_id=ACTION_APPROVAL_APPROVE, value="{nope")
    dispatch_interaction(body, deps=deps)
    assert [m["text"] for m in stub.messages] == [EXPIRED_TEXT]


def test_picker_select_change_is_ignored_silently(bot_session_factory) -> None:
    deps, stub = _deps(bot_session_factory)
    body = _approval_body(
        action_id=ACTION_SELECT_JURISDICTION, value="US", channel="CP"
    )
    dispatch_interaction(body, deps=deps)
    assert stub.messages == []  # a benign select change must not post anything


def test_generate_open_form_button_is_ignored_silently(bot_session_factory) -> None:
    # The generate flow's "Open the NDA form" URL button (action_id open_nda_form) still POSTs a
    # block_actions interaction on click; it MUST be ignored, not answered with "button expired" — a
    # spurious reply on the generate happy path (final-review finding, fixed).
    from app.bot.intents.generate import ACTION_OPEN_FORM

    deps, stub = _deps(bot_session_factory)
    body = _approval_body(
        action_id=ACTION_OPEN_FORM, value="", channel="CG", thread_ts="TG"
    )
    dispatch_interaction(body, deps=deps)
    assert (
        stub.messages == []
    )  # opening the form must not post anything into the thread


def test_unhandled_interaction_type_is_dropped(bot_session_factory) -> None:
    deps, stub = _deps(bot_session_factory)
    dispatch_interaction({"type": "shortcut", "user": {"id": "U1"}}, deps=deps)
    assert stub.messages == []


# =================================================================================================
# 3) template_submit end-to-end → RIGHT thread (payload reconstruction + bot_correlation override)
# =================================================================================================
def test_template_submit_routes_to_template_intent_in_payload_thread(
    bot_session_factory,
) -> None:
    captured: dict[str, IntentContext] = {}

    def fake_template(ctx: IntentContext) -> IntentReply:
        captured["ctx"] = ctx
        return IntentReply(text="Here is your empty NDA template (US / company).")

    deps, stub = _deps(
        bot_session_factory, intent_registry=_template_registry(fake_template)
    )
    dispatch_interaction(
        _template_body(jurisdiction="US", counterparty_type="company", mutuality=""),
        deps=deps,
    )

    ctx = captured["ctx"]
    assert ctx.classification.intent == "template"
    assert ctx.classification.jurisdiction == "US"
    assert ctx.classification.counterparty_type == "company"
    assert ctx.classification.mutuality == ""
    # No correlation row → thread reconstructed from the interaction payload (the Context-bug fix).
    assert ctx.envelope.slack_channel == "CPICKER"
    assert ctx.envelope.slack_thread_ts == "TPICKER"
    assert len(stub.messages) == 1
    assert stub.messages[0]["channel"] == "CPICKER"
    assert stub.messages[0]["thread_ts"] == "TPICKER"
    assert "template" in stub.messages[0]["text"].lower()


def test_template_submit_reads_individual_mutuality(bot_session_factory) -> None:
    captured: dict[str, IntentContext] = {}

    def fake_template(ctx: IntentContext) -> IntentReply:
        captured["ctx"] = ctx
        return IntentReply(text="ok")

    deps, _ = _deps(
        bot_session_factory, intent_registry=_template_registry(fake_template)
    )
    dispatch_interaction(
        _template_body(
            jurisdiction="SG", counterparty_type="individual", mutuality="unilateral"
        ),
        deps=deps,
    )
    cls = captured["ctx"].classification
    assert (cls.jurisdiction, cls.counterparty_type, cls.mutuality) == (
        "SG",
        "individual",
        "unilateral",
    )


def test_template_submit_uploads_file_reply_into_thread(bot_session_factory) -> None:
    # The template intent returns the resolved .docx on IntentReply.attachments; the interactivity
    # delivery must take the file path (files_upload_v2) into the reconstructed origin thread.
    from app.bot.channels.protocol import OutboundAttachment

    def fake_template(_ctx: IntentContext) -> IntentReply:
        return IntentReply(
            text="Here is your empty NDA template (.docx).",
            attachments=(
                OutboundAttachment(
                    filename="NDA-template.docx",
                    content=b"PK\x03\x04-docx-bytes",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            ),
        )

    deps, stub = _deps(
        bot_session_factory, intent_registry=_template_registry(fake_template)
    )
    dispatch_interaction(
        _template_body(channel="CPICKER", thread_ts="TPICKER"), deps=deps
    )
    assert stub.messages == []  # a file reply is not a text post
    assert len(stub.uploads) == 1
    up = stub.uploads[0]
    assert up["channel"] == "CPICKER"
    assert up["thread_ts"] == "TPICKER"
    assert up["filename"] == "NDA-template.docx"


def test_template_submit_correlation_row_pins_origin_thread(
    bot_session_factory,
) -> None:
    # The template intent stored the picker ORIGIN keyed by the picker message ts; it must win over the
    # interaction's own (picker-message) context so the file lands where the request was made.
    _seed_correlation(
        bot_session_factory,
        key="template_picker:MSG1",
        kind="template_picker",
        payload={"slack_channel": "CORIGIN", "slack_thread_ts": "TORIGIN"},
    )
    captured: dict[str, IntentContext] = {}

    def fake_template(ctx: IntentContext) -> IntentReply:
        captured["ctx"] = ctx
        return IntentReply(text="Here is your empty NDA template.")

    deps, stub = _deps(
        bot_session_factory, intent_registry=_template_registry(fake_template)
    )
    dispatch_interaction(
        _template_body(channel="CPICKER", thread_ts="TPICKER", message_ts="MSG1"),
        deps=deps,
    )
    assert captured["ctx"].envelope.slack_channel == "CORIGIN"
    assert captured["ctx"].envelope.slack_thread_ts == "TORIGIN"
    assert _for_channel(stub, "CORIGIN")
    assert _for_channel(stub, "CPICKER") == []  # never the wrong thread


def test_template_submit_without_template_intent_degrades_friendly(
    bot_session_factory,
) -> None:
    deps, stub = _deps(
        bot_session_factory,
        intent_registry=_template_registry(None),  # help only
    )
    dispatch_interaction(_template_body(), deps=deps)
    assert len(stub.messages) == 1
    assert stub.messages[0]["text"] == TEMPLATE_UNAVAILABLE_TEXT
    assert stub.messages[0]["channel"] == "CPICKER"


def test_template_handler_error_is_swallowed(bot_session_factory) -> None:
    def boom(_ctx: IntentContext) -> IntentReply:
        raise RuntimeError("template handler blew up")

    deps, _ = _deps(bot_session_factory, intent_registry=_template_registry(boom))
    # Must not raise (the Bolt route already ACKed).
    dispatch_interaction(_template_body(), deps=deps)


# =================================================================================================
# 4) Approval click — authorization matrix + notify-requester + idempotent double-click
#    (against the REAL approvals contract: seeded NdaPendingRequest + real approve/deny transitions)
# =================================================================================================
def test_approval_from_admin_channel_approves_and_dms_requester(
    bot_session_factory,
) -> None:
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(bot_session_factory, admin_channel="CADMIN")
    dispatch_interaction(_approve_body("req_test", clicker="UADMIN"), deps=deps)

    # The real transition happened: pending -> approved.
    assert _pending_status(bot_session_factory, "req_test") == "approved"
    # Result posted in the admin thread.
    admin = _for_channel(stub, "CADMIN")
    assert len(admin) == 1
    assert "approved" in admin[0]["text"]
    assert admin[0]["thread_ts"] == "TADMIN"
    # Requester DM'd on their stored channel (the pending row's requester user id) — the ported behavior.
    req = _for_channel(stub, "U9")
    assert len(req) == 1
    assert "approved" in req[0]["text"]


def test_approval_deny_transitions_and_notifies(bot_session_factory) -> None:
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(bot_session_factory)
    dispatch_interaction(_deny_body("req_test", clicker="UADMIN"), deps=deps)
    assert _pending_status(bot_session_factory, "req_test") == "denied"
    assert any("denied" in m["text"] for m in _for_channel(stub, "CADMIN"))
    req = _for_channel(stub, "U9")
    assert len(req) == 1
    assert "didn't approve" in req[0]["text"]


def test_approval_denied_when_click_outside_admin_channel(bot_session_factory) -> None:
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(bot_session_factory, admin_channel="CADMIN")
    dispatch_interaction(
        _approve_body("req_test", channel="CRANDOM", clicker="UHACKER"), deps=deps
    )
    # Fail-closed: no transition, no requester DM.
    assert _pending_status(bot_session_factory, "req_test") == "pending"
    assert _for_channel(stub, "U9") == []
    denied = _for_channel(stub, "CRANDOM")
    assert len(denied) == 1
    assert "Only an admin" in denied[0]["text"]


def test_approval_authorized_by_is_admin_predicate_outside_admin_channel(
    bot_session_factory,
) -> None:
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(
        bot_session_factory,
        admin_channel="CADMIN",
        is_admin=lambda uid: uid == "UBOSS",
    )
    dispatch_interaction(
        _approve_body("req_test", channel="CRANDOM", clicker="UBOSS"), deps=deps
    )
    assert _pending_status(bot_session_factory, "req_test") == "approved"


def test_approval_denied_when_no_admin_channel_and_no_predicate(
    bot_session_factory,
) -> None:
    # Neither NDA_ADMIN_SLACK_CHANNEL configured nor an is_admin predicate → fail closed.
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(bot_session_factory, admin_channel="")
    dispatch_interaction(_approve_body("req_test", channel="CANY"), deps=deps)
    assert _pending_status(bot_session_factory, "req_test") == "pending"
    assert any("Only an admin" in m["text"] for m in _for_channel(stub, "CANY"))


def test_approval_idempotent_double_click(bot_session_factory) -> None:
    # First click transitions + notifies; the second is an idempotent repeat → "already handled", no
    # re-notify (idempotency enforced at THIS layer via the row's prior status).
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(bot_session_factory)
    body = _approve_body("req_test", clicker="UADMIN")

    dispatch_interaction(body, deps=deps)
    dispatch_interaction(body, deps=deps)

    assert _pending_status(bot_session_factory, "req_test") == "approved"
    # Requester DM'd exactly once (only the state-changing click).
    assert len(_for_channel(stub, "U9")) == 1
    admin_texts = [m["text"] for m in _for_channel(stub, "CADMIN")]
    assert sum("approved" in t for t in admin_texts) == 1
    assert any("already handled" in t for t in admin_texts)


def test_approval_notifies_requester_from_correlation_when_present(
    bot_session_factory,
) -> None:
    # A bot_correlation row (a richer future store) takes precedence over the pending row: the requester
    # is notified in their original thread rather than a bare DM.
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    _seed_correlation(
        bot_session_factory,
        key="approval:req_test",
        kind="approval",
        payload={"kind": "slack", "channel": "CREQ2", "thread": "TREQ2", "id": "U9"},
    )
    deps, stub = _deps(bot_session_factory)
    dispatch_interaction(_approve_body("req_test", clicker="UADMIN"), deps=deps)
    assert _pending_status(bot_session_factory, "req_test") == "approved"
    req = _for_channel(stub, "CREQ2")
    assert len(req) == 1
    assert req[0]["thread_ts"] == "TREQ2"
    assert _for_channel(stub, "U9") == []  # correlation won over the bare-DM fallback


def test_approval_deny_notifies_email_requester(bot_session_factory) -> None:
    # An email-origin requester is notified by email (the pending row's channel drives the fork).
    class EmailSink:
        channel = "email"

        def __init__(self) -> None:
            self.sent: list[Any] = []

        def deliver(self, envelope: Any, reply: Any) -> Any:
            self.sent.append((envelope, reply))
            from app.bot.channels.protocol import ReplyResult

            return ReplyResult(ok=True, channel="email")

    _seed_pending(
        bot_session_factory,
        request_key="req_test",
        requester="alice@partner.com",
        channel="email",
    )
    stub = StubWebClient()
    email_sink = EmailSink()
    service = ReplyService([SlackReplySink(stub), email_sink])
    settings = Settings(_env_file=None, nda_admin_slack_channel="CADMIN")  # type: ignore[call-arg]
    deps = InteractivityDeps(
        session_factory=bot_session_factory,
        service=service,
        post_blocks=SlackReplySink(stub).post_blocks,
        settings=settings,
    )
    dispatch_interaction(_deny_body("req_test", clicker="UADMIN"), deps=deps)
    assert _pending_status(bot_session_factory, "req_test") == "denied"
    assert len(email_sink.sent) == 1
    env, _reply = email_sink.sent[0]
    assert env.channel == "email"
    assert env.sender_address == "alice@partner.com"


def test_approval_of_missing_request_reports_and_does_not_notify(
    bot_session_factory,
) -> None:
    # No pending row → approve_request returns False → admin told, no requester notification.
    deps, stub = _deps(bot_session_factory)
    dispatch_interaction(_approve_body("req_nope", clicker="UADMIN"), deps=deps)
    admin = _for_channel(stub, "CADMIN")
    assert len(admin) == 1
    assert "Couldn't approve" in admin[0]["text"]
    # Nothing but the admin channel got a message.
    assert {m["channel"] for m in stub.messages} == {"CADMIN"}


def test_approval_degrades_when_approvals_unavailable(
    bot_session_factory, monkeypatch
) -> None:
    # Force the "no approvals module" path deterministically (the real module IS importable now).
    monkeypatch.setattr(interactivity_mod, "_load_approvals", lambda: None)
    _seed_pending(bot_session_factory, request_key="req_test", requester="U9")
    deps, stub = _deps(bot_session_factory, approvals=None)
    dispatch_interaction(_approve_body("req_test", clicker="UADMIN"), deps=deps)
    admin = _for_channel(stub, "CADMIN")
    assert len(admin) == 1
    assert "aren't available" in admin[0]["text"]
    assert _pending_status(bot_session_factory, "req_test") == "pending"
    assert _for_channel(stub, "U9") == []


# =================================================================================================
# 5) The dispatch.process_interaction seam (wave-A Bolt route resolves this exact name)
# =================================================================================================
def test_process_interaction_seam_delegates(bot_session_factory) -> None:
    deps, stub = _deps(bot_session_factory)
    body = _approval_body(
        action_id="totally_unknown", value="{}", channel="CX", thread_ts="TX"
    )
    dispatch.process_interaction(body, deps=deps)
    assert [m["text"] for m in stub.messages] == [EXPIRED_TEXT]


def test_wave_a_lazy_seam_resolves_process_interaction() -> None:
    # The Slack Bolt handler resolves ``process_interaction`` via _lazy_seam — verify the name matches.
    assert slackmod._lazy_seam("process_interaction") is dispatch.process_interaction
