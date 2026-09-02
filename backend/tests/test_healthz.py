"""/healthz liveness + correlation-id middleware, exercised over an in-process ASGI transport."""

from __future__ import annotations

import httpx

from app.config import Settings
from app.main import create_app


def _app() -> object:
    # Zero-env settings: the app must boot and be healthy with nothing configured.
    return create_app(Settings(_env_file=None))


async def test_healthz_ok_and_mints_correlation_id() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # The middleware mints and echoes a correlation id when the caller supplies none.
    assert resp.headers.get("x-correlation-id")


async def test_healthz_propagates_supplied_correlation_id() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz", headers={"X-Correlation-Id": "abc-123"})
    assert resp.headers["x-correlation-id"] == "abc-123"


async def test_unknown_route_is_json_404() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/does/not/exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert body["path"] == "/does/not/exist"
    # Even the default-deny 404 carries a correlation id.
    assert resp.headers.get("x-correlation-id")
