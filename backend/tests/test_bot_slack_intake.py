"""Slack intake pipeline (PLAN §3.3) — the ported guard matrix + fail-closed dedup + dispatch seam.

Drives :meth:`SlackIntake.handle_event` directly (no Bolt, no network — a fake Slack client feeds the
thread-continuity check) so every branch is deterministic: guards drop the right events, the has-content
guard fires, dedup fails closed (a duplicate event is dropped, not reprocessed), and the dispatch seam
is called exactly once per accepted event with the row moving pending → processing → done/failed.
"""

from __future__ import annotations

from app.bot.channels import slack as slackmod
from app.bot.channels.slack import (
    IntakeOutcome,
    SlackIntake,
    is_bot_thread,
    is_human_event,
    needs_thread_gate,
)
from app.bot.models import BotInbox
from app.config import Settings

# conftest.py is frozen; the shared bot fixtures (bot_session_factory) live in conftest_bot.py.
pytest_plugins = ("conftest_bot",)


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, nda_bot_user_id="UBOT", **kw)  # type: ignore[arg-type]


class FakeSlackClient:
    """Stubs the one Web API call the pipeline makes — conversations.replies (thread fetch)."""

    def __init__(self, messages: list[dict] | None = None) -> None:
        self._messages = messages or []
        self.calls: list[dict] = []

    def conversations_replies(self, *, channel: str, ts: str, limit: int) -> dict:
        self.calls.append({"channel": channel, "ts": ts, "limit": limit})
        return {"messages": self._messages}


def _mention(text: str = "<@UBOT> review this", **over: object) -> dict:
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "ts": "111.001",
        "text": text,
    }
    event.update(over)
    return event


# ---- pure guard predicates -----------------------------------------------------------------------
def test_is_human_event_drops_bot_and_self() -> None:
    assert is_human_event({"user": "U1"}, "UBOT") is True
    assert is_human_event({"bot_id": "B1", "user": "U1"}, "UBOT") is False
    assert is_human_event({"subtype": "bot_message", "user": "U1"}, "UBOT") is False
    assert is_human_event({"user": "UBOT"}, "UBOT") is False


def test_needs_thread_gate() -> None:
    assert needs_thread_gate({"type": "message"}) is True
    assert needs_thread_gate({"type": "app_mention"}) is False
    assert needs_thread_gate({"type": "file_shared"}) is False
    assert needs_thread_gate({"type": "message", "files": [{"id": "F"}]}) is False


def test_is_bot_thread() -> None:
    assert is_bot_thread([], "UBOT") is False
    assert is_bot_thread([{"text": "hey <@UBOT> hi"}], "UBOT") is True
    assert is_bot_thread([{"text": "root"}, {"user": "UBOT"}], "UBOT") is True
    assert (
        is_bot_thread([{"text": "root"}, {"bot_profile": {"user_id": "UBOT"}}], "UBOT")
        is True
    )
    assert is_bot_thread([{"text": "root"}, {"bot_id": "BANY"}], "UBOT") is True
    assert is_bot_thread([{"text": "root"}, {"user": "U9"}], "UBOT") is False


# ---- accepted path -------------------------------------------------------------------------------
def test_mention_creates_inbox_row_and_calls_dispatch(bot_session_factory) -> None:
    seen: list = []
    intake = SlackIntake(
        _settings(), bot_session_factory, dispatch=lambda env: seen.append(env)
    )

    result = intake.handle_event(_mention(), "Ev1")

    assert result.outcome is IntakeOutcome.ACCEPTED
    assert len(seen) == 1
    assert seen[0].event_key == "slack:Ev1"
    assert seen[0].verified_sender is True  # Bolt verified the HMAC before the listener
    with bot_session_factory() as s:
        row = s.query(BotInbox).filter_by(event_key="slack:Ev1").one()
        assert row.status == "done"
        assert row.attempts == 1
        assert row.error is None
        assert row.payload_json["text"] == "<@UBOT> review this"


def test_event_key_falls_back_to_ts_when_no_event_id(bot_session_factory) -> None:
    intake = SlackIntake(_settings(), bot_session_factory, dispatch=lambda env: None)
    result = intake.handle_event(_mention(ts="222.5"), None)
    assert result.outcome is IntakeOutcome.ACCEPTED
    assert result.envelope is not None and result.envelope.event_key == "slack:222.5"


