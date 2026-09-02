"""Scheduler jobs + worker bootstrap (P1-12).

The job functions (``idempotency_sweep``, ``process_one_review_job``) are pure + unit-tested; the
APScheduler wiring (``run_worker``) is the deployment shell that runs them on a single-replica
``worker`` app under a Postgres advisory lock so horizontal scale never double-fires. Each job is
registered independently — an exception in one job run never wedges the others (APScheduler
isolates job executions).

(The signed-principal nonce sweep was retired along with the whole SIGNED-principal plane, so this
worker owns only the idempotency sweep + the async review-job claimer.)

P2 bot core adds two more jobs to the same scheduler, both under the SAME advisory lock (so a
multi-replica worker never double-polls a mailbox or double-reclaims a row):

* :func:`imap_poll` — the IMAP intake poller (PLAN §3.1/§3.3): fetch ``UNSEEN`` mail, normalize each
  into an :class:`~app.bot.envelope.Envelope`, and feed the ``app.bot.dispatch`` seam. Scheduled only
  when the ``email_in`` capability is enabled (capability-gated fail-soft — no IMAP config, no job).
* :func:`bot_inbox_sweep` — the ``bot_inbox`` crash-recovery sweeper (PLAN §3.5): reclaim rows stuck in
  ``processing`` past the visibility timeout and re-drive them through the identical dispatch core, on
  the ``BOT_INBOX_SWEEP_SECONDS`` interval.
"""

from __future__ import annotations

import logging
from datetime import datetime

_log = logging.getLogger("nda.worker")

#: How often the IMAP poller runs. The mailbox is polled continuously in the old n8n IMAP trigger;
#: 30s is a low-latency default that keeps IDLE-less polling cheap. (Not a Settings field yet — a
#: candidate to promote to config, owned by the foundation agent, if a deployment needs to tune it.)
IMAP_POLL_INTERVAL_S = 30


def idempotency_sweep(now: datetime | None = None, session_factory=None) -> int:
    """Delete flow-step idempotency rows past retention (2.1 hardening — the stored
    generate-nda payloads are transient by design). Returns the count removed."""
    from app.support_task.bot_dal import idempotency_sweep as _sweep

    return _sweep(now=now, session_factory=session_factory)


