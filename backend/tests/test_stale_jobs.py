"""Boot-time crash recovery: an orphaned review must never be left polling forever.

Regression test for a review that hung in production. A deep review started three minutes before
a deploy; the restart killed the background task that owned it, and the boot sweep skipped it
because it was younger than the 15-minute threshold. It sat at ``queued`` indefinitely while the
task pane polled it — the user just saw a review that never finished.

At boot there is no such thing as a review this process is still working on, so age is the wrong
question to ask.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.api import reviews_repo
from app.models import Review


def _queued_review(db, user, *, minutes_ago: float, status: str = "queued") -> Review:
    review = Review(
        user_id=user.id,
        filename="document.docx",
        mode="deep",
        status=status,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def test_a_review_interrupted_seconds_ago_is_failed_at_boot(db, seed_user):
    """The exact case that hung: young, queued, and orphaned by a restart."""
    review = _queued_review(db, seed_user(), minutes_ago=0.05)

    assert reviews_repo.fail_stale_jobs(db) == 1

    db.refresh(review)
    assert review.status == "failed"
    assert review.error == "service_restarted"
    assert review.finished_at is not None


def test_running_rows_are_swept_too(db, seed_user):
    review = _queued_review(db, seed_user(), minutes_ago=1, status="running")
    assert reviews_repo.fail_stale_jobs(db) == 1
    db.refresh(review)
    assert review.status == "failed"


def test_finished_reviews_are_left_alone(db, seed_user):
    user = seed_user()
    done = _queued_review(db, user, minutes_ago=30, status="done")
    failed = _queued_review(db, user, minutes_ago=30, status="failed")

    reviews_repo.fail_stale_jobs(db)

    db.refresh(done)
    db.refresh(failed)
    assert done.status == "done"
    assert failed.status == "failed"


def test_an_age_threshold_still_works_for_a_periodic_sweep(db, seed_user):
    """Kept for sweeping a LIVE process, where a young row may genuinely still be running."""
    user = seed_user()
    young = _queued_review(db, user, minutes_ago=2)
    old = _queued_review(db, user, minutes_ago=30)

    assert reviews_repo.fail_stale_jobs(db, older_than_minutes=15) == 1

    db.refresh(young)
    db.refresh(old)
    assert young.status == "queued", (
        "a live process must not fail a review still in flight"
    )
    assert old.status == "failed"


def test_boot_sweeps_everything_orphaned(
    client, db, seed_user, session_factory, monkeypatch
):
    """End to end through the real lifespan: nothing is left queued after a boot.

    The lifespan opens its own ``SessionLocal`` (the request-scoped ``get_db`` override doesn't
    reach it), so point that at the throwaway DB for the duration of this boot.
    """
    from app import main as main_module

    monkeypatch.setattr(main_module, "SessionLocal", session_factory)

    user = seed_user()
    _queued_review(db, user, minutes_ago=0.1)
    _queued_review(db, user, minutes_ago=45, status="running")

    with client:  # entering the TestClient context runs the lifespan, i.e. a real boot
        pass

    db.expire_all()  # the sweep committed on a different session
    stuck = (
        db.execute(select(Review).where(Review.status.in_(("queued", "running"))))
        .scalars()
        .all()
    )
    assert stuck == [], "a review was left polling forever after a restart"