def test_file_message_bypasses_thread_gate_and_maps_attachment(
    bot_session_factory,
) -> None:
    seen: list = []
    intake = SlackIntake(
        _settings(), bot_session_factory, dispatch=lambda env: seen.append(env)
    )
    event = {
        "type": "message",
        "subtype": "file_share",
        "user": "U1",
        "channel": "C1",
        "ts": "333.1",
        "text": "",
        "files": [
            {
                "id": "F123",
                "name": "nda.docx",
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size": 4096,
                "url_private_download": "https://files.slack/nda.docx",
            }
        ],
    }
    result = intake.handle_event(event, "EvFile")
    assert result.outcome is IntakeOutcome.ACCEPTED
    env = seen[0]
    assert len(env.attachments) == 1
    assert env.attachments[0].filename == "nda.docx"
    assert env.attachments[0].source_ref == "F123"  # file id preferred over url
    assert env.attachments[0].size == 4096


# ---- guard drops ---------------------------------------------------------------------------------
def test_bot_message_dropped_no_row(bot_session_factory) -> None:
    seen: list = []
    intake = SlackIntake(
        _settings(), bot_session_factory, dispatch=lambda env: seen.append(env)
    )
    result = intake.handle_event(
        {"type": "message", "bot_id": "B1", "channel": "C1", "ts": "1.1", "text": "hi"},
        "EvBot",
    )
    assert result.outcome is IntakeOutcome.NON_HUMAN
    assert seen == []
    with bot_session_factory() as s:
        assert s.query(BotInbox).count() == 0


def test_self_message_dropped(bot_session_factory) -> None:
    intake = SlackIntake(_settings(), bot_session_factory, dispatch=lambda env: None)
    result = intake.handle_event(
        {"type": "message", "user": "UBOT", "channel": "C1", "ts": "1.1", "text": "hi"},
        "EvSelf",
    )
    assert result.outcome is IntakeOutcome.NON_HUMAN


def test_plain_message_without_thread_dropped(bot_session_factory) -> None:
    intake = SlackIntake(_settings(), bot_session_factory, dispatch=lambda env: None)
    result = intake.handle_event(
        {
            "type": "message",
            "user": "U1",
            "channel": "C1",
            "ts": "1.1",
            "text": "hello",
        },
        "EvPlain",
    )
    assert result.outcome is IntakeOutcome.THREAD_GATE


def test_plain_dm_bypasses_thread_gate_and_is_processed(bot_session_factory) -> None:
    # A 1:1 DM (``channel_type == "im"``) is inherently addressed to the bot: a plain message with no
    # @mention and no thread MUST still be processed (not dropped by the thread-continuity gate), so a
    # user can just DM "template sg company" and get a reply.
    seen: list = []
    intake = SlackIntake(
        _settings(), bot_session_factory, dispatch=lambda env: seen.append(env)
    )
    result = intake.handle_event(
        {
            "type": "message",
            "channel_type": "im",
            "user": "U1",
            "channel": "D1",
            "ts": "1.1",
            "text": "template sg company",
        },
        "EvDm",
    )
    assert result.outcome is IntakeOutcome.ACCEPTED
    assert len(seen) == 1


def test_plain_message_in_bot_thread_processed(bot_session_factory) -> None:
    seen: list = []
    client = FakeSlackClient(messages=[{"text": "root"}, {"bot_id": "BANY"}])
    intake = SlackIntake(
        _settings(), bot_session_factory, dispatch=lambda env: seen.append(env)
    )
    event = {
        "type": "message",
        "user": "U1",
        "channel": "C1",
        "ts": "9.2",
        "thread_ts": "9.1",
        "text": "yes please",
    }
    result = intake.handle_event(event, "EvThread", client=client)
    assert result.outcome is IntakeOutcome.ACCEPTED
    assert client.calls[0] == {"channel": "C1", "ts": "9.1", "limit": 30}
    assert len(seen) == 1


