"""The ``generate`` intent (PLAN §3.6, reference §3.3): the reply matrix — Slack card / email link /
capability-off — for the external **Tally** form.

The intent now hands out a link to the public Tally form with the requester's conversation pre-filled
into a SIGNED ``channel`` routing token (so the Tally webhook can route the finished NDA back, and a
respondent can't edit the URL to redirect it). No DB / network: the link is built from ``Settings`` +
the envelope, and the ``tally`` capability gate reads a registry built from those same settings.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.bot.envelope import Envelope
from app.bot.intents import IntentContext
from app.bot.intents.generate import (
    ACTION_OPEN_FORM,
    FORMS_UNAVAILABLE_TEXT,
    GENERATE_FALLBACK_TEXT,
    OPEN_FORM_BUTTON_TEXT,
    GenerateIntent,
)
from app.bot.router import Classification
from app.config import Settings
from app.integrations.tally import verify_routing_token

_SECRET = "s" * 16

pytest_plugins = ("conftest_bot",)

_FORM_ID = "jagDPJ"


def _settings(*, tally_on: bool = True) -> Settings:
    # tally_signing_secret present => the ``tally`` capability is ENABLED; absent => disabled.
    if tally_on:
        return Settings(_env_file=None, tally_signing_secret="s" * 16)
    return Settings(_env_file=None)


def _slack_ctx() -> IntentContext:
    env = Envelope(
        channel="slack",
        event_key="slack:G1",
        text="generate an nda",
        sender_id="U123",
        slack_channel="C9",
        slack_thread_ts="1700.5",
    )
    return IntentContext(envelope=env, classification=Classification(intent="generate"))


def _email_ctx() -> IntentContext:
    env = Envelope(
        channel="email",
        event_key="email:<g1>",
        text="please generate an nda",
        sender_address="user@corp.com",
        email_message_id="<g1@corp.com>",
        email_subject="NDA please",
    )
    return IntentContext(envelope=env, classification=Classification(intent="generate"))


def _button(blocks: tuple[dict, ...]) -> dict:
    actions = next(b for b in blocks if b["type"] == "actions")
    return actions["elements"][0]


# --------------------------------------------------------------------------- #
# Slack: Block Kit card + URL button prefilled with the Slack channel/thread
# --------------------------------------------------------------------------- #
def test_slack_reply_is_tally_link_card() -> None:
    reply = GenerateIntent(settings=_settings())(_slack_ctx())
    assert reply.slack_blocks is not None
    assert reply.fallback_text == GENERATE_FALLBACK_TEXT
    btn = _button(reply.slack_blocks)
    assert btn["action_id"] == ACTION_OPEN_FORM
    assert btn["text"]["text"] == OPEN_FORM_BUTTON_TEXT

    parsed = urlparse(btn["url"])
    assert parsed.netloc == "tally.so"
    assert parsed.path == f"/r/{_FORM_ID}"
    q = parse_qs(parsed.query)
    # Slack origin => channel is a SIGNED routing token (not the raw id); it verifies back to the
    # channel id + thread. thread_ts is folded INTO the token, not a separate (forgeable) query param.
    assert "thread_ts" not in q
    routing = verify_routing_token(_SECRET, q["channel"][0])
    assert routing == {"channel": "C9", "thread_ts": "1700.5"}


# --------------------------------------------------------------------------- #
# Email: inline link prefilled with email||<addr>
# --------------------------------------------------------------------------- #
def test_email_reply_is_tally_link() -> None:
    reply = GenerateIntent(settings=_settings())(_email_ctx())
    assert reply.text is not None
    url = reply.text.split("\n\n")[-1].strip()
    parsed = urlparse(url)
    assert parsed.netloc == "tally.so" and parsed.path == f"/r/{_FORM_ID}"
    q = parse_qs(parsed.query)
    # Email origin => channel is a SIGNED routing token verifying back to email||<addr>.
    assert "thread_ts" not in q  # email has no thread ts; the token carries the routing
    routing = verify_routing_token(_SECRET, q["channel"][0])
    assert routing == {"channel": "email||user@corp.com", "thread_ts": ""}


# --------------------------------------------------------------------------- #
# Capability off (no signing secret) => friendly degrade, no link
# --------------------------------------------------------------------------- #
def test_capability_off_degrades() -> None:
    reply = GenerateIntent(settings=_settings(tally_on=False))(_slack_ctx())
    assert reply.text == FORMS_UNAVAILABLE_TEXT
    assert reply.slack_blocks is None


def test_custom_base_and_form_id_are_honored() -> None:
    settings = Settings(
        _env_file=None,
        tally_signing_secret="s" * 16,
        tally_base_url="https://forms.example.com/",
        tally_form_id="abc123",
    )
    reply = GenerateIntent(settings=settings)(_slack_ctx())
    parsed = urlparse(_button(reply.slack_blocks)["url"])
    assert parsed.netloc == "forms.example.com"
    assert parsed.path == "/r/abc123"
