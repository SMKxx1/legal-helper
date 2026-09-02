"""``seed_demo.py`` idempotency (plan §4.6) — the one real correctness risk in seed data: running
the whole seed twice must never create duplicate users, reviews, or ``llm_calls`` rows.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from sqlalchemy import func, select

from app import seed_demo
from app.models import LlmCall, Review, User


def test_seed_is_idempotent(db):
    now = datetime(2026, 9, 2, tzinfo=UTC)

    created_users_1 = seed_demo.seed_users(db)
    created_reviews_1 = seed_demo.seed_reviews(db, random.Random(2026), now_utc=now)
    assert created_users_1 == len(seed_demo.DEMO_USERS)
    assert created_reviews_1 == seed_demo._REVIEW_COUNT

    def _counts() -> tuple[int, int, int]:
        return (
            db.execute(select(func.count(User.id))).scalar(),
            db.execute(select(func.count(Review.id))).scalar(),
            db.execute(select(func.count(LlmCall.id))).scalar(),
        )

    counts_after_first_run = _counts()
    assert counts_after_first_run[0] == len(seed_demo.DEMO_USERS)
    assert counts_after_first_run[1] == seed_demo._REVIEW_COUNT
    assert counts_after_first_run[2] > 0

    # Run it all again — a fresh RNG with the SAME seed, exactly like a second `make seed`.
    created_users_2 = seed_demo.seed_users(db)
    created_reviews_2 = seed_demo.seed_reviews(db, random.Random(2026), now_utc=now)

    assert created_users_2 == 0
    assert created_reviews_2 == 0
    assert _counts() == counts_after_first_run
