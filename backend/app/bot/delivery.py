"""Boot wiring for the bot reply pipeline (PLAN §3.3 step 5) — the integration seam wave A left open.

Called once per process (``create_app`` for the api, ``run_worker`` for the worker): builds the
channel-aware :class:`~app.bot.channels.replies.ReplyService` from whichever channel sinks this
process's configuration enables, and injects it into the router via
:func:`~app.bot.router.configure_delivery`. The config-group checks mirror the capability
required-keys exactly (``slack`` / ``email_out`` in :mod:`app.capabilities`) so a capability the
registry reports as disabled never gets a sink.

Fail-soft by construction: with nothing configured the router keeps its loud
``bot.deliver.skipped_no_service`` no-op — replies are computed, logged, and dropped, never raised.
"""

from __future__ import annotations

from ..config import Settings
from ..telemetry import get_logger
from .channels.email_out import EmailReplySink
from .channels.replies import ReplyService, SlackReplySink
from .router import PostBlocks, configure_delivery

log = get_logger("nda.bot.delivery")


def wire_delivery(settings: Settings) -> ReplyService:
    """Build the per-process ReplyService from config and wire it into the router. Idempotent."""
    sinks: list[SlackReplySink | EmailReplySink] = []
    post_blocks: PostBlocks | None = None

    if settings.is_configured("slack_bot_token", "slack_signing_secret"):
        # Deferred import: slack_sdk is pinned, but keeping it out of module import scope means a
        # broken/absent package degrades this channel instead of breaking process boot.
        from slack_sdk import WebClient

        slack_sink = SlackReplySink(WebClient(token=settings.slack_bot_token))
        sinks.append(slack_sink)
        post_blocks = slack_sink.post_blocks

    if settings.is_configured("smtp_host", "smtp_user", "smtp_password"):
        sinks.append(EmailReplySink(settings))

    service = ReplyService(sinks)
    configure_delivery(service, post_blocks)
    log.info(
        "bot.delivery.wired",
        channels=sorted(s.channel for s in sinks),
        interactive_blocks=post_blocks is not None,
    )
    return service
