"""Boot wiring of the reply pipeline (app/bot/delivery.py): config groups -> sinks -> router."""

from __future__ import annotations

import pytest

from app.bot import router
from app.bot.delivery import wire_delivery
from app.config import Settings


@pytest.fixture(autouse=True)
def _reset_router_delivery():
    yield
    router.reset_delivery()


def _settings(**overrides: str) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_nothing_configured_wires_an_empty_service_and_router_stays_no_op():
    service = wire_delivery(_settings())
    assert not service.has_channel("slack")
    assert not service.has_channel("email")
    # configure_delivery WAS called (the router has a service), but with no sinks a delivery
    # degrades to the fail-soft no-sink result rather than the no-service log path.
    assert router._DELIVERY is not None
    assert router._DELIVERY[1] is None  # no post_blocks without a Slack sink


def test_slack_config_wires_the_slack_sink_and_post_blocks():
    service = wire_delivery(
        _settings(slack_bot_token="xoxb-test", slack_signing_secret="sig-test")
    )
    assert service.has_channel("slack")
    assert not service.has_channel("email")
    assert router._DELIVERY is not None
    assert router._DELIVERY[0] is service
    assert callable(router._DELIVERY[1])  # SlackReplySink.post_blocks


def test_smtp_config_wires_the_email_sink_without_post_blocks():
    service = wire_delivery(
        _settings(smtp_host="mail.test", smtp_user="bot", smtp_password="pw")
    )
    assert service.has_channel("email")
    assert not service.has_channel("slack")
    assert router._DELIVERY is not None
    assert router._DELIVERY[1] is None


def test_both_channels_wire_together():
    service = wire_delivery(
        _settings(
            slack_bot_token="xoxb-test",
            slack_signing_secret="sig-test",
            smtp_host="mail.test",
            smtp_user="bot",
            smtp_password="pw",
        )
    )
    assert service.has_channel("slack")
    assert service.has_channel("email")
