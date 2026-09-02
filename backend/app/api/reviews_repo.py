"""Persistence for ``reviews`` + ``llm_calls`` (plan §4.2/§4.3). Small and dumb on purpose — every
function here is one query or one insert; the review pipeline itself lives in ``app.agents``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..agents.orchestrator import ReviewResult
from ..ai.ledger import LlmCallRecord
from ..models import LlmCall, Review, User


def doc_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_review(
    db: DbSession,
    user: User,
    *,
    filename: str,
    mode: str,
    our_side: str,
    doc_sha256: str | None = None,
    status: str = "queued",
) -> Review:
    """Insert a new review row. ``status`` is ``"running"`` for a synchronous (quick) review the
    caller is about to compute inline, or ``"queued"`` for an async (deep) one a background task
    will pick up. Does not commit — the caller controls the transaction boundary."""
    review = Review(
        user_id=user.id,
        filename=filename,
        doc_sha256=doc_sha256,
        our_side=our_side or None,
        mode=mode,
        status=status,
    )
    db.add(review)
    db.flush()  # assigns review.id without ending the transaction
    return review


def mark_running(db: DbSession, review: Review) -> None:
    review.status = "running"
    db.commit()


def result_to_json(review: Review, result: ReviewResult) -> dict:
    """The full JSON payload the add-in renders (plan §4.2 — field names are the contract)."""
    return {
        "id": review.id,
        "status": "done",
        "mode": review.mode,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "duration_ms": review.duration_ms,
        "filename": review.filename,
        "doc_type": result.doc_type,
        "our_side": result.our_side,
        "summary": result.summary,
        "risk_tier": result.risk_tier,
        "adherence_score": result.adherence_score,
        "counts": result.counts,
        "findings": [f.as_dict() for f in result.findings],
        "coverage": result.coverage.as_dict() if result.coverage else None,
        "warnings": result.warnings,
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "calls": [
                {
                    "agent": c.agent,
                    "model": c.model,
                    "cost_usd": c.cost_usd,
                    "latency_ms": c.latency_ms,
                }
                for c in result.calls
            ],
        },
        "playbook_version": result.playbook_version,
        "document_stored": bool(review.doc_object_key),
    }


def complete_review(
    db: DbSession, review: Review, result: ReviewResult, *, duration_ms: int
) -> Review:
    """Persist a finished review: flat summary columns (for fast list/usage queries) plus the full
    ``result_json`` the add-in renders. Also writes one ``llm_calls`` row per gateway call. Commits."""
    review.finished_at = datetime.now(UTC)
    review.duration_ms = duration_ms
    review.doc_type = result.doc_type
    review.our_side = result.our_side
    review.status = "done"
    review.risk_tier = result.risk_tier
    review.adherence_score = result.adherence_score
    review.findings_count = len(result.findings)
    review.input_tokens = result.input_tokens
    review.output_tokens = result.output_tokens
    review.cost_usd = result.cost_usd
    review.error = None
    review.result_json = result_to_json(review, result)
    persist_llm_calls(db, review, result.calls)
    db.commit()
    return review


def persist_llm_calls(
    db: DbSession, review: Review, calls: list[LlmCallRecord]
) -> None:
    """One ``llm_calls`` row per gateway call this review made (plan §4.2). Does not commit —
    callers batch this with the review-row update in one transaction."""
    for call in calls:
        db.add(
            LlmCall(
                review_id=review.id,
                user_id=review.user_id,
                agent=call.agent,
                model=call.model,
                provider=call.provider,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                cached_tokens=call.cached_tokens,
                cost_usd=call.cost_usd,
                latency_ms=call.latency_ms,
                ok=call.ok,
                error=call.error,
            )
        )


def fail_review(
    db: DbSession,
    review: Review,
    error_code: str,
    *,
    duration_ms: int | None = None,
    calls: list[LlmCallRecord] | None = None,
) -> Review:
    """Mark a review failed with a stable error code (plan §4.2: no_zdr_route, rate_limited,
    insufficient_credits, timeout, ...). Persists any calls made before the failure (partial spend
    is still real spend — it must still be metered). Commits."""
    review.status = "failed"
    review.error = error_code
    review.finished_at = datetime.now(UTC)
    if duration_ms is not None:
        review.duration_ms = duration_ms
    if calls:
        review.input_tokens = sum(c.prompt_tokens for c in calls)
        review.output_tokens = sum(c.completion_tokens for c in calls)
        review.cost_usd = round(sum(c.cost_usd for c in calls), 6)
        persist_llm_calls(db, review, calls)
    db.commit()
    return review


def list_for_user(db: DbSession, user: User, *, limit: int = 20) -> list[Review]:
    return list(
        db.execute(
            select(Review)
            .where(Review.user_id == user.id)
            .order_by(Review.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def get_owned(db: DbSession, user: User, review_id: str) -> Review | None:
    """A review row, only if it belongs to ``user`` (owner-only reads — plan §4.2)."""
    review = db.get(Review, review_id)
    if review is None or review.user_id != user.id:
        return None
    return review


def usage_for_user(db: DbSession, user: User, *, since: datetime | None = None) -> dict:
    """Aggregate spend/review counts for ``user`` since ``since`` (defaults to the start of the
    current UTC month) — backs the monthly-budget pre-flight check and Phase 3's usage screen."""
    since = since or datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    row = db.execute(
        select(
            func.count(Review.id), func.coalesce(func.sum(Review.cost_usd), 0.0)
        ).where(
            Review.user_id == user.id,
            Review.created_at >= since,
            Review.status == "done",
        )
    ).one()
    count, cost = row
    return {
        "reviews_this_month": int(count),
        "cost_this_month_usd": round(float(cost), 6),
    }


def stale_job_cutoff(minutes: int = 15) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def fail_stale_jobs(db: DbSession, *, older_than_minutes: int = 15) -> int:
    """Boot-time crash recovery (plan §3): any ``queued``/``running`` row older than
    ``older_than_minutes`` means the process restarted mid-review — mark it failed rather than
    leaving the add-in polling forever. Returns the number of rows updated."""
    cutoff = stale_job_cutoff(older_than_minutes)
    rows = list(
        db.execute(
            select(Review).where(
                Review.status.in_(("queued", "running")), Review.created_at < cutoff
            )
        ).scalars()
    )
    for review in rows:
        review.status = "failed"
        review.error = "service_restarted"
        review.finished_at = datetime.now(UTC)
    if rows:
        db.commit()
    return len(rows)
