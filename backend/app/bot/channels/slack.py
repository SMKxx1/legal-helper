"""Slack intake (PLAN §3.3) — the Bolt events/interactivity front door + the ported guard pipeline.

This is the in-process replacement for the n8n ``NDA: Router`` Slack half (reference §3.1) and the
``NDA: Interactivity`` webhook (reference §3.7). Two FastAPI routes are mounted through the slack-bolt
FastAPI adapter — ``/slack/events`` and ``/slack/interactivity`` — and Bolt performs the fail-closed v0
HMAC verification (300s replay window) before any handler runs. Mounting is capability-gated: with the
``slack`` capability disabled the routes answer a clean 503 and the Bolt app is never constructed, so a
missing token/secret degrades the channel without ever crashing boot.

ACK-then-process (PLAN §3.3): Bolt is built with ``process_before_response=False`` — the documented
server behavior is *ack the event immediately, then run the listener in a background thread* — so the
Slack 3s budget is never spent on the pipeline below. The listener body is :meth:`SlackIntake.handle_event`,
kept a pure synchronous function so it is exercised directly (no Bolt, no network) in tests:

    guards (human-event → thread-continuity) → normalize → has-content → fail-closed dedup → dispatch

The guards are ported verbatim from the reference Router chain (reference §3.1): the human-event filter
(drop the bot's own / other bots' messages), the thread-continuity gate (a plain, non-mention,
file-less message is processed only inside a thread the bot participates in — up to 30 replies scanned),
and the has-content guard. Dedup is the UNIQUE ``bot_inbox.event_key`` insert (fail closed — a duplicate
can't be claimed). Accepted events are handed to the pluggable ``bot.dispatch.process_envelope`` seam
(the router agent's dispatcher, imported lazily/defensively); until it exists, processing is marked
done by a logged placeholder. Every inbox row moves ``pending → processing → done|failed`` with an
attempt count, so the worker sweep (``BOT_INBOX_SWEEP_SECONDS``) can re-drive a crash.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from ...capabilities import SLACK, CapabilityRegistry, CapabilityState
from ...config import Settings
from ...telemetry import bind_correlation_id, correlation_id_var, get_logger
from ..envelope import AttachmentRef, Envelope
from ..models import BotInbox

if (
    TYPE_CHECKING
):  # avoid importing FastAPI/session types at module load beyond what mounting needs
    from fastapi import FastAPI
    from sqlalchemy.orm import sessionmaker

log = get_logger("nda.bot.slack")

#: A callable that turns an accepted envelope into work. The router agent supplies the real one at
#: ``app.bot.dispatch.process_envelope``; it may be sync or return an awaitable.
Dispatch = Callable[[Envelope], Any]

#: A zero-arg factory yielding a SQLAlchemy ``Session`` (a ``sessionmaker`` satisfies this).
SessionFactory = Callable[[], Any]


# ==================================================================================================
# Ported guards (reference §3.1 "Slack guard chain") — pure predicates, unit-tested directly.
# ==================================================================================================
def is_human_event(event: dict[str, Any], bot_user_id: str) -> bool:
    """The ported ``Human Slack Event?`` guard: drop bot-authored and self events (reference §3.1).

    True only when ``bot_id`` is absent AND ``subtype != 'bot_message'`` AND the author is not the bot
    itself (``NDA_BOT_USER_ID``). This is the bot-loop prevention that keeps the bot from replying to
    its own posts.
    """
    if event.get("bot_id"):
        return False
    if event.get("subtype") == "bot_message":
        return False
    user = event.get("user") or event.get("user_id") or ""
    return not (bot_user_id and user == bot_user_id)


def needs_thread_gate(event: dict[str, Any]) -> bool:
    """The ported ``Needs Thread Gate?`` predicate (reference §3.1).

    A message needs the bot-thread-continuity check when it is NOT an ``app_mention``, NOT a
    ``file_shared`` event, and carries no files — i.e. a plain chat message. Mentions and file uploads
    are always in-scope and bypass the gate.
    """
    etype = event.get("type", "")
    files = event.get("files") or []
    return etype != "app_mention" and etype != "file_shared" and len(files) == 0


def is_bot_thread(messages: list[dict[str, Any]], bot_user_id: str) -> bool:
    """The ported ``Check Bot Thread`` predicate (reference §3.1).

    True when the bot participates in the thread: it is @-mentioned in the root message text, OR any
    message in the thread was posted by the bot (``user`` / ``bot_profile.user_id`` == the bot id) OR
    by any bot at all (a bare ``bot_id`` present). Empty thread => not a bot thread.
    """
    if not messages:
        return False
    root_text = messages[0].get("text", "") or ""
    if bot_user_id and f"<@{bot_user_id}>" in root_text:
        return True
    for m in messages:
        if bot_user_id and m.get("user") == bot_user_id:
            return True
        bot_profile = m.get("bot_profile") or {}
        if bot_user_id and bot_profile.get("user_id") == bot_user_id:
            return True
        if m.get("bot_id"):
            return True
    return False


# ==================================================================================================
# Normalization (reference §3.1 "Normalize (Slack)") — event dict -> canonical Envelope.
# ==================================================================================================
def slack_event_key(event: dict[str, Any], event_id: str | None) -> str:
    """The ported dedup key: ``'slack:' + (event_id ?? event.ts)`` (reference §3.1)."""
    return "slack:" + (event_id or event.get("ts") or "")


def _attachments_from_files(files: list[dict[str, Any]]) -> tuple[AttachmentRef, ...]:
    """Map Slack ``files[]`` to inbound :class:`AttachmentRef`s (metadata + a lazy source handle).

    ``source_ref`` prefers the stable Slack file ``id`` (resolved to bytes later via ``files.info``),
    falling back to ``url_private_download`` — the hand-off contract from the foundation agent.
    """
    refs: list[AttachmentRef] = []
    for f in files or []:
        refs.append(
            AttachmentRef(
                filename=f.get("name", "") or "",
                content_type=f.get("mimetype", "") or f.get("filetype", "") or "",
                size=int(f.get("size", 0) or 0),
                source_ref=f.get("id", "") or f.get("url_private_download", "") or "",
            )
        )
    return tuple(refs)


def normalize_slack_event(
    event: dict[str, Any], event_id: str | None, *, from_email: str
) -> Envelope:
    """Build the canonical :class:`Envelope` from a Slack event (reference §3.1 ``Normalize (Slack)``).

    ``verified_sender=True`` because Bolt has already verified the v0 HMAC signature before this runs
    (PLAN §3.3, §6 — the Slack path sets it only post-verification). Raises ``ValueError`` (via the
    Envelope validator) if the dedup key is empty — a normalization bug the caller drops safely.
    """
    files = event.get("files") or []
    ts = event.get("ts", "") or ""
    return Envelope(
        channel="slack",
        event_key=slack_event_key(event, event_id),
        text=event.get("text", "") or "",
        sender_id=event.get("user") or event.get("user_id") or "",
        sender_address="",
        verified_sender=True,
        slack_channel=event.get("channel") or event.get("channel_id") or "",
        slack_thread_ts=event.get("thread_ts") or ts,
        from_email=from_email,
        attachments=_attachments_from_files(files),
    )


# ==================================================================================================
# Intake pipeline.
# ==================================================================================================
class IntakeOutcome(str, Enum):
    """Terminal outcome of :meth:`SlackIntake.handle_event` — one per drop reason plus ``ACCEPTED``."""

    NON_HUMAN = "dropped_non_human"
    THREAD_GATE = "dropped_thread_gate"
    MALFORMED = "dropped_malformed"
    NO_CONTENT = "dropped_no_content"
    DUPLICATE = "dropped_duplicate"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class IntakeResult:
    """What the pipeline did — the outcome plus the built envelope + inbox id when one was created."""

    outcome: IntakeOutcome
    envelope: Envelope | None = None
    inbox_id: str | None = None


class SlackIntake:
    """The event→dispatch pipeline (reference §3.1). Holds no network state; deps are injected.

    ``session_factory`` yields sessions bound to the app DB (dedup + status writes). ``dispatch`` is the
    optional processing seam — when ``None`` it is resolved lazily from ``app.bot.dispatch`` at first
    use, falling back to a logged placeholder if that module isn't present yet (the router agent lands
    it concurrently). ``handle_event`` runs the whole pipeline synchronously so a test drives it with a
    fake Slack client and asserts the inbox row + dispatch call deterministically.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory,
        *,
        dispatch: Dispatch | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._dispatch = dispatch

    # -- public entry points ------------------------------------------------
    def handle_event(
        self,
        event: dict[str, Any],
        event_id: str | None = None,
        *,
        client: Any | None = None,
    ) -> IntakeResult:
        """Run guards → normalize → has-content → dedup → dispatch for one Slack event."""
        bot_user_id = self._settings.nda_bot_user_id or ""

        # 1) Human-event filter (drop bot/self events).
        if not is_human_event(event, bot_user_id):
            log.debug("slack.intake.drop.non_human", subtype=event.get("subtype"))
            return IntakeResult(IntakeOutcome.NON_HUMAN)

        # 2) Thread-continuity gate: a plain (non-mention, file-less) message is in-scope only inside a
        #    thread the bot participates in — EXCEPT a direct message (``channel_type == "im"``), which
        #    is inherently addressed to the bot and is always processed. Without this, a 1:1 DM like
        #    "template sg company" (no @mention, no thread) is silently dropped and the bot never replies.
        if needs_thread_gate(event) and event.get("channel_type") != "im":
            thread_ts = event.get("thread_ts")
            if not thread_ts:
                log.debug("slack.intake.drop.thread_gate", reason="not_a_thread_reply")
                return IntakeResult(IntakeOutcome.THREAD_GATE)
            messages = self._fetch_thread(client, event.get("channel", ""), thread_ts)
            if not is_bot_thread(messages, bot_user_id):
                log.debug("slack.intake.drop.thread_gate", reason="not_a_bot_thread")
                return IntakeResult(IntakeOutcome.THREAD_GATE)

        # 3) Normalize to the canonical envelope (fail-closed on an empty dedup key).
        try:
            envelope = normalize_slack_event(
                event, event_id, from_email=self._settings.nda_bot_from_email
            )
        except ValueError as exc:
            log.warning("slack.intake.drop.malformed", error=str(exc))
            return IntakeResult(IntakeOutcome.MALFORMED)

        token = bind_correlation_id(envelope.event_key)
        try:
            # 4) Has-content guard (reference §3.1 ``Has Content?``).
            if not envelope.has_content:
                log.debug("slack.intake.drop.no_content", event_key=envelope.event_key)
                return IntakeResult(IntakeOutcome.NO_CONTENT, envelope)

            # 5) Fail-closed dedup: UNIQUE insert on event_key.
            inbox_id = self._claim(envelope)
            if inbox_id is None:
                log.info("slack.intake.drop.duplicate", event_key=envelope.event_key)
                return IntakeResult(IntakeOutcome.DUPLICATE, envelope)

            # 6) Process (dispatch). Off the ack path in prod (Bolt bg thread); inline in tests.
            log.info(
                "slack.intake.accepted",
                event_key=envelope.event_key,
                inbox_id=inbox_id,
                attachments=len(envelope.attachments),
            )
            self._process(inbox_id, envelope)
            return IntakeResult(IntakeOutcome.ACCEPTED, envelope, inbox_id)
        finally:
            correlation_id_var.reset(token)

    def handle_interaction(self, body: dict[str, Any]) -> None:
        """Post-ack handling of a Slack interactivity payload (button click / modal submit).

        Bolt has already ACKed (<3s) by the time this runs. The typed interactivity state machine lands
        with the router agent (``bot.dispatch.process_interaction``); until then this is a defensive,
        logged no-op so the endpoint is live and signature-verified without depending on P3.
        """
        fn = self._resolve_interaction_dispatch()
        interaction_type = body.get("type", "")
        if fn is None:
            log.info("slack.interaction.received", type=interaction_type)
            return
        try:
            result = fn(body)
            if inspect.isawaitable(result):
                asyncio.run(_drain(result))
        except Exception:  # noqa: BLE001 - a handler failure must not break the (already-sent) ack
            log.exception("slack.interaction.failed", type=interaction_type)

    # -- internals ----------------------------------------------------------
    def _fetch_thread(
        self, client: Any | None, channel: str, thread_ts: str
    ) -> list[dict[str, Any]]:
        """Fetch up to 30 thread replies for the bot-participation check (reference §2.8).

        A missing client or an API failure yields ``[]`` — the gate then treats the thread as
        non-bot (fail-closed: a plain reply we can't verify is dropped rather than processed).
        """
        if client is None or not channel:
            return []
        try:
            resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=30)
            return list(resp.get("messages", []) or [])
        except Exception as exc:  # noqa: BLE001 - degrade to "not a bot thread", never raise
            log.warning(
                "slack.intake.thread_fetch_failed", channel=channel, error=repr(exc)
            )
            return []

    def _claim(self, envelope: Envelope) -> str | None:
        """Insert the ``bot_inbox`` dedup row; return its id, or ``None`` if the event was already seen.

        The insert-and-catch-IntegrityError pattern IS the fail-closed dedup (PLAN §3.3): a duplicate
        ``event_key`` violates the UNIQUE constraint, so it can never be re-claimed. The envelope is
        persisted (``model_dump(mode='json')`` — datetimes → ISO strings for the JSON column) so
        processing survives a restart.
        """
        session = self._session_factory()
        try:
            row = BotInbox(
                event_key=envelope.event_key,
                channel=envelope.channel,
                payload_json=envelope.model_dump(mode="json"),
            )
            session.add(row)
            session.commit()
            return str(row.id)
        except IntegrityError:
            session.rollback()
            return None
        finally:
            session.close()

    def _process(self, inbox_id: str, envelope: Envelope) -> None:
        """Drive the accepted envelope through the dispatch seam, moving the inbox row's status.

        ``pending → processing`` (attempt+1) → ``done`` on success / ``failed`` (with error) on an
        exception. A dispatch that returns an awaitable is run to completion (we are off the ack path).
        """
        dispatch = self._resolve_dispatch()
        self._set_status(inbox_id, "processing", bump_attempts=True)
        try:
            if dispatch is None:
                log.info(
                    "slack.dispatch.absent_placeholder",
                    event_key=envelope.event_key,
                    inbox_id=inbox_id,
                )
            else:
                result = dispatch(envelope)
                if inspect.isawaitable(result):
                    asyncio.run(_drain(result))
            self._set_status(inbox_id, "done")
        except Exception as exc:  # noqa: BLE001 - failure is recorded on the row for the sweep to retry
            log.exception("slack.dispatch.failed", event_key=envelope.event_key)
            self._set_status(inbox_id, "failed", error=str(exc)[:1000])

    def _set_status(
        self,
        inbox_id: str,
        status: str,
        *,
        bump_attempts: bool = False,
        error: str | None = None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            row = session.get(BotInbox, inbox_id)
            if row is None:
                return
            row.status = status
            if bump_attempts:
                row.attempts = (row.attempts or 0) + 1
            if error is not None:
                row.error = error
            elif status != "failed":
                row.error = None

    def _resolve_dispatch(self) -> Dispatch | None:
        if self._dispatch is not None:
            return self._dispatch
        return _lazy_seam("process_envelope")

    def _resolve_interaction_dispatch(self) -> Callable[[dict[str, Any]], Any] | None:
        return _lazy_seam("process_interaction")


async def _drain(awaitable: Any) -> None:
    """Await a value the dispatch seam returned (a coroutine) to completion — used when the seam is
    async and we are off the ack path (Bolt's background thread), so blocking here is fine."""
    await awaitable


def _lazy_seam(name: str) -> Callable[..., Any] | None:
    """Import ``app.bot.dispatch.<name>`` defensively; ``None`` if the module/attr isn't present."""
    try:
        from .. import (
            dispatch,  # local import: the router agent lands this concurrently
        )
    except Exception:  # noqa: BLE001 - module genuinely absent at runtime is an expected state
        return None
    fn = getattr(dispatch, name, None)
    return fn if callable(fn) else None


# ==================================================================================================
# Bolt app + FastAPI mounting (capability-gated, fail-closed verification, boot-safe).
# ==================================================================================================
def _static_authorize(settings: Settings) -> Callable[..., Any]:
    """A no-network ``authorize`` for Bolt: return a static ``AuthorizeResult`` from configured values.

    Bolt's default single-team authorize calls ``auth.test`` on every request to resolve the bot ids;
    supplying our own authorize (bot token + ``NDA_BOT_USER_ID``) keeps boot and every request free of
    a Slack round-trip — the boot-time ``token_verification`` call is disabled for the same reason.
    """
    from slack_bolt.authorization import AuthorizeResult

    def authorize(*_args: Any, **_kwargs: Any) -> Any:
        return AuthorizeResult(
            enterprise_id=None,
            team_id=None,
            bot_user_id=settings.nda_bot_user_id or None,
            bot_id=None,
            bot_token=settings.slack_bot_token or None,
        )

    return authorize


def build_bolt_app(settings: Settings, intake: SlackIntake) -> Any:
    """Construct the slack-bolt ``App``: fail-closed HMAC verification, ack-then-process, no network.

    ``request_verification_enabled=True`` is the fail-closed v0 HMAC gate (300s replay window) Bolt
    runs before any listener. ``process_before_response=False`` acks the event first, then runs the
    listener in Bolt's background thread — the ACK-then-process contract (PLAN §3.3). Requires the
    signing secret, so it is only ever built when the slack capability is enabled.
    """
    from slack_bolt import App

    app = App(
        signing_secret=settings.slack_signing_secret,
        token=settings.slack_bot_token or None,
        token_verification_enabled=False,  # no auth.test at boot (no network)
        request_verification_enabled=True,  # fail-closed v0 HMAC (reference §3.7, §6)
        ignoring_self_events_enabled=True,  # Bolt drops self events; our guard also does (belt+braces)
        process_before_response=False,  # ack first, run the listener in the bg thread
        raise_error_for_unhandled_request=False,
        authorize=_static_authorize(settings),
    )

    def _on_event(body: dict[str, Any], event: dict[str, Any], client: Any) -> None:
        intake.handle_event(event, body.get("event_id"), client=client)

    # Subscribed events (reference §3.1 Slack Trigger): app_mention, message (carries file_share
    # subtype + files), and file_shared. The human-event guard drops subtype/bot noise inside handle_event.
    app.event("app_mention")(_on_event)
    app.event("message")(_on_event)
    app.event("file_shared")(_on_event)

    _any = re.compile(r".*")

    def _on_action(ack: Callable[..., Any], body: dict[str, Any]) -> None:
        ack()  # <3s ack (reference §3.7); real handling is post-ack
        intake.handle_interaction(body)

    def _on_view(ack: Callable[..., Any], body: dict[str, Any]) -> None:
        ack()
        intake.handle_interaction(body)

    app.action(_any)(_on_action)
    app.view(_any)(_on_view)
    return app


def _slack_enabled(settings: Settings, registry: CapabilityRegistry | None) -> bool:
    if registry is not None:
        return registry.state(SLACK) is CapabilityState.ENABLED
    return settings.is_configured("slack_bot_token", "slack_signing_secret")


def _mount_disabled(app: FastAPI) -> None:
    """Mount 503 stubs for both Slack routes when the capability is off (boot never crashes)."""
    from fastapi.responses import JSONResponse

    async def _disabled() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "slack_disabled",
                    "message": "The Slack channel is not configured.",
                    "details": {},
                }
            },
        )

    app.add_api_route("/slack/events", _disabled, methods=["POST"])
    app.add_api_route("/slack/interactivity", _disabled, methods=["POST"])


