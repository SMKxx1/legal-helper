"""Async review jobs (3.1 — the CONTRACT-pre-authorized addition): submit -> claim -> poll.

Sync /v1/reviews holds the connection for the full engine wall-clock; ?async=1 returns
202 + job_id after all gates pass, the worker's claimer runs the job under a visibility-timeout
lease (attempts-capped dead-letter), and the caller polls GET /v1/reviews/jobs/{id}. The backend
never calls n8n outbound — completion is pull, not push.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api import reviews_repo, routes_v1
from app.worker.scheduler import process_one_review_job

_DOC = b"Section 1. Confidentiality. Keep it secret.\nSection 2. Term. Two (2) years."


def _fake_result() -> SimpleNamespace:
    return SimpleNamespace(
        risk_tier="green",
        adherence_score=100.0,
        perspective="mutual",
        playbook_version="test",
        routing={},
        counts={},
        cost_usd=0.002,
        input_tokens=10,
        output_tokens=5,
        findings=[],
        cross_clause_flags=[],
        coverage=SimpleNamespace(absent_required=[]),
    )


@pytest.fixture(autouse=True)
def _reset_semaphore():
    routes_v1._review_slots = None
    yield
    routes_v1._review_slots = None


def _submit(client, body: bytes = _DOC, name: str = "nda.txt"):
    return client.post(
        "/v1/reviews?async=1",
        data={"mode": "quick"},
        files={"file": (name, io.BytesIO(body), "text/plain")},
    )


def test_async_submit_returns_202_with_pending_job(client):
    resp = _submit(client)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    job_id = body["job_id"]
    assert resp.headers["location"] == f"/v1/reviews/jobs/{job_id}"

    poll = client.get(f"/v1/reviews/jobs/{job_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending"
    assert poll.json()["review_id"] is None


def test_worker_completes_job_and_poll_returns_the_review(client, monkeypatch):
    monkeypatch.setattr(routes_v1, "_run_engine", lambda *a, **k: _fake_result())
    job_id = _submit(client).json()["job_id"]

    handled = process_one_review_job()
    assert handled == job_id

    poll = client.get(f"/v1/reviews/jobs/{job_id}").json()
    assert poll["status"] == "done"
    assert poll["review_id"]
    assert poll["review"]["risk_tier"] == "green"  # payload inlined — no second call

    # The persisted review now serves the CONTENT cache tiers: a sync resubmit of the
    # same document is a 200 cache hit, not a fresh engine run.
    resub = client.post(
        "/v1/reviews",
        data={"mode": "quick"},
        files={"file": ("nda.txt", io.BytesIO(_DOC), "text/plain")},
    )
    assert resub.status_code == 200
    assert resub.json()["review_id"] == poll["review_id"]


def test_failed_run_requeues_with_backoff_then_dead_letters(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider melted: secret-internal-detail")

    monkeypatch.setattr(routes_v1, "_run_engine", _boom)
    job_id = _submit(client).json()["job_id"]

    # Attempts 1..MAX all fail; each failure before the cap re-queues to 'pending' with a
    # retry DEFERRAL (backoff), so an immediate re-tick must NOT claim it again.
    now = datetime.now(UTC)
    for attempt in range(1, reviews_repo.REVIEW_JOB_MAX_ATTEMPTS + 1):
        assert process_one_review_job(now=now) == job_id
        status = client.get(f"/v1/reviews/jobs/{job_id}").json()
        expected = (
            "pending" if attempt < reviews_repo.REVIEW_JOB_MAX_ATTEMPTS else "failed"
        )
        assert status["status"] == expected, f"attempt {attempt}"
        # The poll surface gets a SANITIZED error (exception class only) — raw internals
        # stay in the server logs, mirroring the sync path's masked envelope.
        assert "RuntimeError" in status["error"]
        assert "secret-internal-detail" not in status["error"]
        if expected == "pending":
            # Backoff: not claimable right now...
            assert process_one_review_job(now=now) is None
            # ...only after the deferral passes.
            now = now + timedelta(
                seconds=reviews_repo.REVIEW_JOB_RETRY_BACKOFF_S * attempt + 1
            )

    # Dead-lettered: the claimer never picks it up again (even far in the future).
    assert process_one_review_job(now=now + timedelta(hours=1)) is None


def test_expired_lease_is_reclaimed(client, monkeypatch):
    """Crash-safety: a worker that died mid-run leaves status='running' with a stale lease —
    the next claim treats it as runnable (visibility timeout), so no review is ever lost."""
    monkeypatch.setattr(routes_v1, "_run_engine", lambda *a, **k: _fake_result())
    job_id = _submit(client).json()["job_id"]

    # Simulate the crashed worker: claim it, then never complete.
    claimed = reviews_repo.claim_review_job()
    assert claimed and claimed["job_id"] == job_id
    assert client.get(f"/v1/reviews/jobs/{job_id}").json()["status"] == "running"

    # Within the lease the job is NOT claimable (a live run must not be duplicated).
    assert reviews_repo.claim_review_job() is None

    # After the lease expires it is — and the run completes normally.
    past_lease = datetime.now(UTC) + timedelta(
        seconds=reviews_repo.REVIEW_JOB_LEASE_S + 60
    )
    assert process_one_review_job(now=past_lease) == job_id
    assert client.get(f"/v1/reviews/jobs/{job_id}").json()["status"] == "done"


def test_claimer_skips_when_at_capacity(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "review_concurrency", 1, raising=False)
    monkeypatch.setattr(routes_v1, "_run_engine", lambda *a, **k: _fake_result())
    job_id = _submit(client).json()["job_id"]

    sem = routes_v1._review_semaphore()
    assert sem.acquire(blocking=False)  # someone else holds the only slot
    try:
        assert process_one_review_job() is None  # no claim burned
    finally:
        sem.release()
    # The job is untouched and still runnable.
    assert client.get(f"/v1/reviews/jobs/{job_id}").json()["status"] == "pending"
    assert process_one_review_job() == job_id


def test_job_poll_is_org_scoped(client):
    job_id = _submit(client).json()["job_id"]
    # A read from ANOTHER org must 404, not leak status (repo-level check: the HTTP layer
    # always passes the caller's org).
    assert reviews_repo.get_review_job(job_id, "other-org-id") is None
    assert reviews_repo.get_review_job(job_id) is not None


def test_unknown_job_is_404(client):
    resp = client.get("/v1/reviews/jobs/" + "a" * 32)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_inflight_resubmit_dedups_to_the_same_job(client, monkeypatch):
    """The in-flight window: a retried ?async=1 submit (same content, or same idempotency key)
    while job 1 is still pending/running must NOT enqueue a second paid run."""
    monkeypatch.setattr(routes_v1, "_run_engine", lambda *a, **k: _fake_result())

    first = _submit(client)
    job_id = first.json()["job_id"]

    # Same content again -> the SAME job comes back, no second row.
    again = _submit(client)
    assert again.status_code == 202
    assert again.json()["job_id"] == job_id

    # Same idempotency key with byte-DIFFERENT content -> still the same job (a distinct
    # document from job 1, so this exercises the KEY match, not the content match).
    keyed = client.post(
        "/v1/reviews?async=1",
        data={"mode": "quick"},
        files={"file": ("nda.txt", io.BytesIO(_DOC + b" keyed v1"), "text/plain")},
        headers={"X-Idempotency-Key": "flow-1"},
    )
    keyed_job = keyed.json()["job_id"]
    assert keyed_job != job_id
    keyed_retry = client.post(
        "/v1/reviews?async=1",
        data={"mode": "quick"},
        files={
            "file": ("nda.txt", io.BytesIO(_DOC + b" keyed v2 re-export"), "text/plain")
        },
        headers={"X-Idempotency-Key": "flow-1"},
    )
    assert keyed_retry.json()["job_id"] == keyed_job

    # Once the job completes, the dedup naturally moves to the content/idempotency tiers.
    while process_one_review_job() is not None:
        pass
    done = client.get(f"/v1/reviews/jobs/{job_id}").json()
    assert done["status"] == "done"


def test_stale_claim_is_fenced_off(client, monkeypatch):
    """A zombie run (lease expired, job re-claimed elsewhere) must not clobber the live
    claim's state — its complete/fail writes are fenced by the claim token."""
    monkeypatch.setattr(routes_v1, "_run_engine", lambda *a, **k: _fake_result())
    job_id = _submit(client).json()["job_id"]

    stale = reviews_repo.claim_review_job()  # worker A claims... then stalls
    past = datetime.now(UTC) + timedelta(seconds=reviews_repo.REVIEW_JOB_LEASE_S + 60)
    fresh = reviews_repo.claim_review_job(past)  # worker B re-claims after expiry
    assert fresh and fresh["job_id"] == job_id
    assert fresh["claim_token"] != stale["claim_token"]

    # The zombie wakes up and reports: both writes must be no-ops.
    assert (
        reviews_repo.complete_review_job(
            job_id, "bogus-review", claim_token=stale["claim_token"]
        )
        is False
    )
    reviews_repo.fail_review_job(
        job_id, "zombie failure", claim_token=stale["claim_token"]
    )
    status = client.get(f"/v1/reviews/jobs/{job_id}").json()
    assert status["status"] == "running"  # worker B's claim is untouched
    assert status["review_id"] is None

    # Worker B's own completion (current token) lands normally.
    assert (
        reviews_repo.complete_review_job(
            job_id, "real-review", claim_token=fresh["claim_token"]
        )
        is True
    )
    assert client.get(f"/v1/reviews/jobs/{job_id}").json()["status"] == "done"


