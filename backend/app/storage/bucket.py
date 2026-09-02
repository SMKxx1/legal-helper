"""Railway bucket (S3-compatible, boto3) storage for each review's original ``.docx`` (plan §4.5).

The ``bucket`` capability (``app.capabilities``) is enabled only when all four S3-compatible
settings are present; :func:`enabled` mirrors that same check here so every function in this
module is a safe no-op when the capability is disabled — reviews still succeed, ``document_stored``
is simply ``false`` ("Capability disabled (local dev default, no MinIO) -> document_stored always
false, nothing else breaks").

Every operation is ALSO fail-soft against a runtime failure (a reachable-but-broken bucket): the
review is the product, the archive is a convenience. A failure here is logged and turned into
``None``/a no-op — never an exception that could fail an otherwise-successful review.
"""

from __future__ import annotations

import re
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import settings
from ..models import Review
from ..telemetry import get_logger

log = get_logger("legal_helper.storage.bucket")

#: The add-in always sends `.docx` (plan assumption, §1).
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
#: Presigned GET URL lifetime (plan §4.5: "valid 15 minutes").
PRESIGNED_URL_TTL_S = 900

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def enabled() -> bool:
    """True iff all four S3-compatible fields are configured — mirrors the ``bucket`` capability
    (``app.capabilities.default_capabilities``); kept as its own check so this module never
    depends on the capability registry being built first."""
    return bool(
        settings.s3_endpoint
        and settings.s3_bucket
        and settings.s3_access_key_id
        and settings.s3_secret_access_key
    )


@lru_cache
def _client():
    """One boto3 S3 client per process. Settings are fixed at boot, so this is cached; a test that
    needs different S3 config calls ``_client.cache_clear()`` first (or, more simply, monkeypatches
    the module-level functions directly — see ``tests/test_review_document.py``)."""
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region or "auto",
        # Bounded on purpose. botocore's defaults (60s connect, 60s read, legacy retry mode) let a
        # single stalled bucket call hold a request for minutes; a review is worth far less than
        # the archive copy of its document, and this path is already fail-soft.
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def safe_filename(filename: str) -> str:
    """A filename safe to embed in an object key: just the basename, with anything outside
    ``[A-Za-z0-9_.-]`` collapsed to ``_`` (no path traversal, no header-breaking characters)."""
    name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    return name or "document.docx"


def object_key(user_id: str, review_id: str, filename: str) -> str:
    """The bucket key layout from plan §4.5: ``users/{user_id}/reviews/{review_id}/{safe_filename}``."""
    return f"users/{user_id}/reviews/{review_id}/{safe_filename(filename)}"


def put_document(
    user_id: str, review_id: str, filename: str, data: bytes
) -> str | None:
    """Upload the original ``.docx``. Returns the object key on success, ``None`` if the bucket is
    disabled OR the upload failed (logged either way the caller can distinguish by calling
    :func:`enabled` first — see ``routes_reviews._store_document``)."""
    if not enabled():
        return None
    key = object_key(user_id, review_id, filename)
    try:
        _client().put_object(
            Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=DOCX_MIME
        )
    except (BotoCoreError, ClientError) as exc:
        log.warning("bucket.put_failed", review_id=review_id, error=str(exc))
        return None
    return key


def presigned_get_url(key: str, *, expires_in: int = PRESIGNED_URL_TTL_S) -> str | None:
    """A short-lived GET URL for ``key``, or ``None`` if the bucket is disabled or unreachable."""
    if not enabled():
        return None
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    except (BotoCoreError, ClientError) as exc:
        log.warning("bucket.presign_failed", key=key, error=str(exc))
        return None


def delete_object(key: str | None) -> None:
    """Best-effort delete. A failure here must never block deleting/updating the DB row that
    referenced it — the row's cleanup is what actually matters."""
    if not key or not enabled():
        return
    try:
        _client().delete_object(Bucket=settings.s3_bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        log.warning("bucket.delete_failed", key=key, error=str(exc))


def enforce_retention(
    db: DbSession, user_id: str, *, max_docs: int | None = None
) -> None:
    """Retention cap (plan §4.5): once ``user_id`` has more than ``max_docs`` (default
    ``MAX_DOCS_PER_USER``) stored objects, delete the oldest beyond the cap and null their
    ``doc_object_key`` — the review ROW stays (it still counts for stats), only the archived
    original goes away. Commits only if it actually changed something."""
    cap = settings.max_docs_per_user if max_docs is None else max_docs
    rows = list(
        db.execute(
            select(Review)
            .where(Review.user_id == user_id, Review.doc_object_key.isnot(None))
            .order_by(Review.created_at.desc())
        ).scalars()
    )
    if len(rows) <= cap:
        return
    for review in rows[cap:]:
        delete_object(review.doc_object_key)
        review.doc_object_key = None
        review.doc_bytes = None
    db.commit()
