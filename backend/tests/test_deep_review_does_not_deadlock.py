"""A deep review must not be able to freeze the service.

Regression test for the outage that took the whole app down twice — no `/healthz`, no landing
page, nothing — until the container was restarted.

The deep branch flushed an UPDATE on the review row (taking a row lock) and returned 202 without
committing. The background task then ran ON the event loop and its first act was another UPDATE on
that same row, so it waited on the lock. The lock is released when the request's session is
cleaned up, and that cleanup runs on the event loop — which was blocked inside the UPDATE. A
deadlock the process could never leave, on every deep review.

Two invariants keep it dead: the request commits before spawning the task, and the task does all
of its blocking work on a worker thread.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest

from app.api import routes_reviews


def test_the_request_commits_before_spawning_the_task():
    """An uncommitted flush leaves a row lock the background task then waits on."""
    src = " ".join(inspect.getsource(routes_reviews.create_review).split())
    spawn = src.index("asyncio.create_task")
    store = src.index("_store_document")
    between = src[store:spawn]
    assert "db.commit()" in between, (
        "the review row is handed to the background task while this session still holds its lock"
    )


def test_the_background_task_does_its_blocking_work_off_the_loop():
    """`_run_deep_review` must delegate, not do DB or model work inline."""
    src = " ".join(inspect.getsource(routes_reviews._run_deep_review).split())
    assert (
        "to_thread( _deep_review_blocking" in src
        or "to_thread(_deep_review_blocking" in src
    )
    for blocking in ("SessionLocal(", "db.commit(", "run_review("):
        assert blocking not in src, (
            f"{blocking} runs on the event loop in _run_deep_review"
        )


def test_the_blocking_worker_is_synchronous():
    """It must be a plain function — an async one would run on the loop again."""
    assert not inspect.iscoroutinefunction(routes_reviews._deep_review_blocking)
    assert inspect.iscoroutinefunction(routes_reviews._run_deep_review)


@pytest.mark.asyncio
async def test_a_stuck_deep_review_leaves_the_loop_responsive(monkeypatch):
    """The property that actually matters: a wedged review must not wedge everyone else."""
    stuck = threading.Event()
    entered = threading.Event()

    def never_finishes(*args, **kwargs):
        entered.set()
        stuck.wait(timeout=5)

    monkeypatch.setattr(routes_reviews, "_deep_review_blocking", never_finishes)

    class _Slot:
        async def release(self):
            return None

    monkeypatch.setattr(routes_reviews, "_slots", _Slot())

    task = asyncio.create_task(
        routes_reviews._run_deep_review("rid", "text", "us", "key", None, [])
    )
    await asyncio.to_thread(entered.wait, 2)
    assert entered.is_set(), "the deep review never started"

    ticks = 0
    started = time.perf_counter()
    while time.perf_counter() - started < 0.2:
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks > 5, "the event loop was blocked by a stuck deep review"

    stuck.set()
    await task
