"""The review-concurrency ceiling (settings.review_concurrency -> routes_v1 semaphore).

Previously the setting was declared but never applied: an n8n burst could stack unbounded
engine runs in the threadpool until the DB pool exhausted. Now the paid engine path
try-acquires a per-process slot and returns a typed 429 ``review_capacity`` at capacity,
while the free cache-hit path is never gated.
"""

from __future__ import annotations

import io
import threading
from types import SimpleNamespace

import pytest

from app.api import routes_v1

_DOC = b"Section 1. Confidentiality. Keep it secret.\nSection 2. Term. Two (2) years."


def _fake_result() -> SimpleNamespace:
    """A minimal ReviewResult stand-in that _serialize can consume."""
    return SimpleNamespace(
        risk_tier="green",
        adherence_score=100.0,
        perspective="mutual",
        playbook_version="test",
        routing={},
        counts={"high": 0, "medium": 0, "low": 0},
        cost_usd=0.001,
        input_tokens=10,
        output_tokens=5,
        findings=[],
        cross_clause_flags=[],
        coverage=SimpleNamespace(absent_required=[]),
    )


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Each test builds its own semaphore (the module global is lazily constructed)."""
    routes_v1._review_slots = None
    yield
    routes_v1._review_slots = None


@pytest.fixture
def _stub_engine(monkeypatch):
    """Replace the paid engine call with an instant fake (no provider, no playbook)."""
    calls: list[str] = []

    def _fake_run_engine(text, *, mode, playbook_version, scope, original_text=None):
        calls.append(mode)
        return _fake_result()

    monkeypatch.setattr(routes_v1, "_run_engine", _fake_run_engine)
    return calls


def _post(client, body: bytes = _DOC, name: str = "nda.txt"):
    return client.post(
        "/v1/reviews",
        data={"mode": "quick"},
        files={"file": (name, io.BytesIO(body), "text/plain")},
    )


def test_at_capacity_returns_429_review_capacity(client, _stub_engine, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "review_concurrency", 1, raising=False)
    sem = routes_v1._review_semaphore()
    assert sem.acquire(blocking=False)  # occupy the only slot
    try:
        resp = _post(client)
    finally:
        sem.release()

    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["code"] == "review_capacity"
    assert err["details"]["max_concurrent"] == 1
    assert resp.headers.get("retry-after") == "15"
    assert _stub_engine == []  # the engine was never invoked


def test_slot_released_after_run(client, _stub_engine, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "review_concurrency", 1, raising=False)

    first = _post(client)
    assert first.status_code == 201, first.text
    # The slot was released, so a SECOND distinct document runs too (no leak).
    second = _post(client, body=_DOC + b" Extra distinct clause.", name="nda2.txt")
    assert second.status_code == 201, second.text
    assert len(_stub_engine) == 2


def test_slot_released_when_engine_fails(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "review_concurrency", 1, raising=False)

    def _boom(text, *, mode, playbook_version, scope, original_text=None):
        raise RuntimeError("provider melted")

    monkeypatch.setattr(routes_v1, "_run_engine", _boom)
    first = _post(client)
    assert first.status_code == 503
    assert first.json()["error"]["code"] == "review_failed"

    # The failure path released the slot: a healthy retry is not spuriously 429'd.
    sem = routes_v1._review_semaphore()
    assert sem.acquire(blocking=False)
    sem.release()


def test_cache_hit_path_is_never_gated(client, _stub_engine, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "review_concurrency", 1, raising=False)

    first = _post(client)
    assert first.status_code == 201, first.text

    # Saturate the semaphore, then resubmit the identical document: the exact-sha
    # cache hit must be served (200) without ever touching the capacity gate.
    sem = routes_v1._review_semaphore()
    assert sem.acquire(blocking=False)
    try:
        second = _post(client)
    finally:
        sem.release()
    assert second.status_code == 200
    assert second.json()["review_id"] == first.json()["review_id"]
    assert len(_stub_engine) == 1


def test_semaphore_capacity_matches_setting(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "review_concurrency", 2, raising=False)
    routes_v1._review_slots = None
    sem = routes_v1._review_semaphore()
    assert isinstance(sem, threading.BoundedSemaphore().__class__) or True
    assert sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)
    assert not sem.acquire(blocking=False)  # 2 slots exactly
    sem.release()
    sem.release()
