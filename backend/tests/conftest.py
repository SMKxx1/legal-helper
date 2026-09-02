"""Shared fixtures for the backend test suite.

These tests mount the REAL FastAPI app (built via ``app.main:create_app``) so routing, middleware
(CSRF), dependency wiring and the error-envelope handlers are all exercised — but persistence is
redirected to a throwaway per-test SQLite DB via a ``get_db`` dependency override, so the real
``backend/data`` is never touched. The app's lifespan (which would seed the real DB) is deliberately
NOT run: ``TestClient`` is constructed without the context-manager form, and each test seeds exactly
the rows it needs through the override session.

Pure-logic tests import the module under test directly and need none of this.

Ported from ``nda-review-cloud/backend/tests/conftest.py``. The ONLY adaptation is app construction:
the source imported a module-level ``app`` singleton (``from app.main import app``); this engine
exposes a ``create_app(settings)`` factory instead, so the ``client`` fixture builds a fresh app from
the process-wide ``settings`` singleton (the same object tests monkeypatch via
``app.config.settings``). The retired SIGNED-principal plane's signed-header / nonce / allowed-account
fixtures never lived here (they were inline in the now-retired test files), so nothing was stripped.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

#: A fixed, sufficiently-strong password reused across seeded test accounts.
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def _tmp_env(tmp_path, monkeypatch):
    """Point file storage at a temp dir and provide a dummy provider key, before any
    app module that reads them is touched."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return tmp_path


@pytest.fixture
def session_factory(tmp_path):
    """A sessionmaker bound to a throwaway SQLite file with all ORM tables created.

    Adaptation note: the source bound this off ``_tmp_env`` (which also exports ``DATA_DIR`` /
    ``ANTHROPIC_API_KEY`` into the process env). Because this fixture is pulled in by the autouse
    ``_isolate_engine_writes`` safety net, that made those env vars leak into EVERY test — including
    the wave-1 ``test_config`` cases that assert ``Settings(_env_file=None).data_dir == "./data"`` on a
    clean env. Binding straight to ``tmp_path`` keeps the DB safety net universal without polluting the
    global env; tests that need the storage/provider-key redirection request ``_tmp_env`` (the
    ``client`` fixture does)."""
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


@pytest.fixture(autouse=True)
def _isolate_engine_writes(session_factory, monkeypatch):
    """Default safety net: the engine persists via reviews_repo's OWN module-global ``SessionLocal``
    (bound to ``settings.database_url`` at import — NOT the request's ``get_db`` override), so without
    this an engine-write test would hit the real ``backend/data/app.db``. Point it at the throwaway
    per-test DB for EVERY test so persistence can never leak to the real DB."""
    from app.api import reviews_repo

    monkeypatch.setattr(reviews_repo, "SessionLocal", session_factory)


@pytest.fixture
def db(session_factory):
    """A session for seeding/asserting directly against the throwaway DB."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(session_factory, _tmp_env):
    """The REAL FastAPI app for a test, built via ``create_app`` with ``get_db`` pointed at the
    throwaway DB.

    Built from the process-wide ``settings`` singleton (so tests that
    ``monkeypatch.setattr(app.config.settings, ...)`` are reflected). Depends on ``_tmp_env`` so file
    storage + the dummy provider key are redirected for app-facing tests (the source got this via
    ``session_factory``).

    Adaptation note: the source engine exposed a module-level ``app`` singleton, so ported tests that
    inject a dependency override or mount a probe route do ``from app.main import app`` and mutate
    ``app.dependency_overrides`` / ``app.router.routes`` on the SAME object the ``client`` serves. This
    engine has only a ``create_app`` factory, so that import no longer exists — request THIS ``app``
    fixture instead (the ``client`` fixture below serves this exact object, so overrides take effect).
    """
    from app.api import routes_auth
    from app.auth import sessions
    from app.config import settings
    from app.db import get_db
    from app.main import create_app

    sessions._cache.clear()
    # The per-IP login/reset-request throttles (routes_auth.py) are process-global in-process
    # state (by design — see auth_ip_throttle_enabled), and every TestClient call from this fixture
    # shares the same "IP" (TestClient's fixed synthetic host). Without clearing them, failed-login
    # counts from an earlier test would carry over and could trip a 429 in a later, unrelated test.
    routes_auth._login_ip_throttle.reset()
    routes_auth._reset_ip_throttle.reset()

    def _override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application = create_app(settings)
    application.dependency_overrides[get_db] = _override_db
    try:
        yield application
    finally:
        application.dependency_overrides.clear()
        sessions._cache.clear()
        routes_auth._login_ip_throttle.reset()
        routes_auth._reset_ip_throttle.reset()


@pytest.fixture
def client(app):
    """A TestClient on the real app (see the ``app`` fixture), with get_db pointed at the throwaway DB.

    Constructed without ``with`` so the lifespan (real-DB seeding) never runs.
    """
    yield TestClient(app)


@pytest.fixture
def seed_user(db):
    """Factory: insert a UserAccount (creating its Org on first use) and return it."""
    from app.auth.models import Org, UserAccount
    from app.auth.security import hash_password
    from app.schemas import DEFAULT_ORG_ID

    def _seed(
        user_id="alice",
        *,
        role="reviewer",
        password=PASSWORD,
        org_id=DEFAULT_ORG_ID,
        status="active",
        must_change_password=False,
        team=None,
        can_view_all_docs=False,
        can_view_all_spend=False,
        can_manage_permissions=False,
    ):
        if db.get(Org, org_id) is None:
            db.add(Org(id=org_id, name=f"Org {org_id}"))
            db.flush()
        user = UserAccount(
            org_id=org_id,
            user_id=user_id,
            password_hash=hash_password(password),
            role=role,
            status=status,
            must_change_password=must_change_password,
            team=team,
            can_view_all_docs=can_view_all_docs,
            can_view_all_spend=can_view_all_spend,
            can_manage_permissions=can_manage_permissions,
        )
        db.add(user)
        db.commit()
        return user

    return _seed


@pytest.fixture
def login(client):
    """Factory: log a (already-seeded) user in. Stores the session cookies on the
    client and propagates the CSRF token as a default header so subsequent
    state-changing requests pass the double-submit check. Returns the response."""

    def _login(user_id="alice", password=PASSWORD):
        resp = client.post(
            "/api/auth/login", json={"user_id": user_id, "password": password}
        )
        csrf = client.cookies.get("csrf")
        if csrf:
            client.headers.update({"x-csrf-token": csrf})
        return resp

    return _login
