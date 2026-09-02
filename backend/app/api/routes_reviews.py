"""``POST /api/reviews`` and friends (plan §4.2) — the core of the product.

Pre-flight checks run, IN ORDER, before any LLM spend: has an OpenRouter key (409), under the
monthly budget (402), a free review slot (429), the upload parses to a real document (422), and
it isn't too large (413). Quick mode then runs SYNCHRONOUSLY (the request holds a slot until the
result is ready — Quick is a single fast pass, seconds not minutes). Deep mode answers `202` and
runs as an ``asyncio`` background task under the same slot, polled via ``GET /api/reviews/{id}``.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session as DbSession

from .. import crypto
from ..agents.orchestrator import ModelChoice, run_review
from ..ai.gateway import ProviderError, error_code_for
from ..auth.deps import get_current_user
from ..config import settings
from ..db import SessionLocal, get_db
from ..ingestion.docx import extract_docx_bytes
from ..models import User
from ..storage import bucket
from ..telemetry import get_logger
from . import reviews_repo
from .errors import EngineError

log = get_logger("legal_helper.api.reviews")

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


class _ReviewSlots:
    """A tiny counting semaphore with a non-blocking ``try_acquire`` (``asyncio.Semaphore`` has no
    such thing) — the ``REVIEW_CONCURRENCY`` pre-flight check (plan §4.2: 429 ``review_capacity``
    when no slot is free, never a queued wait)."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._count = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._count >= self._capacity:
                return False
            self._count += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._count = max(0, self._count - 1)


_slots = _ReviewSlots(settings.review_concurrency)


def _models_for(user: User) -> ModelChoice:
    return ModelChoice(
        classifier=settings.model_classifier,
        quick=user.preferred_model_quick or settings.model_quick,
        deep=user.preferred_model_deep or settings.model_deep,
    )


def _parse_upload(data: bytes) -> str:
    """Bytes -> full document text, or ``422 empty_document`` / ``413`` on the way. Never raises
    anything but :class:`EngineError`."""
    try:
        parsed = extract_docx_bytes(data)
    except ValueError as exc:
        raise EngineError(
            422, "empty_document", f"Could not read this document: {exc}"
        ) from exc
    text = parsed.full_text
    if len(text) < 200:
        raise EngineError(
            422, "empty_document", "This document has too little text to review."
        )
    if len(text) > settings.max_doc_chars:
        raise EngineError(
            413,
            "document_too_large",
            f"This document is longer than the {settings.max_doc_chars:,}-character limit.",
        )
    return text


def _store_document(
    db: DbSession, user: User, review, filename: str, data: bytes
) -> list[str]:
    """Archive the original ``.docx`` in the bucket (plan §4.5), then enforce the per-user
    retention cap. Mutates ``review.doc_object_key``/``doc_bytes`` in place (persisted by the
    caller's own commit) and returns warnings to fold into the review result: empty when the
    bucket is disabled (an expected, silent state) OR the upload succeeded; ``["document_not_stored"]``
    only on an actual upload failure while the bucket IS configured (fail-soft — the review still
    succeeds).

    BLOCKING — callers MUST run this off the event loop (``asyncio.to_thread``). boto3 is
    synchronous and ``enforce_retention`` adds more S3 round-trips on top of the upload; called
    inline this froze the entire service in production, so no request was served at all — not
    ``/healthz``, and not the landing page people download the manifest from."""
    if not bucket.enabled():
        return []
    key = bucket.put_document(user.id, review.id, filename, data)
    if key is None:
        log.warning("reviews.document_not_stored", review_id=review.id)
        return ["document_not_stored"]
    review.doc_object_key = key
    review.doc_bytes = len(data)
    db.flush()  # so enforce_retention's own SELECT sees this row's new key within the same txn
    bucket.enforce_retention(db, user.id)
    return []


async def _preflight(user: User, db: DbSession) -> None:
    if not user.openrouter_key_enc:
        raise EngineError(409, "no_openrouter_key", "Add your OpenRouter key first.")
    if settings.max_monthly_cost_usd > 0:
        usage = reviews_repo.usage_for_user(db, user)
        if usage["cost_this_month_usd"] >= settings.max_monthly_cost_usd:
            raise EngineError(
                402,
                "budget_exceeded",
                f"Monthly review budget (${settings.max_monthly_cost_usd:.2f}) reached.",
            )