def test_plain_message_in_non_bot_thread_dropped(bot_session_factory) -> None:
    client = FakeSlackClient(messages=[{"text": "root"}, {"user": "U9"}])
    intake = SlackIntake(_settings(), bot_session_factory, dispatch=lambda env: None)
    event = {
        "type": "message",
        "user": "U1",
        "channel": "C1",
        "ts": "9.2",
        "thread_ts": "9.1",
        "text": "hi",
    }
    result = intake.handle_event(event, "EvThread2", client=client)
    assert result.outcome is IntakeOutcome.THREAD_GATE


def test_empty_mention_dropped_by_has_content(bot_session_factory) -> None:
    intake = SlackIntake(_settings(), bot_session_factory, dispatch=lambda env: None)
    result = intake.handle_event(_mention(text="   "), "EvEmpty")
    assert result.outcome is IntakeOutcome.NO_CONTENT
    with bot_session_factory() as s:
        assert s.query(BotInbox).count() == 0


# ---- fail-closed dedup ---------------------------------------------------------------------------
def test_duplicate_event_dropped(bot_session_factory) -> None:
    seen: list = []
    intake = SlackIntake(
        _settings(), bot_session_factory, dispatch=lambda env: seen.append(env)
    )
    first = intake.handle_event(_mention(), "EvDup")
    second = intake.handle_event(_mention(text="<@UBOT> again"), "EvDup")
    assert first.outcome is IntakeOutcome.ACCEPTED
    assert second.outcome is IntakeOutcome.DUPLICATE
    assert len(seen) == 1  # dispatch fired only for the first, uniquely-claimed event
    with bot_session_factory() as s:
        assert s.query(BotInbox).filter_by(event_key="slack:EvDup").count() == 1


# ---- dispatch seam -------------------------------------------------------------------------------
def test_dispatch_absent_marks_done(bot_session_factory, monkeypatch) -> None:
    # Force the lazy seam to report absent regardless of whether the router's dispatch.py has landed.
    monkeypatch.setattr(slackmod, "_lazy_seam", lambda name: None)
    intake = SlackIntake(_settings(), bot_session_factory)  # no injected dispatch
    result = intake.handle_event(_mention(), "EvNoDisp")
    assert result.outcome is IntakeOutcome.ACCEPTED
    with bot_session_factory() as s:
        row = s.query(BotInbox).filter_by(event_key="slack:EvNoDisp").one()
        assert row.status == "done"  # placeholder path still completes the row
        assert row.attempts == 1


def test_dispatch_failure_marks_failed(bot_session_factory) -> None:
    def boom(_env: object) -> None:
        raise RuntimeError("engine exploded")

    intake = SlackIntake(_settings(), bot_session_factory, dispatch=boom)
    result = intake.handle_event(_mention(), "EvFail")
    assert (
        result.outcome is IntakeOutcome.ACCEPTED
    )  # accepted + claimed; processing failed
    with bot_session_factory() as s:
        row = s.query(BotInbox).filter_by(event_key="slack:EvFail").one()
        assert row.status == "failed"
        assert row.attempts == 1
        assert "engine exploded" in (row.error or "")


def test_async_dispatch_is_awaited(bot_session_factory) -> None:
    seen: list = []

    async def adispatch(env: object) -> None:
        seen.append(env)

    intake = SlackIntake(_settings(), bot_session_factory, dispatch=adispatch)
    result = intake.handle_event(_mention(), "EvAsync")
    assert result.outcome is IntakeOutcome.ACCEPTED
    assert len(seen) == 1
    with bot_session_factory() as s:
        row = s.query(BotInbox).filter_by(event_key="slack:EvAsync").one()
        assert row.status == "done"


def test_interaction_defensive_noop_when_seam_absent(
    bot_session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(slackmod, "_lazy_seam", lambda name: None)
    intake = SlackIntake(_settings(), bot_session_factory)
    # Must not raise even though no interaction handler exists yet.
    intake.handle_interaction({"type": "block_actions", "actions": []})


def test_interaction_forwards_to_seam(bot_session_factory, monkeypatch) -> None:
    got: list = []
    monkeypatch.setattr(
        slackmod, "_lazy_seam", lambda name: lambda body: got.append((name, body))
    )
    intake = SlackIntake(_settings(), bot_session_factory)
    intake.handle_interaction({"type": "view_submission"})
    assert got == [("process_interaction", {"type": "view_submission"})]