def mount_slack(
    app: FastAPI,
    settings: Settings,
    *,
    registry: CapabilityRegistry | None = None,
    session_factory: sessionmaker | None = None,
    intake: SlackIntake | None = None,
) -> None:
    """Mount ``/slack/events`` + ``/slack/interactivity`` (PLAN §3.3), capability-gated and boot-safe.

    Disabled capability → clean 503 stubs, no Bolt app. Enabled → the Bolt app (fail-closed HMAC) behind
    the FastAPI adapter. Any construction failure marks the capability unhealthy and falls back to the
    503 stubs, so a misconfiguration degrades the channel instead of crashing the process.
    """
    if not _slack_enabled(settings, registry):
        log.info("slack.mount.disabled")
        _mount_disabled(app)
        return

    try:
        from slack_bolt.adapter.fastapi import SlackRequestHandler

        from ...db import SessionLocal

        resolved_intake = intake or SlackIntake(
            settings, session_factory or SessionLocal
        )
        bolt_app = build_bolt_app(settings, resolved_intake)
        handler = SlackRequestHandler(bolt_app)
    except Exception as exc:  # noqa: BLE001 - never let Slack wiring crash boot
        log.error("slack.mount.failed", error=repr(exc))
        if registry is not None:
            try:
                registry.mark_unhealthy(SLACK, f"mount failed: {exc!r}")
            except Exception:  # noqa: BLE001 - registry is best-effort here
                pass
        _mount_disabled(app)
        return

    async def slack_events(request: Request) -> Any:
        return await handler.handle(request)

    async def slack_interactivity(request: Request) -> Any:
        return await handler.handle(request)

    app.add_api_route("/slack/events", slack_events, methods=["POST"])
    app.add_api_route("/slack/interactivity", slack_interactivity, methods=["POST"])
    log.info("slack.mount.enabled", routes=["/slack/events", "/slack/interactivity"])