def test_recovery_reuses_saved_review_without_rerunning_the_engine(client, monkeypatch):
    """Crash between save_review and complete_review_job: the retry must NOT re-run the paid
    engine — the persisted review for the same (content, mode) is reused."""
    calls: list[str] = []

    def _engine(*a, **k):
        calls.append("run")
        return _fake_result()

    monkeypatch.setattr(routes_v1, "_run_engine", _engine)
    job_id = _submit(client).json()["job_id"]

    # First attempt: engine + save succeed, but completion "crashes" (simulated by a
    # completion that raises once).
    real_complete = reviews_repo.complete_review_job

    def _crash(*a, **k):
        raise RuntimeError("db blip during completion")

    monkeypatch.setattr(reviews_repo, "complete_review_job", _crash)
    assert process_one_review_job() == job_id
    monkeypatch.setattr(reviews_repo, "complete_review_job", real_complete)
    assert len(calls) == 1
    assert client.get(f"/v1/reviews/jobs/{job_id}").json()["status"] == "pending"

    # Retry (after the backoff): recovery finds the saved review — no second engine run.
    later = datetime.now(UTC) + timedelta(
        seconds=reviews_repo.REVIEW_JOB_RETRY_BACKOFF_S * 2
    )
    assert process_one_review_job(now=later) == job_id
    assert len(calls) == 1  # the paid engine ran exactly once
    assert client.get(f"/v1/reviews/jobs/{job_id}").json()["status"] == "done"


def test_async_cache_hit_still_short_circuits_sync(client, monkeypatch):
    """A document already reviewed (cache tier hit) never creates a job — the 200 cached
    response wins before the async branch."""
    monkeypatch.setattr(routes_v1, "_run_engine", lambda *a, **k: _fake_result())
    sync = client.post(
        "/v1/reviews",
        data={"mode": "quick"},
        files={"file": ("nda.txt", io.BytesIO(_DOC), "text/plain")},
    )
    assert sync.status_code == 201

    resub = _submit(client)  # async=1, but identical content
    assert resub.status_code == 200  # cache hit, no job created
    assert resub.json()["review_id"] == sync.json()["review_id"]
