"""Shared fixtures for the backend test suite.

These tests mount the REAL FastAPI app (built via ``app.main:create_app``) so routing, middleware
and the error-envelope handlers are all exercised — but persistence is redirected to a throwaway
per-test SQLite DB via a ``get_db`` dependency override, so the real ``backend/data`` is never
touched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient


@pytest.fixture
def session_factory(tmp_path):
    """A sessionmaker bound to a throwaway SQLite file with all ORM tables created."""
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


@pytest.fixture
def seed_user(db):
    """Insert a ``User`` row (default username ``"alice.tan"``, password ``"correct horse"``) and
    return it. Call with kwargs to override any field, e.g. ``seed_user(role="admin")``."""
    from app.auth.security import hash_password
    from app.models import User

    def _make(*, username="alice.tan", password="correct horse", **kwargs) -> User:
        user = User(
            username=username,
            display_name=kwargs.pop("display_name", username),
            password_hash=hash_password(password),
            **kwargs,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return _make


@pytest.fixture
def login(client):
    """POST /api/auth/login for ``username``/``password`` and return the raw bearer token."""

    def _login(username="alice.tan", password="correct horse") -> str:
        resp = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    return _login


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """The login-failure throttle (``routes_auth``) is a module-global sliding window by design —
    it must survive across requests within one process. Reset it around EVERY test so failures
    recorded by one test (wrong-password cases, mostly) can never trip another test's 429."""
    from app.api import routes_auth

    routes_auth.reset_throttle()
    yield
    routes_auth.reset_throttle()


@pytest.fixture
def auth_headers(login):
    """``{"Authorization": "Bearer <token>"}`` for a freshly logged-in default ``seed_user``.

    Depends on ``login`` (not ``seed_user``) so tests that need a *specific* seeded user should
    call ``login(username=..., password=...)`` directly instead of this fixture.
    """
    return {"Authorization": f"Bearer {login()}"}
