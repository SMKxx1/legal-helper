"""Shared fixtures for the backend test suite.

These tests mount the REAL FastAPI app (built via ``app.main:create_app``) so routing, middleware
and the error-envelope handlers are all exercised — but persistence is redirected to a throwaway
per-test SQLite DB via a ``get_db`` dependency override, so the real ``backend/data`` is never
touched.

Phase 0 note: this file is intentionally minimal. The predecessor engine's cookie+CSRF auth fixtures
(``seed_user``, ``login``) and its engine-write DB-isolation fixture depended on modules this rebuild
deleted (bearer-token auth replaces cookies — see plan §1). Phase 1 adds the real ``seed_user``/
``login`` fixtures against the new auth surface.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient


@pytest.fixture
def session_factory(tmp_path):
    """A sessionmaker bound to a throwaway SQLite file with all ORM tables created."""
    import app.auth.models  # noqa: F401 - register identity tables on Base
    import app.models  # noqa: F401 - register core tables on Base
    from app.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def db(session_factory):
    """A session for seeding/asserting directly against the throwaway DB."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(session_factory):
    """The REAL FastAPI app for a test, built via ``create_app`` with ``get_db`` pointed at the
    throwaway DB.

    Built from a fresh zero-env ``Settings`` object (the app must boot with nothing configured).
    """
    from app.config import Settings
    from app.db import get_db
    from app.main import create_app

    def _override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application = create_app(Settings(_env_file=None))
    application.dependency_overrides[get_db] = _override_db
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app):
    """A TestClient on the real app (see the ``app`` fixture), with get_db pointed at the throwaway DB.

    Constructed without ``with`` so the lifespan (real-DB init) never runs.
    """
    yield TestClient(app)
