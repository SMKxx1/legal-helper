"""Persistence for the /v1 engine reviews (replaces the in-memory dict).

Stores each review's full serialized payload (round-trips verbatim to the API)
plus indexed metadata for history/audit, and groups re-reviews of the same
document under a Contract (light-CLM, ARCHITECTURE §4). Every write also records
a ReviewEvent for the audit trail.

Uses the app's ``SessionLocal`` (SQLite dev / Postgres prod); tests point it at a
throwaway DB by monkeypatching ``SessionLocal`` on this module.
"""

from __future__ import annotations

import logging
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.models import Contract, EngineReview, ReviewEvent
from app.playbook.release import playbook_release_id
from app.schemas import DEFAULT_ORG_ID

log = logging.getLogger("nda.repo")


def _clamp(value: str, n: int) -> str:
    """Truncate a string to a column's VARCHAR(n) width (Postgres rejects over-length values)."""
    return (value or "")[:n]


def _latest_contract_for_sha(s, doc_sha256: str, org_id: str) -> Contract | None:
    """Most-recent contract with this doc hash WITHIN ``org_id``, or None. Duplicate hashes are legal
    (migration 0002 relaxed the UNIQUE to a plain index), so dedup picks the newest row. SCOPED to the
    tenant (PL-8): two orgs uploading byte-identical documents must NOT collapse into one shared
    contract — that would attach one tenant's review to another tenant's contract."""
    return s.execute(
        select(Contract)
        .where(Contract.doc_sha256 == doc_sha256, Contract.org_id == org_id)
        .order_by(Contract.created_at.desc(), Contract.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _get_or_create_contract(
    s,
    doc_sha256: str,
    *,
    title: str,
    counterparty: str,
    contract_id: str | None = None,
    new_contract: bool = False,
    org_id: str | None = None,
) -> Contract:
    """Find the contract for this document, or create it.

    ``contract_id`` is an explicit bypass of the doc-hash dedup: when given, the review binds to
    THAT contract (a CLM document version), returning the existing row if present, else creating
    one with that id.

    ``new_contract`` forces a brand-new contract even when an identical ``doc_sha256`` exists — the
    deliberate "separate agreement that happens to share text" override. Ignored when an explicit
    ``contract_id`` is given (that already pins the contract).

    Without either, dedup by ``doc_sha256`` returns the MOST RECENT matching contract. Since
    migration 0002 relaxed the UNIQUE to a plain index, duplicate doc_sha256 rows are legal, so
    concurrent creators may each make a contract (acceptable for the CLM; the common sequential path
    still collapses to one). The savepoint/IntegrityError guard is retained defensively for any
    OTHER constraint — the doc_sha256 UNIQUE clash it used to catch no longer occurs.

    ``org_id`` scopes a newly-created contract to the caller's tenant (defaults to the bootstrap org
    via the column server default when None).
    """
    eff_org = (
        org_id or DEFAULT_ORG_ID
    )  # None -> the column's bootstrap default; dedup matches it
    org_kwargs = {"org_id": org_id} if org_id else {}
    if contract_id is not None:
        existing = s.get(Contract, contract_id)
        if existing is not None:
            return existing
        c = Contract(
            id=contract_id,
            contract_type="nda",
            title=title,
            counterparty_name=counterparty,
            doc_sha256=doc_sha256 or None,
            **org_kwargs,
        )
        s.add(c)
        s.flush()
        return c

    if not new_contract and doc_sha256:
        existing = _latest_contract_for_sha(s, doc_sha256, eff_org)
        if existing is not None:
            return existing
    c = Contract(
        contract_type="nda",
        title=title,
        counterparty_name=counterparty,
        doc_sha256=doc_sha256 or None,
        **org_kwargs,
    )
    try:
        # add INSIDE the savepoint so a constraint clash is raised by the body's flush; the savepoint
        # rollback then cleanly detaches c (no s.expunge — expunging an already-detached object raises
        # InvalidRequestError). Largely defensive: 0002 relaxed doc_sha256 to a non-unique index, so
        # only an effectively-impossible PK collision could trip this branch today.
        with s.begin_nested():
            s.add(c)
            s.flush()
    except IntegrityError:
        # A forced new contract must NOT silently collapse into a dedup match on a clash — re-raise.
        if new_contract:
            raise
        won = _latest_contract_for_sha(s, doc_sha256, eff_org)
        if won is None:
            raise
        return won
    return c


def persist_review_in_session(
    s,
    payload: dict,
    *,
    mode: str,
    source_channel: str = "api",
    doc_filename: str = "",
    doc_sha256: str = "",
    norm_sha256: str = "",
    contract_id: str | None = None,
    new_contract: bool = False,
    org_id: str | None = None,
    actor_user_id: str | None = None,
) -> Contract:
    """Add the EngineReview + 'reviewed' ReviewEvent to ``s`` (no commit) and return the contract.

    Shared by ``save_review`` (own session) and the in-process CLM wrapper (``app.clm.engine_invoke``,
    which passes the router's session so the review + lifecycle advance commit as ONE transaction).
    """
    review_id = payload["review_id"]
    routing = payload.get("routing") or {}
    counterparty = (routing.get("router", {}) or {}).get("counterparty_name", "") or ""
    # Clamp variable, model/client-controlled strings to their VARCHAR widths so an over-long value
    # (a long client filename, a free-form playbook_version, model-generated counterparty text) can't
    # fail the INSERT on Postgres — which enforces VARCHAR(n) — and silently lose a PAID review via
    # the best-effort save. SQLite (dev/tests) doesn't enforce widths, which is why this hid.
    doc_filename = _clamp(doc_filename, 512)
    title = _clamp(doc_filename or payload.get("perspective", "") or "NDA review", 512)
    contract = _get_or_create_contract(
        s,
        doc_sha256,
        title=title,
        counterparty=_clamp(counterparty, 255),
        contract_id=contract_id,
        new_contract=new_contract,
        org_id=org_id,
    )
    s.add(
        EngineReview(
            id=review_id,
            contract_id=contract.id,
            org_id=contract.org_id,
            source_channel=source_channel or "api",
            actor_user_id=actor_user_id or None,
            mode=mode,
            playbook_version=_clamp(str(payload.get("playbook_version", "") or ""), 32),
            perspective=_clamp(payload.get("perspective", "") or "", 32),
            risk_tier=_clamp(payload.get("risk_tier", "") or "", 16),
            adherence_score=payload.get("adherence_score"),
            cost_usd=payload.get("cost_usd"),
            doc_filename=doc_filename,
            doc_sha256=doc_sha256,
            norm_sha256=norm_sha256,
            # The playbook release that graded this run — the content-cache version key (audit #3).
            # Computed here so every persist path (sync route, async worker, in-process CLM) stamps it.
            playbook_release=playbook_release_id() or None,
            payload_json=payload,
        )
    )
    s.add(
        ReviewEvent(
            contract_id=contract.id,
            org_id=contract.org_id,
            review_id=review_id,
            event_type="reviewed",
            detail=f"{mode} via {source_channel}",
        )
    )
    return contract


def save_review(
    payload: dict,
    *,
    mode: str,
    source_channel: str = "api",
    doc_filename: str = "",
    doc_sha256: str = "",
    norm_sha256: str = "",
    contract_id: str | None = None,
    new_contract: bool = False,
    org_id: str | None = None,
    actor_user_id: str | None = None,
) -> dict:
    """Persist a serialized review payload (own session, committed). Returns the payload unchanged.

    ``payload`` is the API ``_serialize`` output (carries review_id, risk_tier,
    adherence_score, perspective, playbook_version, routing, counts, cost_usd, ...).
    ``norm_sha256`` is ``simcache.norm_sha256(text)`` — the Tier-1 normalized-text
    cache key; blank on legacy/test callers, which leaves the column empty (no match).
    ``actor_user_id`` is the principal the engine run is attributed to (a service-account
    principal id like ``svc:default`` for the machine path, P0-12; a UserAccount id for the
    in-process CLM path later) — None for legacy/unattributed callers.
    """
    with SessionLocal() as s:
        persist_review_in_session(
            s,
            payload,
            mode=mode,
            source_channel=source_channel,
            doc_filename=doc_filename,
            doc_sha256=doc_sha256,
            norm_sha256=norm_sha256,
            contract_id=contract_id,
            new_contract=new_contract,
            org_id=org_id,
            actor_user_id=actor_user_id,
        )
        s.commit()
    return payload


def monthly_cost_usd(actor_user_id: str) -> float:
    """Total engine spend attributed to ``actor_user_id`` in the CURRENT calendar month (UTC).

    Drives the per-principal monthly cost cap (P0-12). Returns 0.0 for a blank principal or when
    nothing is attributed yet. Scaffold note: this is a best-effort pre-flight read — the precise
    under-concurrency cap counter is hardened in Phase 1 (P1-4)."""
    if not actor_user_id:
        return 0.0
    from datetime import datetime

    from sqlalchemy import func

    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as s:
        total = s.execute(
            select(func.coalesce(func.sum(EngineReview.cost_usd), 0.0)).where(
                EngineReview.actor_user_id == actor_user_id,
                EngineReview.created_at >= start,
            )
        ).scalar_one()
    return float(total or 0.0)


def find_existing_review(
    doc_sha256: str, mode: str, org_id: str = DEFAULT_ORG_ID
) -> dict | None:
    """Most recent review of this exact document + mode WITHIN the caller's org (idempotency lookup).

    SCOPED to ``org_id`` (PL-8): the document-reuse cache must never serve one tenant's stored
    analysis to another — two orgs uploading byte-identical documents get isolated results.

    KEYED on the current playbook RELEASE (audit #3): a review graded by an OLD release never matches
    once the playbook changes, so a stale result is never re-served. Legacy NULL rows also miss (a
    release id is always non-NULL here), correctly falling through to a fresh review."""
    if not doc_sha256:
        return None
    with SessionLocal() as s:
        rec = s.execute(
            select(EngineReview)
            .where(
                EngineReview.doc_sha256 == doc_sha256,
                EngineReview.mode == mode,
                EngineReview.org_id == org_id,
                EngineReview.playbook_release == playbook_release_id(),
            )
            .order_by(EngineReview.created_at.desc(), EngineReview.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return rec.payload_json if rec else None


def find_similar_review(
    norm_sha256: str, mode: str, org_id: str = DEFAULT_ORG_ID
) -> dict | None:
    """Cache lookup for a re-submitted document (same ``mode``) WITHIN the caller's org, or None.

    Serves a stored review only when the re-submitted document's NORMALIZED TEXT is
    IDENTICAL (same document after unicode/whitespace/case/punctuation canonicalization;
    bridges cross-channel extraction differences, e.g. email PDF vs Word add-in DOCX).
    Zero content difference -> safe to reuse.

    SCOPED to ``org_id`` (PL-8): like the exact-sha tier, the normalized-text cache must never serve
    one tenant's stored analysis to another — identical text across orgs gets isolated results.

    Near-duplicate ("similar") matching is intentionally NOT done: for legal text a
    one-word change keeps the text near-identical yet can flip the meaning, so a
    similar-but-not-identical document must get a fresh review, never a reused one.

    KEYED on the current playbook RELEASE (audit #3), like the exact-sha tier: a review graded by an
    OLD release never matches once the playbook changes, and legacy NULL rows miss — both fall
    through to a fresh review.

    Returns the stored payload with a ``cache`` annotation so the human approver sees
    the result was reused, not freshly run.
    """
    if not settings.sim_cache_enabled or not norm_sha256:
        return None
    with SessionLocal() as s:
        rec = s.execute(
            select(EngineReview)
            .where(
                EngineReview.norm_sha256 == norm_sha256,
                EngineReview.mode == mode,
                EngineReview.org_id == org_id,
                EngineReview.playbook_release == playbook_release_id(),
            )
            .order_by(EngineReview.created_at.desc(), EngineReview.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if rec is not None:
            return _annotate_cache(rec, tier="normalized", similarity=1.0)
    return None


def _annotate_cache(rec: EngineReview, *, tier: str, similarity: float) -> dict:
    """Stored payload + a ``cache`` block flagging it as a reused (non-fresh) result."""
    # Copy so the per-request ``cache`` annotation never mutates the session-cached dict.
    payload = dict(rec.payload_json)
    payload["cache"] = {
        "hit": True,
        "tier": tier,
        "similarity": round(float(similarity), 4),
        "matched_review_id": rec.id,
        "matched_filename": rec.doc_filename or "",
        "matched_at": rec.created_at.isoformat() if rec.created_at else None,
    }
    return payload


def get_review(review_id: str, org_id: str = DEFAULT_ORG_ID) -> dict | None:
    """Return the stored payload for a review id WITHIN the caller's org, or None.

    SCOPED to ``org_id`` (PL-8): a review id is an unguessable handle, but tenant isolation must not
    depend on its secrecy — a principal can only read a review that belongs to its own org, so a
    leaked/guessed id from another tenant resolves to None (404 at the route)."""
    with SessionLocal() as s:
        rec = s.execute(
            select(EngineReview).where(
                EngineReview.id == review_id, EngineReview.org_id == org_id
            )
        ).scalar_one_or_none()
        return rec.payload_json if rec is not None else None


def list_reviews(
    *,
    org_id: str = DEFAULT_ORG_ID,
    limit: int = 50,
    offset: int = 0,
    contract_id: str | None = None,
) -> list[dict]:
    """History: summary rows for the caller's org, newest first. Column-only (never loads payload_json).

    SCOPED to ``org_id`` (PL-8): GET /v1/reviews must not enumerate other tenants' review metadata
    (ids, filenames, cost, risk)."""
    with SessionLocal() as s:
        stmt = (
            select(
                EngineReview.id,
                EngineReview.contract_id,
                EngineReview.created_at,
                EngineReview.source_channel,
                EngineReview.mode,
                EngineReview.risk_tier,
                EngineReview.adherence_score,
                EngineReview.cost_usd,
                EngineReview.doc_filename,
            )
            .where(EngineReview.org_id == org_id)
            .order_by(EngineReview.created_at.desc(), EngineReview.id.desc())
        )
        if contract_id:
            stmt = stmt.where(EngineReview.contract_id == contract_id)
        stmt = stmt.limit(min(max(limit, 1), 500)).offset(max(offset, 0))
        return [
            {
                "review_id": r.id,
                "contract_id": r.contract_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "source_channel": r.source_channel,
                "mode": r.mode,
                "risk_tier": r.risk_tier,
                "adherence_score": r.adherence_score,
                "cost_usd": r.cost_usd,
                "doc_filename": r.doc_filename,
            }
            for r in s.execute(stmt).all()
        ]


def find_review_by_idempotency_key(
    principal_id: str, key: str, org_id: str = DEFAULT_ORG_ID
) -> dict | None:
    """The stored review a prior identical flow-step (same principal + caller-supplied key)
    produced, annotated as a cache hit — or None. Folded into /v1/reviews AHEAD of the
    content-sha tiers: a retried n8n call replays the first result even if the retry uploads
    byte-different (e.g. re-exported) content. Org-scoped like every review read (PL-8)."""
    from app.models_bot import NdaIdempotencyKey

    if not key:
        return None
    with SessionLocal() as s:
        row = s.execute(
            select(NdaIdempotencyKey).where(
                NdaIdempotencyKey.principal_id == principal_id,
                NdaIdempotencyKey.purpose == "review",
                NdaIdempotencyKey.key == key[:128],
                NdaIdempotencyKey.org_id == org_id,
            )
        ).scalar_one_or_none()
        if row is None or not row.review_id:
            return None
        rec = s.execute(
            select(EngineReview).where(
                EngineReview.id == row.review_id, EngineReview.org_id == org_id
            )
        ).scalar_one_or_none()
        if rec is None:
            return None
        return _annotate_cache(rec, tier="idempotency_key", similarity=1.0)


def record_review_idempotency_key(
    principal_id: str, key: str, review_id: str, org_id: str = DEFAULT_ORG_ID
) -> None:
    """Map a caller-supplied idempotency key to the completed review (first writer wins;
    a concurrent duplicate's IntegrityError is the convergence signal, not an error)."""
    from app.models_bot import NdaIdempotencyKey

    if not key:
        return
    with SessionLocal() as s:
        s.add(
            NdaIdempotencyKey(
                principal_id=principal_id,
                purpose="review",
                key=key[:128],
                org_id=org_id,
                review_id=review_id,
            )
        )
        try:
            s.commit()
        except IntegrityError:
            s.rollback()


# --------------------------------------------------------------------------- #
# Async review jobs (3.1) — submit/poll persistence + the worker's crash-safe
# claim. All functions use this module's SessionLocal (tests repoint it).
# --------------------------------------------------------------------------- #
#: Visibility timeout: comfortably above the worst-case engine wall-clock (~3 x 150s
#: provider timeout x 2 sequential stages) so a live run's lease never expires under it.
REVIEW_JOB_LEASE_S = 30 * 60
#: Dead-letter cap: a job that failed this many claims stops retrying (status='failed').
REVIEW_JOB_MAX_ATTEMPTS = 3
#: Retry deferral per failed attempt (attempt N re-runs no sooner than N x this). Without it a
#: provider blip burns all attempts inside ~30s of claimer ticks and dead-letters good jobs.
REVIEW_JOB_RETRY_BACKOFF_S = 120


def create_review_job(
    *,
    org_id: str,
    principal_id: str,
    mode: str,
    scope: str,
    playbook_version: str | None,
    source_channel: str,
    doc_filename: str,
    doc_sha256: str,
    norm_sha256: str,
    incoming_text: str,
    original_text: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Persist a PENDING async review job (already validated + extracted). Returns the job id.

    IN-FLIGHT dedup: a retried submit (n8n timeout replay) while the first job is still
    pending/running must NOT enqueue a second paid run — if an open job in this org matches the
    caller's idempotency key OR the same (content, mode, scope), its id is returned instead.
    (The completed-run tiers — content caches + the idempotency mapping — only cover finished
    work; this closes the minutes-long in-flight window.)"""
    from sqlalchemy import ColumnElement, or_

    from app.models import EngineReviewJob

    with SessionLocal() as s:
        match_terms: list[ColumnElement[bool]] = [
            (EngineReviewJob.doc_sha256 == doc_sha256)
            & (EngineReviewJob.mode == mode)
            & (EngineReviewJob.scope == scope)
        ]
        if idempotency_key:
            match_terms.append(EngineReviewJob.idempotency_key == idempotency_key)
        existing = s.execute(
            select(EngineReviewJob.id)
            .where(
                EngineReviewJob.org_id == org_id,
                EngineReviewJob.status.in_(("pending", "running")),
                or_(*match_terms),
            )
            .order_by(EngineReviewJob.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        job = EngineReviewJob(
            org_id=org_id,
            principal_id=(principal_id or "")[:64],
            mode=mode,
            scope=scope,
            playbook_version=(playbook_version or None),
            source_channel=source_channel,
            doc_filename=_clamp(doc_filename, 512),
            doc_sha256=doc_sha256,
            norm_sha256=norm_sha256,
            idempotency_key=(idempotency_key or None),
            incoming_text=incoming_text,
            original_text=original_text,
        )
        s.add(job)
        s.commit()
        return job.id


def _job_out(job) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "attempts": job.attempts,
        "mode": job.mode,
        "scope": job.scope,
        "review_id": job.review_id,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def get_review_job(job_id: str, org_id: str = DEFAULT_ORG_ID) -> dict | None:
    """Job status for the poll endpoint, org-scoped (PL-8) — None for a foreign/unknown id."""
    from app.models import EngineReviewJob

    with SessionLocal() as s:
        job = s.execute(
            select(EngineReviewJob).where(
                EngineReviewJob.id == job_id, EngineReviewJob.org_id == org_id
            )
        ).scalar_one_or_none()
        return None if job is None else _job_out(job)


def claim_review_job(
    now=None,
    *,
    lease_s: int = REVIEW_JOB_LEASE_S,
    max_attempts: int = REVIEW_JOB_MAX_ATTEMPTS,
    session_factory=None,
) -> dict | None:
    """Atomically claim ONE runnable job (pending, or running with an EXPIRED lease — the
    crashed-worker re-queue). Same conditional-UPDATE claim shape as bot_dal.consume_request:
    the WHERE re-checks the state so two claimers can never win the same row. A job past the
    attempts cap is dead-lettered ('failed') instead of claimed. Returns the claimed job's
    fields (detached dict), or None when nothing is runnable."""
    import uuid as _uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import or_, update

    from app.models import EngineReviewJob

    now = now or datetime.now(UTC)
    factory = session_factory or SessionLocal
    with factory() as s:
        # A PENDING row with a future lease_expires_at is a retry DEFERRAL (backoff after a
        # failed attempt) — not claimable until it passes. Running rows re-claim on expiry.
        runnable = or_(
            (EngineReviewJob.status == "pending")
            & (
                EngineReviewJob.lease_expires_at.is_(None)
                | (EngineReviewJob.lease_expires_at < now)
            ),
            (EngineReviewJob.status == "running")
            & (EngineReviewJob.lease_expires_at < now),
        )
        while True:
            candidate = s.execute(
                select(EngineReviewJob)
                .where(runnable)
                .order_by(EngineReviewJob.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if candidate is None:
                return None

            if candidate.attempts >= max_attempts:
                # Dead-letter: never wedge the claimer on a poison job. Conditional so a
                # concurrent claimer's win is a no-op here. The captured text is cleared —
                # a terminal job never runs again.
                s.execute(
                    update(EngineReviewJob)
                    .where(
                        EngineReviewJob.id == candidate.id,
                        EngineReviewJob.status.in_(("pending", "running")),
                    )
                    .values(
                        status="failed",
                        error=f"attempts exhausted ({candidate.attempts})",
                        lease_expires_at=None,
                        claim_token=None,
                        incoming_text="",
                        original_text=None,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                s.commit()
                log.warning(
                    "review job %s dead-lettered after %s attempts",
                    candidate.id,
                    candidate.attempts,
                )
                continue  # look for the next runnable job

            token = _uuid.uuid4().hex
            res = s.execute(
                update(EngineReviewJob)
                .where(EngineReviewJob.id == candidate.id, runnable)
                .values(
                    status="running",
                    # SQL-side increment: never trust the earlier SELECT's snapshot (a
                    # concurrent claim/fail cycle between SELECT and UPDATE would be lost).
                    attempts=EngineReviewJob.attempts + 1,
                    lease_expires_at=now + timedelta(seconds=lease_s),
                    claim_token=token,
                    updated_at=now,
                )
                # No in-session sync: the ORM's Python-side 'evaluate' can't compare the
                # tz-aware bind with SQLite's naive round-trip; we refresh() after commit.
                .execution_options(synchronize_session=False)
            )
            s.commit()
            if (getattr(res, "rowcount", 0) or 0) != 1:
                continue  # lost the race — try the next candidate
            s.refresh(candidate)
            return {
                "job_id": candidate.id,
                "claim_token": token,
                "org_id": candidate.org_id,
                "principal_id": candidate.principal_id,
                "attempts": candidate.attempts,
                "mode": candidate.mode,
                "scope": candidate.scope,
                "playbook_version": candidate.playbook_version,
                "source_channel": candidate.source_channel,
                "doc_filename": candidate.doc_filename,
                "doc_sha256": candidate.doc_sha256,
                "norm_sha256": candidate.norm_sha256,
                "idempotency_key": candidate.idempotency_key,
                "incoming_text": candidate.incoming_text,
                "original_text": candidate.original_text,
            }


def complete_review_job(
    job_id: str, review_id: str, *, claim_token: str, session_factory=None
) -> bool:
    """Mark a claimed run done. FENCED on (status='running', claim_token): a zombie run whose
    lease expired and whose job was re-claimed holds a stale token and writes nothing — it can
    never clobber the live claim or resurrect a terminal row. The captured document text is
    cleared (the persisted review is the durable artifact). Returns True if the write landed."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.models import EngineReviewJob

    factory = session_factory or SessionLocal
    with factory() as s:
        res = s.execute(
            update(EngineReviewJob)
            .where(
                EngineReviewJob.id == job_id,
                EngineReviewJob.status == "running",
                EngineReviewJob.claim_token == claim_token,
            )
            .values(
                status="done",
                review_id=review_id,
                error=None,
                lease_expires_at=None,
                claim_token=None,
                incoming_text="",
                original_text=None,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        s.commit()
        landed = (getattr(res, "rowcount", 0) or 0) == 1
        if not landed:
            log.warning(
                "review job %s: completion by a stale claim was fenced off "
                "(lease expired and the job was re-claimed or finished elsewhere)",
                job_id,
            )
        return landed


def fail_review_job(
    job_id: str,
    error: str,
    *,
    claim_token: str,
    max_attempts: int = REVIEW_JOB_MAX_ATTEMPTS,
    retry_backoff_s: int = REVIEW_JOB_RETRY_BACKOFF_S,
    session_factory=None,
) -> None:
    """Record a failed run: back to 'pending' with a per-attempt retry deferral, or dead-letter
    'failed' once the attempts cap is reached. FENCED like complete_review_job — a stale claim's
    failure report is dropped (the live claim owns the row's state)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models import EngineReviewJob

    factory = session_factory or SessionLocal
    with factory() as s:
        job = s.get(EngineReviewJob, job_id)
        if job is None:
            return
        terminal = job.attempts >= max_attempts
        now = datetime.now(UTC)
        values: dict = {
            "status": "failed" if terminal else "pending",
            "error": (error or "")[:4000],
            "claim_token": None,
            "updated_at": now,
        }
        if terminal:
            values["lease_expires_at"] = None
            values["incoming_text"] = ""  # terminal rows never run again
            values["original_text"] = None
        else:
            # Retry deferral (attempt N waits N x backoff): a provider blip must not burn
            # every attempt within a few claimer ticks and dead-letter a good job.
            values["lease_expires_at"] = now + timedelta(
                seconds=retry_backoff_s * max(1, job.attempts)
            )
        res = s.execute(
            update(EngineReviewJob)
            .where(
                EngineReviewJob.id == job_id,
                EngineReviewJob.status == "running",
                EngineReviewJob.claim_token == claim_token,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        s.commit()
        if (getattr(res, "rowcount", 0) or 0) != 1:
            log.warning(
                "review job %s: failure report from a stale claim was fenced off",
                job_id,
            )


def record_event(
    *,
    review_id: str | None = None,
    contract_id: str | None = None,
    event_type: str,
    detail: str = "",
    org_id: str | None = None,
) -> None:
    """Append an audit event (no-op-safe: skips a dangling review_id/contract_id).

    Org-scoped by construction: when ``review_id``/``contract_id`` is given the row is fetched WITH
    the caller's ``org_id`` filter, so a cross-tenant id resolves to None and the event is dropped
    rather than written against (or inheriting the org of) another tenant's row. Callers should pass
    the principal's ``org_id``; without it the function still works but falls back to the row's own
    org (legacy single-org path)."""
    with SessionLocal() as s, s.begin():
        resolved_org = org_id or DEFAULT_ORG_ID
        if review_id is not None:
            stmt = select(EngineReview.org_id, EngineReview.contract_id).where(
                EngineReview.id == review_id
            )
            if org_id is not None:
                stmt = stmt.where(EngineReview.org_id == org_id)
            row = s.execute(stmt).first()
            if row is None:
                log.warning(
                    "record_event: unknown/cross-org review_id %s; dropping event",
                    review_id,
                )
                return
            if contract_id is None:
                contract_id = row.contract_id
            resolved_org = (
                row.org_id or resolved_org
            )  # inherit the review's tenant (PL-8)
        elif contract_id is not None:
            cstmt = select(Contract.org_id).where(Contract.id == contract_id)
            if org_id is not None:
                cstmt = cstmt.where(Contract.org_id == org_id)
            crow = s.execute(cstmt).first()
            if crow is not None:
                resolved_org = crow.org_id or resolved_org
        s.add(
            ReviewEvent(
                contract_id=contract_id,
                org_id=resolved_org,
                review_id=review_id,
                event_type=event_type,
                detail=detail,
            )
        )
