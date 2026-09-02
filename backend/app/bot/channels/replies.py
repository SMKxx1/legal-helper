"""Channel-aware reply delivery (PLAN §3.3 step 5) — the Slack sink + the dispatching service.

The n8n stack split delivery into ``NDA: Reply`` (text) and ``NDA: Reply File`` (file), each an
``IF channel === 'slack'`` fork (reference §2.3, §3.8, §3.9). Here that collapses into two pieces:

* :class:`SlackReplySink` — the Slack half of the :class:`~app.bot.channels.protocol.ReplySink`
  contract. ``chat.postMessage`` (threaded) for text; ``files_upload_v2`` (threaded) for a file reply;
  a Slack-only :meth:`SlackReplySink.post_blocks` for the interactive Block Kit cards (help / template
  picker) the old handlers posted directly (reference §2.2).
* :class:`ReplyService` — the one channel-aware entry point an intent handler calls. It routes on
  ``envelope.channel`` to the registered sink (``slack`` here, ``email`` from the email agent's
  :class:`EmailReplySink`) — an EXACT match, exactly the ported ``$json.channel === 'slack'`` rule
  (reference §2.3). A missing sink degrades the turn (``ok=False``) rather than raising: delivery is a
  fail-soft capability, not a gate.

Everything is synchronous — ``chat.postMessage`` and ``smtplib`` are both blocking; async callers run a
delivery off the loop with ``await asyncio.to_thread(service.deliver, envelope, reply)``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ...telemetry import get_logger
from ..envelope import Envelope
from .protocol import OutboundAttachment, Reply, ReplyResult, ReplySink

log = get_logger("nda.bot.replies")


class SlackReplySink:
    """The Slack delivery surface (reference §3.8/§3.9 Slack branches).

    Threading + recipient come straight off the inbound :class:`Envelope`: ``slack_channel`` is the
    conversation and ``slack_thread_ts`` keeps the reply in-thread (``None`` posts to the channel root).
    ``client`` is a ``slack_sdk.WebClient`` (or any object exposing ``chat_postMessage`` /
    ``files_upload_v2`` — the tests pass a stub, so no network).
    """

    channel = "slack"

    def __init__(self, client: Any) -> None:
        self._client = client

    def deliver(self, envelope: Envelope, reply: Reply) -> ReplyResult:
        """Post ``reply`` into the envelope's Slack thread. Never raises (fail-soft)."""
        thread_ts = envelope.slack_thread_ts or None
        channel = envelope.slack_channel
        if not channel:
            return ReplyResult(
                ok=False, channel="slack", error="missing slack_channel on envelope"
            )
        try:
            if reply.attachments:
                return self._deliver_files(channel, thread_ts, reply)
            resp = self._client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=reply.text
            )
            return ReplyResult(
                ok=True,
                channel="slack",
                detail="text",
                meta={"ts": str(_get(resp, "ts", ""))},
            )
        except Exception as exc:  # noqa: BLE001 - delivery is fail-soft, never crash the turn
            log.warning(
                "bot.reply.slack.failed",
                channel=channel,
                thread_ts=thread_ts,
                error=repr(exc),
            )
            return ReplyResult(ok=False, channel="slack", error=str(exc)[:500])

    def _deliver_files(
        self, channel: str, thread_ts: str | None, reply: Reply
    ) -> ReplyResult:
        # Ported ``NDA: Reply File`` shape: the text rides as the upload's initial comment (on the
        # FIRST file only, so a multi-file reply doesn't repeat it), the filename doubles as the title.
        last: object = None
        for i, att in enumerate(reply.attachments):
            last = self._client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                filename=att.filename,
                file=att.content,
                title=att.filename,
                initial_comment=(reply.text or None) if i == 0 else None,
            )
        return ReplyResult(
            ok=True,
            channel="slack",
            detail="file",
            meta={
                "files": str(len(reply.attachments)),
                "ts": str(_get(last, "ts", "")),
            },
        )

    def post_blocks(
        self, envelope: Envelope, blocks: list[dict[str, Any]], fallback_text: str
    ) -> ReplyResult:
        """Post an interactive Block Kit card (help / template picker) into the thread.

        Slack-only and NOT part of the shared :class:`ReplySink` protocol — email has no Block Kit, so
        the block-posting handlers are channel-gated by the caller. ``fallback_text`` is the required
        notification/accessibility text Slack shows where blocks can't render.
        """
        thread_ts = envelope.slack_thread_ts or None
        try:
            resp = self._client.chat_postMessage(
                channel=envelope.slack_channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=fallback_text,
            )
            return ReplyResult(
                ok=True,
                channel="slack",
                detail="blocks",
                meta={"ts": str(_get(resp, "ts", ""))},
            )
        except Exception as exc:  # noqa: BLE001 - fail-soft
            log.warning(
                "bot.reply.slack.blocks_failed",
                channel=envelope.slack_channel,
                error=repr(exc),
            )
            return ReplyResult(ok=False, channel="slack", error=str(exc)[:500])


class ReplyService:
    """The single channel-aware reply entry point (PLAN §3.3 step 5).

    Holds one :class:`ReplySink` per channel and routes a delivery on ``envelope.channel``. Built with
    whichever sinks the process wired (Slack always; email only when the email capability is enabled),
    so a reply to a channel with no sink degrades to ``ok=False`` instead of crashing — the ported
    ``IF channel === 'slack'`` fork, made fail-soft.
    """

    def __init__(self, sinks: Iterable[ReplySink] = ()) -> None:
        self._sinks: dict[str, ReplySink] = {}
        for sink in sinks:
            self.register(sink)

    def register(self, sink: ReplySink) -> None:
        """Add/replace the sink for its channel. Idempotent; last registration wins."""
        self._sinks[sink.channel] = sink

    def has_channel(self, channel: str) -> bool:
        return channel in self._sinks

    def deliver(self, envelope: Envelope, reply: Reply) -> ReplyResult:
        """Deliver ``reply`` on the envelope's channel via the matching sink (fail-soft on a miss)."""
        sink = self._sinks.get(envelope.channel)
        if sink is None:
            log.warning(
                "bot.reply.no_sink",
                channel=envelope.channel,
                event_key=envelope.event_key,
            )
            return ReplyResult(
                ok=False,
                channel=envelope.channel,
                error=f"no reply sink for channel {envelope.channel!r}",
            )
        return sink.deliver(envelope, reply)

    def send_text(self, envelope: Envelope, text: str) -> ReplyResult:
        """Convenience: deliver a plain (Slack-mrkdwn) text reply."""
        return self.deliver(envelope, Reply(text=text))

    def send_file(
        self,
        envelope: Envelope,
        *,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        text: str = "",
    ) -> ReplyResult:
        """Convenience: deliver a single-file reply (the ported ``NDA: Reply File`` path)."""
        return self.deliver(
            envelope,
            Reply(
                text=text,
                attachments=(
                    OutboundAttachment(
                        filename=filename, content=content, content_type=content_type
                    ),
                ),
            ),
        )


def _get(resp: object, key: str, default: str) -> object:
    """Read ``key`` off a Slack ``SlackResponse`` (mapping-like) or a plain dict stub, defensively."""
    try:
        return resp[key]  # type: ignore[index]
    except Exception:  # noqa: BLE001 - the response is best-effort metadata only
        return getattr(resp, key, default)