def process_one_review_job(
    now: datetime | None = None, session_factory=None, run_engine=None
) -> str | None:
    """Claim and run ONE async review job (3.1). Returns the job id handled, or None when
    idle / at capacity. Never raises — a job failure re-queues (attempts-capped dead-letter)
    and MUST NOT wedge the scheduler's other jobs (they are registered independently, and this
    function contains its own try/except as well).

    Concurrency: bounded by the SAME settings.review_concurrency semaphore the api process
    uses — per PROCESS, so the worker has its own budget of slots (documented in config).
    The slot is checked BEFORE claiming so a full worker never burns a job attempt.
    APScheduler runs this on an interval with max_instances = review_concurrency: while one
    instance is inside a long engine run, subsequent ticks start new instances up to the cap,
    each holding one semaphore slot.
    """
    from app.api import reviews_repo, routes_v1

    sem = routes_v1._review_semaphore()
    if not sem.acquire(blocking=False):
        return None  # at capacity — don't claim (no wasted attempt)
    try:
        job = reviews_repo.claim_review_job(now, session_factory=session_factory)
        if job is None:
            return None
        job_id, token = job["job_id"], job["claim_token"]
        _log.info(
            "review job %s claimed (attempt %s, mode=%s)",
            job_id,
            job["attempts"],
            job["mode"],
        )
        try:
            # Idempotent recovery: if the review for this exact (content, mode) already
            # exists in this org — e.g. a prior attempt saved it but crashed before
            # complete_review_job — reuse it instead of re-running the PAID engine.
            prior = reviews_repo.find_existing_review(
                job["doc_sha256"], job["mode"], job["org_id"]
            )
            if prior is not None and prior.get("review_id"):
                review_id = prior["review_id"]
                _log.info(
                    "review job %s recovered existing review %s (no engine run)",
                    job_id,
                    review_id,
                )
            else:
                # The SAME engine entrypoint the sync route uses (tests monkeypatch
                # routes_v1._run_engine); text was extracted at submit time.
                engine = run_engine or routes_v1._run_engine
                result = engine(
                    job["incoming_text"],
                    mode=job["mode"],
                    playbook_version=job["playbook_version"],
                    scope=job["scope"],
                    original_text=job["original_text"],
                )
                import uuid as _uuid

                review_id = _uuid.uuid4().hex
                out = routes_v1._serialize(review_id, result)
                reviews_repo.save_review(
                    out,
                    mode=job["mode"],
                    source_channel=job["source_channel"],
                    doc_filename=job["doc_filename"],
                    doc_sha256=job["doc_sha256"],
                    norm_sha256=job["norm_sha256"],
                    org_id=job["org_id"],
                    actor_user_id=job["principal_id"][:32],
                )
            if job.get("idempotency_key"):
                reviews_repo.record_review_idempotency_key(
                    job["principal_id"],
                    job["idempotency_key"],
                    review_id,
                    job["org_id"],
                )
            reviews_repo.complete_review_job(
                job_id, review_id, claim_token=token, session_factory=session_factory
            )
            _log.info("review job %s done -> review %s", job_id, review_id)
        except Exception as e:  # noqa: BLE001 — a failed run re-queues; never crash the tick
            _log.exception("review job %s failed", job_id)
            # Store only the exception CLASS on the job (the poll endpoint returns job.error
            # to callers; raw internals stay in the server logs, mirroring the sync path's
            # masked review_failed envelope).
            reviews_repo.fail_review_job(
                job_id,
                f"review engine failed ({type(e).__name__}); see server logs",
                claim_token=token,
                session_factory=session_factory,
            )
        return job_id
    finally:
        sem.release()


# --------------------------------------------------------------------------- #
# P2 bot-core jobs: IMAP intake poll + bot_inbox crash-recovery sweep.
# Thin wrappers (like idempotency_sweep) delegating to the owning modules, so the scheduler stays a
# deployment shell. Both are worker jobs: they must NEVER raise (an exception here would only be logged
# by APScheduler, but a clean fail-soft keeps the log readable and the tick cheap).
# --------------------------------------------------------------------------- #
def imap_poll(settings=None) -> int:
    """Poll the IMAP mailbox once for UNSEEN mail and feed each message to the dispatch seam (PLAN
    §3.1/§3.3). Returns the number of messages handled. Capability-gated by the caller (only scheduled
    when ``email_in`` is enabled); also a no-op when IMAP is unconfigured. Fail-soft: a connection
    error (mailbox briefly unreachable) is logged and swallowed so the next tick simply retries."""
    from app.bot.channels import email_in
    from app.config import get_settings

    settings = settings or get_settings()
    try:
        return email_in.poll_once(settings)
    except Exception:  # noqa: BLE001 — a transient IMAP/connection error must not crash the tick
        _log.exception(
            "imap_poll failed (mailbox unreachable?); will retry next interval"
        )
        return 0


def bot_inbox_sweep(now=None, session_factory=None) -> int:
    """Reclaim + re-drive ``bot_inbox`` rows stuck in ``processing`` past the visibility timeout
    (crash recovery, PLAN §3.5). Returns the count re-driven. Delegates to
    ``app.bot.dispatch.sweep_bot_inbox`` (the module that owns the claim lifecycle, so reclaim and the
    fresh-intake claim share identical semantics)."""
    from app.bot.dispatch import sweep_bot_inbox

    try:
        return sweep_bot_inbox(now, session_factory=session_factory)
    except Exception:  # noqa: BLE001 — a sweep must never wedge the scheduler's other jobs
        _log.exception("bot_inbox_sweep failed; will retry next interval")
        return 0