@router.post("")
async def create_review(
    file: UploadFile = File(...),
    mode: str = Form(...),
    our_side: str = Form(""),
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    if mode not in ("quick", "deep"):
        raise EngineError(422, "invalid_mode", "mode must be 'quick' or 'deep'.")
    await _preflight(user, db)

    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise EngineError(
            413,
            "file_too_large",
            f"File exceeds the {settings.max_upload_mb} MB limit.",
        )
    text = _parse_upload(data)

    if not await _slots.try_acquire():
        raise EngineError(
            429,
            "review_capacity",
            "The review service is at capacity — try again in a moment.",
        )

    filename = file.filename or "document.docx"
    assert user.openrouter_key_enc is not None  # guaranteed by _preflight above
    api_key = crypto.decrypt(user.openrouter_key_enc)
    models = _models_for(user)

    if mode == "quick":
        try:
            review = reviews_repo.create_review(
                db,
                user,
                filename=filename,
                mode=mode,
                our_side=our_side,
                doc_sha256=reviews_repo.doc_sha256(data),
                status="running",
            )
            db.commit()
            doc_warnings = await asyncio.to_thread(
                _store_document, db, user, review, filename, data
            )
            started = time.perf_counter()
            try:
                result = await asyncio.to_thread(
                    run_review, text, mode, our_side, api_key, models=models
                )
            except ProviderError as exc:
                reviews_repo.fail_review(db, review, error_code_for(exc))
                raise EngineError(
                    502, error_code_for(exc), "The review could not be completed."
                ) from exc
            result.warnings.extend(doc_warnings)
            duration_ms = int((time.perf_counter() - started) * 1000)
            reviews_repo.complete_review(db, review, result, duration_ms=duration_ms)
            return JSONResponse(
                reviews_repo.result_to_json(review, result), status_code=200
            )
        finally:
            await _slots.release()

    # deep: 202 + queued row, computed by a background task that owns the slot until it's done.
    review = reviews_repo.create_review(
        db,
        user,
        filename=filename,
        mode=mode,
        our_side=our_side,
        doc_sha256=reviews_repo.doc_sha256(data),
        status="queued",
    )
    db.commit()
    doc_warnings = await asyncio.to_thread(
        _store_document, db, user, review, filename, data
    )
    # Commit BEFORE handing the row to the background task. _store_document only flushes, so this
    # session is still holding a row lock on the review; the task's first act is to UPDATE that
    # same row, and it would block on the lock until this request's session is cleaned up — which
    # cannot happen while the task is blocking the event loop it would be cleaned up on.
    db.commit()
    review_id = review.id
    asyncio.create_task(
        _run_deep_review(review_id, text, our_side, api_key, models, doc_warnings)
    )
    return JSONResponse(
        {"id": review_id, "status": "queued"},
        status_code=202,
        headers={"Location": f"/api/reviews/{review_id}"},
    )


async def _run_deep_review(
    review_id: str,
    text: str,
    our_side: str,
    api_key: str,
    models: ModelChoice,
    doc_warnings: list[str],
) -> None:
    """The deep-mode background task: run the whole job in ONE worker thread, then free the slot.

    Nothing blocking may touch the event loop here. The DB calls below look harmless, but
    ``review.status = "running"; commit()`` is an UPDATE on a row the spawning request may still
    hold a lock on — and waiting for that lock on the loop froze the entire service (no /healthz,
    no landing page) until the container was restarted. A worker thread can wait safely; the loop
    cannot, because it is what releases the lock.
    """
    try:
        await asyncio.to_thread(
            _deep_review_blocking,
            review_id,
            text,
            our_side,
            api_key,
            models,
            doc_warnings,
        )
    finally:
        await _slots.release()


def _deep_review_blocking(
    review_id: str,
    text: str,
    our_side: str,
    api_key: str,
    models: ModelChoice,
    doc_warnings: list[str],
) -> None:
    """The deep review, start to finish, on a worker thread. Opens its OWN DB session (the
    request's session is gone by the time this runs). ``doc_warnings`` carries forward any
    ``document_not_stored`` warning from the bucket upload the request handler already did."""
    from ..models import (
        Review,  # local import: avoids a module-level cycle with reviews_repo
    )

    db = SessionLocal()
    try:
        review = db.get(Review, review_id)
        if review is None:
            return
        review.status = "running"
        db.commit()
        started = time.perf_counter()
        try:
            result = run_review(text, "deep", our_side, api_key, models=models)
        except ProviderError as exc:
            reviews_repo.fail_review(db, review, error_code_for(exc))
            return
        except Exception:  # noqa: BLE001 — a background task must never crash the process
            log.exception("reviews.deep_task_failed", review_id=review_id)
            reviews_repo.fail_review(db, review, "internal_error")
            return
        result.warnings.extend(doc_warnings)
        duration_ms = int((time.perf_counter() - started) * 1000)
        reviews_repo.complete_review(db, review, result, duration_ms=duration_ms)
    finally:
        db.close()


@router.get("/{review_id}")
def get_review(
    review_id: str,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    review = reviews_repo.get_owned(db, user, review_id)
    if review is None:
        raise EngineError(404, "not_found", "No review with that id.")
    if review.status in ("queued", "running"):
        return {"id": review.id, "status": review.status}
    if review.status == "failed":
        return {"id": review.id, "status": "failed", "error": review.error}
    return review.result_json or {"id": review.id, "status": review.status}


@router.get("/{review_id}/document")
def get_review_document(
    review_id: str,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """302 to a 15-minute presigned GET URL for the review's original ``.docx`` (plan §4.5).
    Owner-only: :func:`reviews_repo.get_owned` returns ``None`` for another user's review, which
    this maps to the SAME 404 as "no such review" — an access-control boundary must never leak
    whether a resource merely doesn't exist vs. belongs to someone else."""
    review = reviews_repo.get_owned(db, user, review_id)
    if review is None or not review.doc_object_key:
        raise EngineError(404, "not_found", "No stored document for this review.")
    url = bucket.presigned_get_url(review.doc_object_key)
    if url is None:
        raise EngineError(404, "not_found", "The stored document is unavailable.")
    return RedirectResponse(url, status_code=302)


@router.get("")
def list_reviews(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    limit = max(1, min(limit, 100))
    reviews = reviews_repo.list_for_user(db, user, limit=limit)
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "filename": r.filename,
            "mode": r.mode,
            "status": r.status,
            "doc_type": r.doc_type,
            "risk_tier": r.risk_tier,
            "adherence_score": r.adherence_score,
            "findings_count": r.findings_count,
            "cost_usd": r.cost_usd,
            "document_stored": bool(r.doc_object_key),
        }
        for r in reviews
    ]


@router.delete("/{review_id}", status_code=204)
def delete_review(
    review_id: str,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
) -> None:
    review = reviews_repo.get_owned(db, user, review_id)
    if review is None:
        raise EngineError(404, "not_found", "No review with that id.")
    bucket.delete_object(review.doc_object_key)  # plan §4.5: object goes, then the row
    db.delete(review)
    db.commit()
