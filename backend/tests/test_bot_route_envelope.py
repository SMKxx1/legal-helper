"""The routing pipeline: ``route`` / ``process`` / ``route_envelope`` (PLAN §3.3/§3.4).

This is the ported n8n Router's *routing + dispatch* half — the pipeline
``app.bot.dispatch.process_envelope`` (the worker/email agent's intake+dedup seam) hands a claimed
envelope to. Exercised with fake intent handlers, a fake reply service and a fake classifier gateway —
zero network, zero DB. Covers: deterministic→classifier routing, the allowlist/approvals gate HOOK,
intent dispatch, the ``unknown``→help fallback, the friendly error-reply path, and delivery wiring.
"""

from __future__ import annotations

import json

import pytest

from app.ai.gateway import Gateway, RawResult, Usage
from app.bot.envelope import Envelope
from app.bot.intents import IntentContext, IntentRegistry, IntentReply
from app.bot.router import (
    AllowAllGate,
    Classification,
    GateDecision,
    configure_delivery,
    process,
    reset_delivery,
    route,
    route_envelope,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeAdapter:
    name = "fake"
    model_id = "fake/classifier"

    def __init__(self, obj: dict) -> None:
        self._text = json.dumps(obj)

    def complete(self, req) -> RawResult:  # noqa: ANN001
        return RawResult(text=self._text, usage=Usage(), model_version=self.model_id)


def _classifier_gateway(intent: str) -> Gateway:
    return Gateway(
        _FakeAdapter(
            {
                "reasoning": "fake",
                "intent": intent,
                "jurisdiction": None,
                "counterparty_type": None,
                "mutuality": None,
                "signer_emails": [],
                "sequential": False,
                "cc_emails": [],
                "cc_timing": "after",
            }
        )
    )


class _RecordingService:
    """A stand-in ``ReplyService``: records every text delivery instead of hitting a channel."""

    channel = "recording"

    def __init__(self) -> None:
        self.delivered: list[tuple[str, object]] = []

    def deliver(self, envelope: Envelope, reply: object) -> str:
        self.delivered.append((envelope.event_key, reply))
        return "ok"


def _slack_env(text: str = "help", **kw) -> Envelope:
    return Envelope(
        channel="slack",
        event_key=kw.pop("event_key", "slack:E1"),
        text=text,
        slack_channel="C1",
        slack_thread_ts="1.0",
        **kw,
    )


def _email_env(text: str = "help", **kw) -> Envelope:
    return Envelope(
        channel="email",
        event_key=kw.pop("event_key", "email:<1@x>"),
        text=text,
        sender_address="user@x.com",
        email_message_id="<1@x>",
        **kw,
    )


@pytest.fixture(autouse=True)
def _clear_delivery():
    """Every test starts with no process-wide delivery config (module global)."""
    reset_delivery()
    yield
    reset_delivery()


# --------------------------------------------------------------------------- #
# route(): deterministic first, classifier on defer, degrade on no provider
# --------------------------------------------------------------------------- #
def test_route_uses_deterministic_without_touching_gateway() -> None:
    # A gateway that would explode if consulted proves the deterministic path short-circuits it.
    class _Boom:
        name = "boom"
        model_id = "boom"

        def complete(self, req):  # noqa: ANN001
            raise AssertionError("classifier must not be called for a bare command")

    c = route(_slack_env("review this"), gateway=Gateway(_Boom()))
    assert c.intent == "review"
    assert c.deterministic is True


def test_route_defers_to_classifier() -> None:
    c = route(
        _email_env("take a careful pass over the attached and flag anything"),
        gateway=_classifier_gateway("review"),
    )
    assert c.intent == "review"
    assert c.deterministic is False


def test_route_degrades_to_unknown_when_no_provider() -> None:
    from app.config import Settings

    no_llm = Settings(_env_file=None, openrouter_api_key="", anthropic_api_key="")
    c = route(_email_env("something ambiguous with no keywords"), settings=no_llm)
    assert c == Classification(intent="unknown")


# --------------------------------------------------------------------------- #
# process(): dispatch to the registered handler
# --------------------------------------------------------------------------- #
def _registry_with(**handlers) -> IntentRegistry:
    r = IntentRegistry()
    # Always provide a help fallback so unknown/unregistered intents resolve.
    r.register("help", lambda ctx: IntentReply(text="HELP", fallback_text="help"))
    for intent, reply in handlers.items():
        r.register(intent, (lambda rep: lambda ctx: rep)(reply))
    return r


def test_process_dispatches_to_registered_handler() -> None:
    # 'review' is gated; this test exercises DISPATCH, not the gate, so opt into allow-all explicitly
    # (the default is now the real fail-closed AllowlistGate — approvals coverage lives in test_bot_approvals).
    reg = _registry_with(review=IntentReply(text="REVIEWED"))
    res = process(_slack_env("review this"), registry=reg, gate=AllowAllGate())
    assert res.outcome == "handled"
    assert res.intent == "review"
    assert res.reply.text == "REVIEWED"
    assert res.deterministic is True


def test_process_unknown_falls_back_to_help() -> None:
    reg = _registry_with()  # help only
    res = process(
        _email_env("gibberish"), gateway=_classifier_gateway("unknown"), registry=reg
    )
    assert res.outcome == "handled"
    assert res.intent == "help"  # unknown → help fallback (reference §1/§3.10)
    assert res.reply.text == "HELP"


def test_process_unregistered_intent_falls_back_to_help() -> None:
    # 'review' classified but no review handler registered yet (wave B) → help. Allow-all so the miss
    # exercises the registry fallback, not the gate (the default gate is now fail-closed AllowlistGate).
    reg = _registry_with()  # help only
    res = process(_slack_env("review this"), registry=reg, gate=AllowAllGate())
    assert res.intent == "help"
    assert res.outcome == "handled"


def test_process_default_registry_help_card_on_slack() -> None:
    # The real default registry: help produces the Block Kit card.
    res = process(_slack_env("help"))
    assert res.intent == "help"
    assert res.reply.slack_blocks is not None
    assert res.reply.slack_blocks[0]["type"] == "header"


# --------------------------------------------------------------------------- #
# process(): the gate HOOK
# --------------------------------------------------------------------------- #
def test_allow_all_gate_lets_gated_intent_through() -> None:
    reg = _registry_with(review=IntentReply(text="REVIEWED"))
    res = process(_slack_env("review this"), registry=reg, gate=AllowAllGate())
    assert res.outcome == "handled"
    assert res.reply.text == "REVIEWED"


def test_pending_gate_short_circuits_with_pending_reply() -> None:
    class _PendingGate:
        def check(self, envelope, classification) -> GateDecision:
            return GateDecision(status="pending", request_key="req_abc123")

    reg = _registry_with(review=IntentReply(text="SHOULD NOT RUN"))
    res = process(_slack_env("review this"), registry=reg, gate=_PendingGate())
    assert res.outcome == "pending"
    assert res.intent == "review"
    assert "needs sign-off" in res.reply.text
    assert "req_abc123" in res.reply.text
    assert res.reply.text != "SHOULD NOT RUN"  # the handler never ran


# --------------------------------------------------------------------------- #
# process(): the friendly error-reply path (reference §2.7)
# --------------------------------------------------------------------------- #
def test_handler_exception_yields_friendly_error_reply() -> None:
    def _boom(_ctx: IntentContext) -> IntentReply:
        raise RuntimeError("handler blew up")

    reg = IntentRegistry()
    reg.register("help", lambda ctx: IntentReply(text="HELP"))
    reg.register("review", _boom)
    # Allow-all so the (gated) handler actually runs and we can assert its exception → error reply.
    res = process(_slack_env("review this"), registry=reg, gate=AllowAllGate())
    assert res.outcome == "error"
    assert res.intent == "error"
    assert res.reply.text.startswith(
        "*Sorry — I hit a problem finishing that request.*"
    )


def test_classifier_failure_yields_friendly_error_reply() -> None:
    from app.ai.gateway import TerminalProviderError

    class _BoomAdapter:
        name = "boom"
        model_id = "boom"

        def complete(self, req):  # noqa: ANN001
            raise TerminalProviderError("provider down")

    # A deferred message + a classifier that fails → error reply (mirrors n8n classifier onError).
    res = process(
        _email_env("ambiguous no-keyword message"), gateway=Gateway(_BoomAdapter())
    )
    assert res.outcome == "error"
    assert res.reply.text.startswith("*Sorry — I hit a problem")


# --------------------------------------------------------------------------- #
# Delivery wiring
# --------------------------------------------------------------------------- #
def test_process_delivers_text_through_service() -> None:
    svc = _RecordingService()
    reg = _registry_with(review=IntentReply(text="REVIEWED"))
    # Allow-all so this delivery test isn't short-circuited by the (now fail-closed) default gate.
    res = process(
        _email_env("review this"), registry=reg, service=svc, gate=AllowAllGate()
    )
    assert res.delivery == "ok"
    assert len(svc.delivered) == 1
    ek, reply = svc.delivered[0]
    assert ek == "email:<1@x>"
    assert (
        reply.text == "REVIEWED"
    )  # a channels.protocol.Reply carrying the mrkdwn text


def test_process_posts_blocks_on_slack_via_post_blocks() -> None:
    posted: list[tuple[str, list, str]] = []

    def _post_blocks(env: Envelope, blocks: list, fallback: str):
        posted.append((env.event_key, blocks, fallback))
        return "posted"

    svc = _RecordingService()
    # Default registry: help → blocks. Slack channel + post_blocks → the card is posted, not text.
    res = process(_slack_env("help"), service=svc, post_blocks=_post_blocks)
    assert res.delivery == "posted"
    assert len(posted) == 1
    assert posted[0][2]  # a non-empty fallback text was passed
    assert svc.delivered == []  # text deliver NOT used when blocks are posted


def test_email_help_uses_text_not_blocks() -> None:
    svc = _RecordingService()

    def _post_blocks(env, blocks, fallback):  # noqa: ANN001
        raise AssertionError("email must never post Slack blocks")

    res = process(_email_env("help"), service=svc, post_blocks=_post_blocks)
    assert res.delivery == "ok"
    assert len(svc.delivered) == 1  # delivered as text on the email channel


# --------------------------------------------------------------------------- #
# route_envelope(): the production entry the dispatcher calls
# --------------------------------------------------------------------------- #
def test_route_envelope_uses_configured_delivery() -> None:
    svc = _RecordingService()
    configure_delivery(svc)
    # Allow-all so this delivery-wiring test isn't short-circuited by the fail-closed default gate.
    res = route_envelope(
        _email_env("review this"),
        registry=_registry_with(review=IntentReply(text="REVIEWED")),
        gate=AllowAllGate(),
    )
    assert res.outcome == "handled"
    assert len(svc.delivered) == 1  # delivered via the process-wide configured service


def test_route_envelope_is_a_drop_in_for_dispatch_router_contract() -> None:
    # The worker's dispatch calls route_envelope(envelope) positionally and ignores the return value.
    reset_delivery()
    res = route_envelope(
        _slack_env("help")
    )  # no service configured → no delivery, still computes
    assert res.intent == "help"
    assert res.reply.slack_blocks is not None
    assert res.delivery is None  # nothing wired → fail-soft no-op


def test_route_envelope_never_raises_on_handler_failure() -> None:
    def _boom(_ctx):  # noqa: ANN001
        raise RuntimeError("kaboom")

    reg = IntentRegistry()
    reg.register("help", lambda ctx: IntentReply(text="HELP"))
    reg.register("generate", _boom)
    res = route_envelope(_slack_env("generate an nda"), registry=reg)
    assert res.outcome == "error"  # degraded, not raised