# --------------------------------------------------------------------------- #
# P4 archive job: the Drive cache-folder watcher (PLAN §3.10, reference §3.11).
# Thin wrapper delegating to ``app.archive.watcher.run_watch_once`` (which owns the whole pass +
# its own capability gate). Scheduled only when ``google_drive`` is enabled, on the config-driven
# ``watcher_interval_minutes`` — the OLD 1-min-vs-5 misconfiguration fixed (reference §3.11/§5).
# --------------------------------------------------------------------------- #
def cache_folder_watch(settings=None) -> int:
    """Run ONE cache-folder watcher pass (PLAN §3.10). Returns the number of files claimed this pass.

    Capability-gated by the caller (only scheduled when ``google_drive`` is enabled) AND internally by
    ``run_watch_once`` (a disabled capability is a clean no-op). Fail-soft: any Drive/LLM error is logged
    and swallowed so the next tick simply retries — a worker job must never raise (reference §3.11: a
    failed file is left in the cache for the next pass, the tick itself never wedges the scheduler)."""
    from app.archive.watcher import run_watch_once

    try:
        return run_watch_once(settings=settings).claimed
    except Exception:  # noqa: BLE001 — a transient Drive/provider error must not crash the tick
        _log.exception("cache_folder_watch failed; will retry next interval")
        return 0


# --------------------------------------------------------------------------- #
# Postgres advisory lock — only ONE worker replica runs the schedule (no double-fire).
# --------------------------------------------------------------------------- #
_SCHED_LOCK_KEY = 0x4E44_4153  # "NDAS" — an arbitrary, stable advisory-lock id


def try_acquire_lock(conn) -> bool:
    """Best-effort single-runner lock. Postgres: pg_try_advisory_lock (held for the session). SQLite
    (dev/single-process): always True. A returned False means another worker holds it -> stand down."""
    dialect = conn.engine.dialect.name
    if dialect == "postgresql":
        return bool(
            conn.exec_driver_sql(
                "SELECT pg_try_advisory_lock(%(k)s)", {"k": _SCHED_LOCK_KEY}
            ).scalar()
        )
    return True


