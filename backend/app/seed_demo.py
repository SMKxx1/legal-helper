"""Synthetic demo users (plan §4.6). Reviews + ``llm_calls`` land in Phase 3.

Run with ``python -m app.seed_demo`` (also runs at boot when ``SEED_DEMO_DATA=true`` **and** the
``users`` table is empty — wired in Phase 3, which is also where usage history is seeded).
Idempotent: an existing username is left untouched, so running this twice creates nothing new.

Every seeded user shares one password, ``DEMO_USER_PASSWORD`` — and carries no OpenRouter key.
Only the presenter's own account gets a real key, entered live in the add-in.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .auth.security import hash_password
from .config import settings
from .db import SessionLocal, init_db
from .models import User

#: (username, display_name, role) — fixed order/spelling so every deployment looks the same.
DEMO_USERS: list[tuple[str, str, str]] = [
    ("admin", "Admin", "admin"),
    ("alice.tan", "Alice Tan", "user"),
    ("ben.lim", "Ben Lim", "user"),
    ("chloe.ng", "Chloe Ng", "user"),
    ("dev.raj", "Dev Raj", "user"),
    ("emma.koh", "Emma Koh", "user"),
    ("farid.hassan", "Farid Hassan", "user"),
    ("grace.lee", "Grace Lee", "user"),
]


def seed_users(db: DbSession) -> int:
    """Insert any :data:`DEMO_USERS` row not already present. Returns the number created."""
    password_hash = hash_password(settings.demo_user_password)
    existing = {row[0] for row in db.execute(select(User.username)).all()}
    created = 0
    for username, display_name, role in DEMO_USERS:
        if username in existing:
            continue
        db.add(
            User(
                username=username,
                display_name=display_name,
                role=role,
                password_hash=password_hash,
            )
        )
        created += 1
    db.commit()
    return created


def run() -> None:
    init_db()
    with SessionLocal() as db:
        created = seed_users(db)
        print(
            f"seed_demo: {created} user(s) created, "
            f"{len(DEMO_USERS) - created} already present"
        )


if __name__ == "__main__":
    run()
