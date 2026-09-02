"""Flow-step idempotency keys (2.1 hardening): generate-nda + /v1/reviews + the retention sweep.

n8n retries HTTP calls on timeout and ``svc:n8n`` is one shared principal, so dedup is scoped
(principal, purpose, key). A replayed generate-nda returns the FIRST .docx byte-for-byte; a
replayed review returns the first stored result annotated as a cache hit — even when the retry
uploads byte-different content. Rows expire via the worker's ``idempotency_sweep``.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from docx import Document

from app.api import routes_v1

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(text: str = "Party: {{party}}.") -> bytes:
    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _generate(client, key: str | None, *, values='{"party": "Acme"}', body=None):
    data = {"values": values}
    if key:
        data["idempotency_key"] = key
    return client.post(
        "/v1/support_task/generate-nda",
        data=data,
        files={"file": ("nda.docx", io.BytesIO(body or _docx_bytes()), _DOCX_MIME)},
    )


def test_generate_nda_replays_the_first_result(client):
    first = _generate(client, "flow-step-1")
    assert first.status_code == 200, first.text
    assert "x-idempotency-replayed" not in first.headers

    # The replay carries DIFFERENT values — and must still return the FIRST bytes (the caller
    # retried; the first result is the truth).
    replay = _generate(client, "flow-step-1", values='{"party": "Changed Corp"}')
    assert replay.status_code == 200
    assert replay.headers.get("x-idempotency-replayed") == "true"
    assert replay.content == first.content

    # A different key generates fresh.
    fresh = _generate(client, "flow-step-2", values='{"party": "Changed Corp"}')
    assert "x-idempotency-replayed" not in fresh.headers
    assert fresh.content != first.content


def test_generate_nda_header_key_also_works(client):
    first = client.post(
        "/v1/support_task/generate-nda",
        data={"values": "{}"},
        files={"file": ("nda.docx", io.BytesIO(_docx_bytes()), _DOCX_MIME)},
        headers={"X-Idempotency-Key": "hdr-key-1"},
    )
    assert first.status_code == 200
    replay = client.post(
        "/v1/support_task/generate-nda",
        data={"values": "{}"},
        files={"file": ("nda.docx", io.BytesIO(_docx_bytes()), _DOCX_MIME)},
        headers={"X-Idempotency-Key": "hdr-key-1"},
    )
    assert replay.headers.get("x-idempotency-replayed") == "true"


def test_generate_nda_without_key_never_dedups(client):
    a = _generate(client, None)
    b = _generate(client, None)
    assert a.status_code == b.status_code == 200
    assert "x-idempotency-replayed" not in b.headers


def _stub_engine(monkeypatch):
    result = SimpleNamespace(
        risk_tier="green",
        adherence_score=100.0,
        perspective="mutual",
        playbook_version="test",
        routing={},
        counts={},
        cost_usd=0.001,
        input_tokens=1,
        output_tokens=1,
        findings=[],
        cross_clause_flags=[],
        coverage=SimpleNamespace(absent_required=[]),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        routes_v1, "_run_engine", lambda *a, **k: calls.append("run") or result
    )
    return calls


def _review(client, body: bytes, key: str | None = None, force: bool = False):
    headers = {"X-Idempotency-Key": key} if key else {}
    data = {"mode": "quick"}
    if force:
        data["force"] = "true"
    return client.post(
        "/v1/reviews",
        data=data,
        files={"file": ("nda.txt", io.BytesIO(body), "text/plain")},
        headers=headers,
    )


def test_review_idempotency_key_replays_even_for_different_bytes(client, monkeypatch):
    calls = _stub_engine(monkeypatch)

    first = _review(client, b"doc v1 text", key="step-abc")
    assert first.status_code == 201, first.text
    rid = first.json()["review_id"]

    # Retry with byte-DIFFERENT content (re-exported doc): the content-sha tiers would miss,
    # but the explicit key replays the first result as a cache hit.
    replay = _review(client, b"doc v1 text re-exported differently", key="step-abc")
    assert replay.status_code == 200
    body = replay.json()
    assert body["cache"]["tier"] == "idempotency_key"
    assert body["cache"]["matched_review_id"] == rid
    assert len(calls) == 1  # the engine ran exactly once


def test_review_force_bypasses_the_idempotency_key(client, monkeypatch):
    calls = _stub_engine(monkeypatch)
    assert _review(client, b"some doc", key="step-x").status_code == 201
    forced = _review(client, b"another doc", key="step-x", force=True)
    assert forced.status_code == 201  # fresh run despite the seen key
    assert len(calls) == 2


def test_idempotency_sweep_removes_only_expired_rows(db):
    from app.models_bot import NdaIdempotencyKey
    from app.support_task.bot_dal import IDEMPOTENCY_RETENTION_H
    from app.worker.scheduler import idempotency_sweep

    now = datetime.now(UTC)
    old = NdaIdempotencyKey(
        principal_id="svc:n8n",
        purpose="review",
        key="old",
        created_at=now - timedelta(hours=IDEMPOTENCY_RETENTION_H + 1),
    )
    fresh = NdaIdempotencyKey(
        principal_id="svc:n8n", purpose="review", key="fresh", created_at=now
    )
    db.add_all([old, fresh])
    db.commit()

    # Run the sweep against the same throwaway DB via a session factory on its engine.
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    removed = idempotency_sweep(now=now, session_factory=factory)
    assert removed == 1

    remaining = db.query(NdaIdempotencyKey).all()
    assert [r.key for r in remaining] == ["fresh"]