def run_worker() -> (
    None
):  # pragma: no cover - deployment shell (needs APScheduler + a live loop)
    """Entry point for the dedicated ``worker`` app (``python -m app.worker``). Boots settings +
    logging, acquires the Postgres advisory lock (so >1 replica never double-fires), then schedules
    the idempotency sweep + the async review-job claimer on an AsyncIOScheduler.

    Shutdown: a container stop delivers SIGTERM (SIGINT for a local Ctrl-C). The handler stops the
    loop so ``run_forever`` returns and we shut the scheduler down + release the advisory lock
    cleanly (exit 0), rather than being killed mid-tick — matching the P0 stub's graceful stop.
    """
    import asyncio
    import signal
    from types import FrameType

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.config import get_settings
    from app.db import engine
    from app.telemetry import configure_logging

    settings = get_settings()
    configure_logging(settings)

    # Reply sinks for email-originated turns processed in this process (PLAN §3.3 step 5): the IMAP
    # poller and the inbox sweeper dispatch envelopes here, so their replies need wired channels too.
    from app.bot.delivery import wire_delivery

    wire_delivery(settings)

    # Create and install an explicit event loop. On Python 3.12+ `asyncio.get_event_loop()` raises
    # RuntimeError in a thread with no running/current loop, and AsyncIOScheduler.start() calls it
    # internally — so without this the worker would crash on boot and never run a single job. We bind
    # the scheduler to THIS loop and run it ourselves.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        # Minimal + thread-safe: just stop the loop; the finally block below does the real teardown.
        _log.info("worker.shutdown signal=%s", signal.Signals(signum).name)
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    with engine.connect() as conn:
        if not try_acquire_lock(conn):
            _log.warning(
                "another scheduler worker holds the advisory lock; standing down."
            )
            loop.close()
            return
        _log.info("scheduler worker acquired the advisory lock; starting jobs.")

        slots = max(1, int(getattr(settings, "review_concurrency", 3) or 3))
        sched = AsyncIOScheduler(timezone="UTC", event_loop=loop)
        sched.add_job(idempotency_sweep, "interval", hours=1, id="idempotency_sweep")

        # bot_inbox crash-recovery sweep (PLAN §3.5) — always on; the table always exists.
        sweep_secs = max(1, int(getattr(settings, "bot_inbox_sweep_seconds", 30) or 30))
        sched.add_job(
            bot_inbox_sweep,
            "interval",
            seconds=sweep_secs,
            id="bot_inbox_sweep",
            coalesce=True,
            max_instances=1,
        )

        # IMAP intake poll (PLAN §3.1/§3.3) — capability-gated: only scheduled when the email_in
        # capability is enabled (IMAP host/user/password present). A missing mailbox = no job (the
        # channel is politely off), never a boot error.
        from app.capabilities import (
            EMAIL_IN,
            GOOGLE_DRIVE,
            CapabilityState,
            build_registry,
        )

        registry = build_registry(settings)
        if registry.state(EMAIL_IN) is CapabilityState.ENABLED:
            sched.add_job(
                imap_poll,
                "interval",
                seconds=IMAP_POLL_INTERVAL_S,
                id="imap_poll",
                coalesce=True,
                max_instances=1,
            )
            _log.info(
                "email_in enabled: IMAP poll scheduled every %ss", IMAP_POLL_INTERVAL_S
            )
        else:
            _log.info("email_in disabled (no IMAP config): IMAP poll not scheduled")

        # Cache-folder watcher (PLAN §3.10, reference §3.11) — capability-gated: only scheduled when the
        # google_drive capability is enabled (OAuth trio + destination folder id). On the config-driven
        # ``watcher_interval_minutes`` — the OLD n8n 1-min-vs-5 misconfiguration fixed (reference §5).
        # coalesce + max_instances=1 so a slow pass (many files) never stacks overlapping ticks.
        if registry.state(GOOGLE_DRIVE) is CapabilityState.ENABLED:
            watch_minutes = max(
                1, int(getattr(settings, "watcher_interval_minutes", 5) or 5)
            )
            sched.add_job(
                cache_folder_watch,
                "interval",
                minutes=watch_minutes,
                id="cache_folder_watch",
                coalesce=True,
                max_instances=1,
            )
            _log.info(
                "google_drive enabled: cache-folder watcher scheduled every %smin",
                watch_minutes,
            )
        else:
            _log.info(
                "google_drive disabled (no Drive config): cache-folder watcher not scheduled"
            )
        # Async review claimer (3.1): each instance handles ONE job start-to-finish (minutes
        # under a live engine), so max_instances = the concurrency budget — new ticks start
        # new instances while earlier ones are still mid-run; the shared per-process semaphore
        # inside process_one_review_job is the hard bound either way. Jobs are independent:
        # a claimer exception can never wedge idempotency_sweep (and vice versa).
        sched.add_job(
            process_one_review_job,
            "interval",
            seconds=10,
            id="review_job_claimer",
            max_instances=slots,
            coalesce=True,
        )

        # P4 expiration: nightly straggler sweep (cron at expiration_sweep_hour_utc, capability-
        # gated inside) + the archive-hook subscription so freshly archived files get extraction
        # immediately (PLAN §3.10). Both fail-soft when Airtable/Drive/LLM config is absent.
        from app.expiration.hooks import register_archive_hook
        from app.expiration.jobs import register_expiration_jobs

        register_expiration_jobs(sched, settings)
        register_archive_hook(settings)

        sched.start()
        _log.info(
            "worker.up scheduler started (idempotency_sweep 1h, "
            "review_job_claimer 10s x%s), app_env=%s",
            slots,
            settings.app_env,
        )
        try:
            loop.run_forever()  # idle until a signal stops the loop — no busy loop
        finally:
            sched.shutdown()
            loop.close()
            _log.info("worker.stopped")
