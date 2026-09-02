"""Shared fixtures for the P2 bot-core tests (foundation agent).

``tests/conftest.py`` is frozen (never edited); bot-specific fixtures live here and are imported by
the bot test modules (``from conftest_bot import bot_session_factory`` — pytest's prepend import mode
puts ``tests/`` on ``sys.path``). This provides a throwaway
per-test SQLite session factory with the FULL ORM schema created — importing ``app.models`` registers
the bot tables (``app.bot.models``) on ``Base.metadata`` — so model round-trips and the unique-insert
dedup semantics run with no network and no shared state.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def bot_session_factory(tmp_path):
    """A sessionmaker bound to a throwaway SQLite file with every ORM table created."""
    import app.auth.models  # noqa: F401 - register identity/org tables on Base.metadata
    import app.models  # noqa: F401 - register core + bot tables on Base.metadata
    from app.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'bot.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()
