"""``seed_demo.py`` — demo history seeded onto an account that already exists.

No accounts are seeded any more (everyone registers themselves with their own OpenRouter key), so
the tests here cover the two things that can still go wrong: seeding twice must not duplicate
rows, and seeding at a username nobody has registered must do nothing rather than half-create
something.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from sqlalchemy import func, select

from app import seed_demo
from app.auth.security import hash_password
from app.models import LlmCall, Review, User

_NOW = datetime(2026, 9, 2, tzinfo=UTC)


def _make_account(db, username: str = "jane.tan", role: str = "user") -> User:
    user = User(
        username=username,
        display_name=username,
        role=role,
        password_hash=hash_password("a-password-long-enough"),
    )
    db.add(user)
    db.commit()
    return user


def _counts(db) -> tuple[int, int, int]:
    return (
        db.execute(select(func.count(User.id))).scalar(),
        db.execute(select(func.count(Review.id))).scalar(),
        db.execute(select(func.count(LlmCall.id))).scalar(),
    )


def test_seeding_twice_creates_nothing_the_second_time(db):
    _make_account(db)

    first = seed_demo.seed_reviews(db, random.Random(2026), "jane.tan", now_utc=_NOW)
    assert first == seed_demo._REVIEW_COUNT
    after_first = _counts(db)
    assert after_first[1] == seed_demo._REVIEW_COUNT
    assert after_first[2] > 0

    # a second `make seed`, same RNG seed, exactly as a user would re-run it
    second = seed_demo.seed_reviews(db, random.Random(2026), "jane.tan", now_utc=_NOW)
    assert second == 0
    assert _counts(db) == after_first


def test_seeding_an_unknown_account_is_a_no_op(db):
    """No account, no rows — never a half-seeded database."""
    assert seed_demo.seed_reviews(db, random.Random(2026), "nobody", now_utc=_NOW) == 0
    assert _counts(db) == (0, 0, 0)


def test_seeded_history_belongs_to_the_named_account(db):
    _make_account(db, "jane.tan")
    _make_account(db, "other.person")

    seed_demo.seed_reviews(db, random.Random(2026), "jane.tan", now_utc=_NOW)

    jane = db.execute(select(User).where(User.username == "jane.tan")).scalar_one()
    owners = {r[0] for r in db.execute(select(Review.user_id)).all()}
    assert owners == {jane.id}


def test_promote_grants_admin_and_reports_a_missing_account(db):
    _make_account(db, "jane.tan")
    assert seed_demo.promote_to_admin(db, "jane.tan") is True
    assert (
        db.execute(select(User).where(User.username == "jane.tan")).scalar_one().role
        == "admin"
    )
    assert seed_demo.promote_to_admin(db, "nobody") is False


def test_prune_removes_non_admin_accounts_and_their_history(db):
    _make_account(db, "keeper", role="admin")
    _make_account(db, "jane.tan")
    seed_demo.seed_reviews(db, random.Random(2026), "jane.tan", now_utc=_NOW)

    users, reviews = seed_demo.prune_non_admin_users(db)
    assert users == 1
    assert reviews == seed_demo._REVIEW_COUNT
    assert _counts(db) == (1, 0, 0)  # only the admin survives, with nothing orphaned
