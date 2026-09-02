"""``python -m app.worker`` — the dedicated single-replica scheduler worker (P1-12).

The API runs web-only; this process OWNS the schedule (idempotency sweep + async review-job claimer)
under a Postgres advisory lock so running it at >1 replica never double-fires. ``run_worker`` boots
settings + logging, installs SIGTERM/SIGINT handlers for a clean shutdown, and runs the scheduler
loop until signalled.
"""

from app.worker.scheduler import run_worker

if __name__ == "__main__":  # pragma: no cover
    run_worker()
