"""The dispatch seam + bot_inbox lifecycle: routing delegation, fail-closed dedup claim, crash sweep.

No network, throwaway SQLite (``bot_session_factory`` from conftest_bot). Contract (Option B, shared with
the Slack handler + router): ``process_envelope`` is ROUTING-ONLY; the channel owns the has-content
guard + dedup ``claim`` + status ``finalize``. ``sweep_bot_inbox`` reclaims crashed
``pending``/``processing`` rows and retries ``failed`` rows (PLAN §3.5), mirroring the review-job claimer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.bot import dispatch
from app.bot.dispatch import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    claim,
    finalize,
    process_envelope,
    sweep_bot_inbox,
)
from app.bot.envelope import Envelope
from app.bot.models import BotInbox

# conftest.py is frozen; bot fixtures live in conftest_bot and are registered as a plugin.
pytest_plugins = ("conftest_bot",)


class _CaptureRouter:
    def __init__(self, result: object = None) -> None:
        self.calls: list[Envelope] = []
        self._result = result

    def __call__(self, env: Envelope) -> object:
        self.calls.append(env)
        return self._result


def _env(event_key: str = "email:e1", **over) -> Envelope:
    base = dict(channel="email", event_key=event_key, text="please review this NDA")
    base.update(over)
    return Envelope(**base)


def _rows(factory) -> list[BotInbox]:
    with factory() as s:
        return list(s.query(BotInbox).all())


def _row(factory, row_id: str) -> BotInbox:
    with factory() as s:
        return s.get(BotInbox, row_id)


# --------------------------------------------------------------------------- #
# process_envelope: ROUTING ONLY (no bot_inbox side effects)
# --------------------------------------------------------------------------- #
def test_process_envelope_delegates_to_router():
    router = _CaptureRouter(result="routed")
    out = process_envelope(_env(), router=router)
    assert out == "routed"
    assert [e.event_key for e in router.calls] == ["email:e1"]


def test_process_envelope_no_router_returns_none(monkeypatch):
    # Router pipeline not wired -> lands with nothing to route to, never raises.
    monkeypatch.setattr(dispatch, "_load_router", lambda: None)
    assert process_envelope(_env(), router=None) is None


def test_process_envelope_propagates_router_exception():
    def boom(_env):
        raise RuntimeError("router blew up")

    # The CHANNEL records 'failed' on an exception, so process_envelope must propagate it.
    raised = False
    try:
        process_envelope(_env(), router=boom)
    except RuntimeError:
        raised = True
    assert raised


def test_process_envelope_touches_no_bot_inbox_row(bot_session_factory):
    # Routing-only: it must not claim/persist. (The channel owns bot_inbox.)
    process_envelope(_env(), router=_CaptureRouter())
    assert _rows(bot_session_factory) == []


# --------------------------------------------------------------------------- #
# claim: fail-closed dedup
# --------------------------------------------------------------------------- #
def test_claim_inserts_a_processing_row(bot_session_factory):
    env = _env(sender_address="a@partner.com", verified_sender=True)
    inbox_id = claim(env, session_factory=bot_session_factory)
    assert inbox_id
    row = _row(bot_session_factory, inbox_id)
    assert row.status == STATUS_PROCESSING
    assert row.attempts == 1
    assert Envelope.model_validate(row.payload_json) == env


def test_claim_is_fail_closed_dedup(bot_session_factory):
    first = claim(_env(), session_factory=bot_session_factory)
    second = claim(_env(), session_factory=bot_session_factory)
    assert first is not None
    assert second is None  # duplicate event_key -> not re-claimed
    assert len(_rows(bot_session_factory)) == 1


# --------------------------------------------------------------------------- #
# finalize: terminal status transitions
# --------------------------------------------------------------------------- #
def test_finalize_marks_done(bot_session_factory):
    inbox_id = claim(_env(), session_factory=bot_session_factory)
    finalize(inbox_id, STATUS_DONE, session_factory=bot_session_factory)
    assert _row(bot_session_factory, inbox_id).status == STATUS_DONE


def test_finalize_records_failure(bot_session_factory):
    inbox_id = claim(_env(), session_factory=bot_session_factory)
    finalize(inbox_id, STATUS_FAILED, error="boom", session_factory=bot_session_factory)
    row = _row(bot_session_factory, inbox_id)
    assert row.status == STATUS_FAILED
    assert row.error == "boom"


# --------------------------------------------------------------------------- #
# sweep_bot_inbox: crash reclaim + failed retry + dead-letter (PLAN §3.5)
# --------------------------------------------------------------------------- #
def _seed(
    factory, *, event_key: str, status: str, updated_at: datetime, attempts: int = 1
) -> str:
    env = _env(event_key=event_key)
    with factory() as s, s.begin():
        row = BotInbox(
            event_key=event_key,
            channel="email",
            payload_json=env.model_dump(mode="json"),
            status=status,
            attempts=attempts,
            created_at=updated_at,
            updated_at=updated_at,
        )
        s.add(row)
        s.flush()
        return str(row.id)


def _sweep(factory, router, now):
    return sweep_bot_inbox(
        now=now,
        lease_s=300,
        retry_backoff_s=60,
        max_attempts=5,
        session_factory=factory,
        router=router,
    )


def test_sweep_reclaims_a_stale_processing_row(bot_session_factory):
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:crash",
        status=STATUS_PROCESSING,
        updated_at=now - timedelta(minutes=30),
    )
    router = _CaptureRouter()
    assert _sweep(bot_session_factory, router, now) == 1
    assert [e.event_key for e in router.calls] == ["email:crash"]
    row = _row(bot_session_factory, rid)
    assert row.status == STATUS_DONE
    assert row.attempts == 2  # reclaim incremented the attempt count


def test_sweep_reclaims_a_stale_pending_row(bot_session_factory):
    # The Slack handler claims as 'pending' then flips to 'processing'; a crash in that window leaves a
    # stale 'pending' row the sweep must also recover.
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:pending",
        status=STATUS_PENDING,
        updated_at=now - timedelta(minutes=30),
    )
    assert _sweep(bot_session_factory, _CaptureRouter(), now) == 1
    assert _row(bot_session_factory, rid).status == STATUS_DONE


def test_sweep_retries_a_failed_row_after_backoff(bot_session_factory):
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:retry",
        status=STATUS_FAILED,
        updated_at=now - timedelta(minutes=5),  # past the 60s backoff
        attempts=1,
    )
    router = _CaptureRouter()
    assert _sweep(bot_session_factory, router, now) == 1
    assert len(router.calls) == 1
    assert _row(bot_session_factory, rid).status == STATUS_DONE


def test_sweep_does_not_retry_a_failed_row_within_backoff(bot_session_factory):
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:toosoon",
        status=STATUS_FAILED,
        updated_at=now - timedelta(seconds=30),  # inside the 60s backoff
    )
    router = _CaptureRouter()
    assert _sweep(bot_session_factory, router, now) == 0
    assert router.calls == []
    assert _row(bot_session_factory, rid).status == STATUS_FAILED


def test_sweep_leaves_a_fresh_processing_row_alone(bot_session_factory):
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:inflight",
        status=STATUS_PROCESSING,
        updated_at=now,  # live lease
    )
    router = _CaptureRouter()
    assert _sweep(bot_session_factory, router, now) == 0
    assert router.calls == []
    assert _row(bot_session_factory, rid).status == STATUS_PROCESSING


def test_sweep_dead_letters_a_processing_row_past_the_attempts_cap(bot_session_factory):
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:poison",
        status=STATUS_PROCESSING,
        updated_at=now - timedelta(minutes=30),
        attempts=5,
    )
    router = _CaptureRouter()
    assert _sweep(bot_session_factory, router, now) == 0  # dead-lettered, not re-driven
    assert router.calls == []
    row = _row(bot_session_factory, rid)
    assert row.status == STATUS_FAILED
    assert "attempts exhausted" in (row.error or "")


def test_sweep_does_not_retry_a_failed_row_past_the_attempts_cap(bot_session_factory):
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:donetrying",
        status=STATUS_FAILED,
        updated_at=now - timedelta(minutes=30),
        attempts=5,
    )
    assert _sweep(bot_session_factory, _CaptureRouter(), now) == 0
    assert _row(bot_session_factory, rid).status == STATUS_FAILED  # stays terminal


def test_sweep_marks_failed_when_redrive_routing_raises(bot_session_factory):
    now = datetime.now(UTC)
    rid = _seed(
        bot_session_factory,
        event_key="email:reboom",
        status=STATUS_PROCESSING,
        updated_at=now - timedelta(minutes=30),
    )

    def boom(_env):
        raise RuntimeError("still broken")

    assert (
        _sweep(bot_session_factory, boom, now) == 1
    )  # a row WAS re-driven (it failed routing)
    row = _row(bot_session_factory, rid)
    assert row.status == STATUS_FAILED
    assert "RuntimeError" in (row.error or "")


def test_sweep_returns_zero_when_nothing_is_runnable(bot_session_factory):
    assert (
        sweep_bot_inbox(session_factory=bot_session_factory, router=_CaptureRouter())
        == 0
    )


def test_sweep_bounds_work_by_limit(bot_session_factory):
    now = datetime.now(UTC)
    stale = now - timedelta(minutes=30)
    for i in range(5):
        _seed(
            bot_session_factory,
            event_key=f"email:s{i}",
            status=STATUS_PROCESSING,
            updated_at=stale,
        )
    router = _CaptureRouter()
    n = sweep_bot_inbox(
        now=now,
        lease_s=300,
        limit=3,
        session_factory=bot_session_factory,
        router=router,
    )
    assert n == 3
    assert len(router.calls) == 3
