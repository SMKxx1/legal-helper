"""``GET /api/me/usage`` and ``GET /api/admin/usage`` (plan §4.3) — the numbers behind the add-in's
Usage tab and the admin usage table. Every gateway call already writes one ``llm_calls`` row
(``app.agents.orchestrator`` -> ``reviews_repo.persist_llm_calls``); this module only aggregates
what is already there. Both routes read ``reviews.cost_usd``/``llm_calls.cost_usd`` — never
recompute cost locally (plan §4.3: OpenRouter's ``usage.cost`` is the one authoritative number).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..auth.deps import get_current_user, require_admin
from ..config import settings
from ..db import get_db
from ..models import LlmCall, Review, User

router = APIRouter(prefix="/api", tags=["usage"])

#: ``GET /api/admin/usage`` covers the trailing window named in plan §4.3.
_ADMIN_WINDOW_DAYS = 60


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _count_and_cost(db: DbSession, *clauses) -> tuple[int, float]:
    """One ``(count, sum(cost_usd))`` query over ``reviews``, filtered by ``clauses``."""
    count, cost = db.execute(
        select(
            func.count(Review.id), func.coalesce(func.sum(Review.cost_usd), 0.0)
        ).where(*clauses)
    ).one()
    return int(count), round(float(cost), 6)


def _review_summary(r: Review) -> dict:
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "filename": r.filename,
        "mode": r.mode,
        "status": r.status,
        "doc_type": r.doc_type,
        "risk_tier": r.risk_tier,
        "cost_usd": r.cost_usd,
    }


@router.get("/me/usage")
def get_my_usage(
    user: User = Depends(get_current_user), db: DbSession = Depends(get_db)
) -> dict:
    """Plan §4.3's exact shape: totals, this-month, spend by mode/model, budget headroom, and the
    10 most recent reviews (any status — a failed review is still worth seeing in the list)."""
    done = Review.status == "done"
    reviews_total, cost_total_usd = _count_and_cost(db, Review.user_id == user.id, done)
    since = _month_start()
    reviews_this_month, cost_this_month_usd = _count_and_cost(
        db, Review.user_id == user.id, done, Review.created_at >= since
    )

    by_mode = {}
    for mode in ("quick", "deep"):
        n, cost = _count_and_cost(
            db, Review.user_id == user.id, done, Review.mode == mode
        )
        by_mode[mode] = {"n": n, "cost_usd": cost}

    by_model = [
        {"model": model, "calls": int(calls), "cost_usd": round(float(cost), 6)}
        for model, calls, cost in db.execute(
            select(
                LlmCall.model,
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
            )
            .where(LlmCall.user_id == user.id)
            .group_by(LlmCall.model)
            .order_by(func.sum(LlmCall.cost_usd).desc())
        ).all()
    ]

    last_review_at = db.execute(
        select(func.max(Review.created_at)).where(Review.user_id == user.id, done)
    ).scalar()

    recent = [
        _review_summary(r)
        for r in db.execute(
            select(Review)
            .where(Review.user_id == user.id)
            .order_by(Review.created_at.desc())
            .limit(10)
        ).scalars()
    ]

    monthly_cap = settings.max_monthly_cost_usd
    remaining_usd = (
        round(max(0.0, monthly_cap - cost_this_month_usd), 6)
        if monthly_cap > 0
        else None
    )

    return {
        "reviews_total": reviews_total,
        "reviews_this_month": reviews_this_month,
        "cost_total_usd": cost_total_usd,
        "cost_this_month_usd": cost_this_month_usd,
        "by_mode": by_mode,
        "by_model": by_model,
        "last_review_at": last_review_at.isoformat() if last_review_at else None,
        "budget": {"monthly_cap_usd": monthly_cap, "remaining_usd": remaining_usd},
        "recent": recent,
    }


@router.get("/admin/usage")
def get_admin_usage(
    user: User = Depends(require_admin), db: DbSession = Depends(get_db)
) -> dict:
    """Org-wide totals, per-user spend, and a per-day series over the trailing 60 days (plan §4.3)
    — role ``admin`` only (``require_admin`` -> 403 for anyone else)."""
    done = Review.status == "done"
    reviews, cost_usd = _count_and_cost(db, done)
    users = int(db.execute(select(func.count(User.id))).scalar() or 0)

    per_user = [
        {
            "username": username,
            "reviews": int(count),
            "cost_usd": round(float(cost), 6),
            "last_review_at": last.isoformat() if last else None,
        }
        for username, count, cost, last in db.execute(
            select(
                User.username,
                func.count(Review.id),
                func.coalesce(func.sum(Review.cost_usd), 0.0),
                func.max(Review.created_at),
            )
            .join(Review, Review.user_id == User.id)
            .where(done)
            .group_by(User.username)
            .order_by(func.sum(Review.cost_usd).desc())
        ).all()
    ]

    since = datetime.now(UTC) - timedelta(days=_ADMIN_WINDOW_DAYS)
    per_day = [
        {"day": str(day), "reviews": int(count), "cost_usd": round(float(cost), 6)}
        for day, count, cost in db.execute(
            select(
                func.date(Review.created_at),
                func.count(Review.id),
                func.coalesce(func.sum(Review.cost_usd), 0.0),
            )
            .where(done, Review.created_at >= since)
            .group_by(func.date(Review.created_at))
            .order_by(func.date(Review.created_at))
        ).all()
    ]

    return {
        "totals": {"users": users, "reviews": reviews, "cost_usd": cost_usd},
        "per_user": per_user,
        "per_day": per_day,
    }
