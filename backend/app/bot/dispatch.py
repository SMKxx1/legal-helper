"""The dispatch seam + the ``bot_inbox`` lifecycle helpers every intake channel shares (PLAN §3.3, §3.5).

Contract (agreed across the Slack, router, and worker builders):

* :func:`process_envelope` is the **routing** seam — the stable ``bot.dispatch.process_envelope`` entry a
  channel calls once it has ALREADY normalized, has-content-guarded, and fail-closed-dedup-CLAIMED an
  event. It binds the correlation id to the ``event_key`` and delegates to the router's
  ``route_envelope`` pipeline (deterministic router → classifier → allowlist → intent dispatch → reply).
  The CHANNEL owns the ``bot_inbox`` row and its status; ``process_envelope`` never touches the table —
  that is what lets the Slack handler (which claims + manages status inline) and the email poll loop
  feed the identical seam. It propagates exceptions so the caller records ``failed`` (``route_envelope``
  itself never raises).

* :func:`claim` / :func:`finalize` are the ``bot_inbox`` lifecycle helpers (the same insert-and-catch
  fail-closed dedup the Slack handler does inline, reference §3.3): the email intake path and the sweeper
  use them so their durable-claim + status transitions match the Slack path exactly.

* :func:`sweep_bot_inbox` is the worker's crash-recovery pass (PLAN §3.5, "for the sweep to retry"): it
  re-leases rows stuck in ``pending``/``processing`` past the visibility timeout (a crashed worker) and
  retries ``failed`` rows under a backoff + attempts cap, re-driving each through the SAME
  :func:`process_envelope`. Same conditional-claim shape as ``reviews_repo.claim_review_job``; a row that
  keeps crashing is dead-lettered rather than re-driven forever. The worker
  (``app.worker.scheduler``) schedules it on the ``BOT_INBOX_SWEEP_SECONDS`` interval under the advisory
  lock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..telemetry import bind_correlation_id, correlation_id_var, get_logger
from .envelope import Envelope
from .models import BotInbox

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from .interactivity import InteractivityDeps, InteractivityRegistry

log = get_logger("nda.bot.dispatch")

#: Visibility timeout for an in-flight claim: a routing turn (guards + a possible cheap-tier classifier
#: call + intent dispatch + reply) is seconds, so this generous lease never expires under a live turn —
#: a ``pending``/``processing`` row older than this signals a crashed worker (PLAN §3.5).
BOT_INBOX_LEASE_S = 5 * 60
#: Retry deferral for a ``failed`` row (the channel records ``failed`` "for the sweep to retry"): a
#: transient routing failure re-runs after a short backoff rather than on the very next tick.
BOT_INBOX_RETRY_BACKOFF_S = 60
#: A row reclaimed/retried this many times is dead-lettered (terminal ``failed``) — never wedge the
#: sweeper on a poison event.
BOT_INBOX_MAX_ATTEMPTS = 5

# bot_inbox.status values (shared with the Slack handler's inline lifecycle).
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: A routing callable: ``route_envelope(envelope)``. The return value is ignored (the pipeline delivers
#: its own replies). Injected in tests; resolved from ``app.bot.router`` in production.
Router = Callable[[Envelope], object]


def _default_session_factory() -> sessionmaker:
    from app.db import SessionLocal

    return SessionLocal


def _load_router() -> Router | None:
    """Resolve the router's ``route_envelope`` lazily, or ``None`` if the pipeline is not wired yet
    (then a claimed row simply lands ``done`` with nothing to route to — logged, never silent).

    ``getattr`` tolerates the interleaved build order: ``app.bot.router`` exists (the deterministic
    router) but ``route_envelope`` — the full pipeline — is landed by the router agent alongside this."""
    try:
        from . import router as _router
    except ImportError:
        return None
    fn = getattr(_router, "route_envelope", None)
    return fn if callable(fn) else None


def process_envelope(envelope: Envelope, *, router: Router | None = None) -> object:
    """Route an already-claimed, guarded envelope (the ``bot.dispatch.process_envelope`` seam).

    Binds the correlation id to ``event_key`` for the whole turn, then delegates to the router's
    ``route_envelope`` (or an injected ``router``). Returns the routing result — callers ignore it (the
    pipeline delivers its own replies). Propagates a routing exception so the CHANNEL records the
    ``failed`` status (``route_envelope`` itself never raises; this is defence in depth). Does NOT touch
    ``bot_inbox`` — the channel owns the durable row (see :func:`claim` / :func:`finalize`).
    """
    token = bind_correlation_id(envelope.event_key)
    try:
        r = router if router is not None else _load_router()
        if r is None:
            log.info("dispatch.no_router", event_key=envelope.event_key)
            return None
        return r(envelope)
    finally:
        correlation_id_var.reset(token)


def process_interaction(
    body: dict,
    *,
    registry: InteractivityRegistry | None = None,
    deps: InteractivityDeps | None = None,
) -> None:
    """Handle ONE Slack interactivity payload (button click / modal submit) — the ``process_interaction``
    seam the wave-A Bolt route resolves lazily (``app.bot.channels.slack._resolve_interaction_dispatch``).

    Mirrors :func:`process_envelope`: it runs post-ACK (the Bolt handler already ACKed <3s, reference
    §3.7), binds a correlation id derived from the interaction for the turn's structured logs, and
    delegates to the typed, versioned :func:`app.bot.interactivity.dispatch_interaction` state machine.
    Interactivity carries NO ``bot_inbox`` row (it is not a fresh event — its idempotency lives in
    ``approve_request`` and Slack's own delivery), so this never touches the dedup table. Never raises —
    ``dispatch_interaction`` is itself fail-soft."""
    token = bind_correlation_id(_interaction_correlation_id(body))
    try:
        from . import interactivity

        interactivity.dispatch_interaction(body, registry=registry, deps=deps)
    finally:
        correlation_id_var.reset(token)


def _interaction_correlation_id(body: dict) -> str:
    """A stable-ish correlation id for one interaction turn (trigger id > action ts > a constant)."""
    if not isinstance(body, dict):
        return "slack:interaction"
    trigger = body.get("trigger_id")
    if trigger:
        return f"slack:int:{trigger}"
    actions = body.get("actions") or []
    if actions:
        first = actions[0] if isinstance(actions[0], dict) else {}
        marker = first.get("action_ts") or first.get("action_id")
        if marker:
            return f"slack:int:{marker}"
    return "slack:interaction"


def claim(
    envelope: Envelope, *, session_factory: sessionmaker | None = None
) -> str | None:
    """Fail-closed dedup: INSERT the durable ``bot_inbox`` row for this event, returning its id when
    THIS caller won the claim, or ``None`` when the ``event_key`` was already present (duplicate).

    The UNIQUE ``event_key`` constraint IS the dedup — a duplicate INSERT raises ``IntegrityError``,
    translated to "already seen" (never to "assume new"). The row starts ``processing`` / ``attempts=1``
    (the caller is about to route it); a crash before :func:`finalize` leaves the reclaimable signal for
    the sweep. Mirrors the Slack handler's inline ``_claim`` so both channels persist identically."""
    factory = session_factory or _default_session_factory()
    with factory() as s:
        row = BotInbox(
            event_key=envelope.event_key,
            channel=envelope.channel,
            payload_json=envelope.model_dump(mode="json"),
            status=STATUS_PROCESSING,
            attempts=1,
        )
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return None
        return str(row.id)


def finalize(
    inbox_id: str,
    status: str,
    *,
    error: str | None = None,
    session_factory: sessionmaker | None = None,
    now: datetime | None = None,
) -> None:
    """Set a claimed row's terminal status (``done`` / ``failed``). ``updated_at`` is written explicitly
    (tz-aware UTC) so the sweep's visibility-timeout comparison is consistent across dialects."""
    now = now or datetime.now(UTC)
    factory = session_factory or _default_session_factory()
    with factory() as s:
        s.execute(
            update(BotInbox)
            .where(BotInbox.id == inbox_id)
            .values(status=status, error=(error or None), updated_at=now)
            .execution_options(synchronize_session=False)
        )
        s.commit()


def _reclaim_one(
    factory: sessionmaker,
    *,
    lease_cutoff: datetime,
    retry_cutoff: datetime,
    now: datetime,
    max_attempts: int,
) -> tuple[str, dict] | str | None:
    """Atomically re-lease ONE runnable row (crashed ``pending``/``processing`` past the lease, or a
    ``failed`` row due for retry). Mirrors ``reviews_repo.claim_review_job``: the conditional UPDATE
    re-checks the runnable condition so two sweepers never re-drive the same row. Returns
    ``(row_id, payload)`` on a win, ``"dead"`` when a row was dead-lettered / a race was lost (keep
    scanning), or ``None`` when nothing is runnable."""
    runnable = or_(
        and_(
            BotInbox.status.in_((STATUS_PENDING, STATUS_PROCESSING)),
            BotInbox.updated_at < lease_cutoff,
        ),
        and_(
            BotInbox.status == STATUS_FAILED,
            BotInbox.attempts < max_attempts,
            BotInbox.updated_at < retry_cutoff,
        ),
    )
    with factory() as s:
        candidate = s.execute(
            select(BotInbox)
            .where(runnable)
            .order_by(BotInbox.updated_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if candidate is None:
            return None

        # A crashed in-flight row that has already burned its attempts is a poison event: dead-letter it
        # (terminal ``failed``) instead of reclaiming forever. (``failed`` candidates always satisfy
        # ``attempts < max`` via the runnable clause, so only pending/processing reach this branch.)
        if (
            candidate.status in (STATUS_PENDING, STATUS_PROCESSING)
            and candidate.attempts >= max_attempts
        ):
            s.execute(
                update(BotInbox)
                .where(
                    BotInbox.id == candidate.id,
                    BotInbox.status.in_((STATUS_PENDING, STATUS_PROCESSING)),
                )
                .values(
                    status=STATUS_FAILED,
                    error=f"attempts exhausted ({candidate.attempts}); dead-lettered by sweep",
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            s.commit()
            log.warning(
                "dispatch.dead_letter",
                event_key=candidate.event_key,
                attempts=candidate.attempts,
            )
            return "dead"

        row_id = str(candidate.id)
        payload = dict(candidate.payload_json or {})
        res = s.execute(
            update(BotInbox)
            .where(BotInbox.id == row_id, runnable)
            .values(
                status=STATUS_PROCESSING, attempts=BotInbox.attempts + 1, updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        s.commit()
        if (getattr(res, "rowcount", 0) or 0) != 1:
            return "dead"  # lost the race — treat as "keep scanning"
        log.info(
            "dispatch.reclaimed",
            event_key=candidate.event_key,
            attempts=candidate.attempts + 1,
        )
        return row_id, payload


def sweep_bot_inbox(
    now: datetime | None = None,
    *,
    lease_s: int = BOT_INBOX_LEASE_S,
    retry_backoff_s: int = BOT_INBOX_RETRY_BACKOFF_S,
    max_attempts: int = BOT_INBOX_MAX_ATTEMPTS,
    session_factory: sessionmaker | None = None,
    router: Router | None = None,
    limit: int = 50,
) -> int:
    """Reclaim + re-drive rows the intake path never finished (PLAN §3.5): a crashed worker's stuck
    ``pending``/``processing`` rows (visibility timeout) and ``failed`` rows the channel left "for the
    sweep to retry" (backoff + attempts cap).

    Returns the number of rows re-driven this pass (dead-lettered rows are not counted). Bounded by
    ``limit`` so one tick can never run unbounded. Each reclaimed row is routed through the SAME
    :func:`process_envelope` as fresh intake — behavior is identical whether an event is handled on
    arrival or recovered after a crash. Never raises (worker-job discipline)."""
    now = now or datetime.now(UTC)
    lease_cutoff = now - timedelta(seconds=lease_s)
    retry_cutoff = now - timedelta(seconds=retry_backoff_s)
    factory = session_factory or _default_session_factory()
    redriven = 0
    while redriven < limit:
        claimed = _reclaim_one(
            factory,
            lease_cutoff=lease_cutoff,
            retry_cutoff=retry_cutoff,
            now=now,
            max_attempts=max_attempts,
        )
        if claimed is None:
            break
        if isinstance(claimed, str):  # the "dead" sentinel — dead-lettered or lost race
            continue  # keep scanning for the next runnable row
        row_id, payload = claimed
        try:
            envelope = Envelope.model_validate(payload)
        except Exception:  # noqa: BLE001 — a corrupt persisted payload must not wedge the sweep
            log.exception("dispatch.reclaim_bad_payload", row_id=row_id)
            finalize(
                row_id,
                STATUS_FAILED,
                error="unparseable persisted envelope",
                session_factory=factory,
                now=now,
            )
            continue
        token = bind_correlation_id(envelope.event_key)
        try:
            process_envelope(envelope, router=router)
            finalize(row_id, STATUS_DONE, session_factory=factory)
        except Exception as exc:  # noqa: BLE001 — routing failure is recorded; sweep never crashes
            log.exception("dispatch.redrive_failed", event_key=envelope.event_key)
            finalize(
                row_id,
                STATUS_FAILED,
                error=f"{type(exc).__name__}: routing failed (see server logs)",
                session_factory=factory,
            )
        finally:
            correlation_id_var.reset(token)
        redriven += 1
    return redriven
