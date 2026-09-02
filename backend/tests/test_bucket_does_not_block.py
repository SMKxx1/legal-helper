"""A stalled bucket must not take the whole service down.

Regression test for a production outage: ``_store_document`` (boto3 ``put_object`` plus
``enforce_retention``'s extra S3 round-trips) was called inline from the async review handler, so
it ran ON the event loop. One stalled bucket call froze the process — for 77 minutes no request
was served at all, including ``/healthz`` and the landing page people download the manifest from.

The fix is that the upload runs in a worker thread. What that buys is exactly this: while a review
is stuck inside the bucket, the event loop still answers everyone else.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.api import routes_reviews


def test_store_document_is_never_called_inline_on_the_event_loop():
    """A direct call reads `_store_document(db, ...)`; the threaded hand-off reads
    `to_thread(_store_document, db, ...)`. Only the second form may appear."""
    import inspect

    src = " ".join(inspect.getsource(routes_reviews).split())  # normalise wrapping
    assert "BLOCKING" in inspect.getdoc(routes_reviews._store_document)
    assert "_store_document(db" not in src, (
        "called inline on the event loop — this is the outage"
    )
    assert (
        src.count("to_thread( _store_document,")
        + src.count("to_thread(_store_document,")
        == 2
    )


@pytest.mark.asyncio
async def test_a_stalled_bucket_leaves_the_event_loop_free():
    """With the upload on a worker thread, the loop keeps running while the bucket hangs."""
    release = threading.Event()
    entered = threading.Event()

    def stalled_upload(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)  # simulate a bucket that stopped answering
        return []

    task = asyncio.create_task(asyncio.to_thread(stalled_upload))
    await asyncio.to_thread(entered.wait, 2)
    assert entered.is_set(), "the upload never started"

    # the loop must still be responsive while that thread is stuck
    ticks = 0
    started = time.perf_counter()
    while time.perf_counter() - started < 0.2:
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks > 5, "event loop was blocked by the stalled upload"

    release.set()
    await task


def test_bucket_client_has_bounded_timeouts(monkeypatch):
    """botocore's defaults (60s connect, 60s read, legacy retries) are too slow to fail here."""
    from app.config import settings
    from app.storage import bucket

    monkeypatch.setattr(settings, "s3_endpoint", "https://bucket.example.com")
    monkeypatch.setattr(settings, "s3_bucket", "documents")
    monkeypatch.setattr(settings, "s3_access_key_id", "key")
    monkeypatch.setattr(settings, "s3_secret_access_key", "secret")
    bucket._client.cache_clear()
    client = bucket._client()
    cfg = client.meta.config
    assert cfg.connect_timeout <= 10
    assert cfg.read_timeout <= 30
    # botocore normalises max_attempts=2 into total_max_attempts=3 (the initial try + 2 retries)
    assert cfg.retries["total_max_attempts"] <= 3
    assert cfg.retries["mode"] == "standard"  # not the unbounded-ish legacy mode
    bucket._client.cache_clear()  # don't leak a configured client into other tests
