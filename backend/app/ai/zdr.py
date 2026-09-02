"""OpenRouter's Zero-Data-Retention endpoint list — fetch, cache, and validate against it.

Every model a user picks (quick/deep tier, ``PUT /api/me/models``) and every model the review
pipeline calls (Phase 2) must be a ZDR route: ``provider: {data_collection: "deny", zdr: true}``
on the request is only half the story — the ROUTE itself has to support it, which OpenRouter
publishes at ``GET /endpoints/zdr``. This module is the one place that knows that response shape;
everything else asks :func:`list_zdr_models` / :func:`is_zdr_model`.

Filtered to ``status == "healthy"`` (or unset — some rows omit it) chat models whose
``supported_parameters`` includes ``response_format``, since those are the only routes the
classifier/reviewer/coverage agents (Phase 2) can actually use for structured output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..config import settings
from ..telemetry import get_logger

log = get_logger("legal_helper.ai.zdr")

#: How long a fetched list is served from the in-process cache before the next call re-fetches.
CACHE_TTL_S = 600.0


@dataclass(frozen=True)
class ZdrModel:
    id: str
    name: str
    provider: str
    context_length: int | None
    prompt_usd_per_m: float | None
    completion_usd_per_m: float | None


class _Cache:
    """A single in-process TTL cache slot — one ZDR list for the whole process, shared across
    users (the list itself isn't user-specific; only whether a user's key is valid to fetch it)."""

    def __init__(self) -> None:
        self._models: list[ZdrModel] | None = None
        self._fetched_at: float = 0.0

    def get(self) -> list[ZdrModel] | None:
        if self._models is None or (time.monotonic() - self._fetched_at) > CACHE_TTL_S:
            return None
        return self._models

    def set(self, models: list[ZdrModel]) -> None:
        self._models = models
        self._fetched_at = time.monotonic()

    def clear(self) -> None:
        self._models = None


_cache = _Cache()


def _openrouter_client() -> httpx.AsyncClient:
    """Factory so tests can monkeypatch this to inject an ``httpx.MockTransport``."""
    return httpx.AsyncClient(base_url=settings.openrouter_base_url, timeout=15.0)


def _parse_endpoint(row: dict) -> ZdrModel | None:
    """One row of ``GET /endpoints/zdr`` -> a :class:`ZdrModel`, or ``None`` if it isn't a
    healthy, structured-output-capable chat route."""
    status = row.get("status")
    # OpenRouter reports a NUMERIC status per endpoint: 0 is serving normally, negative means
    # deranked or disabled. (An earlier reading of this as the string "healthy" silently rejected
    # every row, which emptied the model picker without any error anywhere.)
    if isinstance(status, bool):
        return None
    if isinstance(status, int | float):
        if status < 0:
            return None
    elif status is not None and status != "healthy":
        return None
    supported = set(row.get("supported_parameters") or [])
    if "response_format" not in supported:
        return None
    # the rows key the model as `model_id`; `id`/`slug` are accepted only as a fallback shape
    model_id = row.get("model_id") or row.get("id") or row.get("slug") or ""
    if not model_id:
        return None
    pricing = row.get("pricing") or {}
    prompt = pricing.get("prompt")
    completion = pricing.get("completion")
    provider = row.get("provider_name") or row.get("provider") or model_id.split("/")[0]
    return ZdrModel(
        id=model_id,
        # `model_name` is the readable one ("Z.AI: GLM 5.3 Flash"); `name` is the per-endpoint
        # label ("Modal | z-ai/glm-5.3-flash"), which is not what a picker should show.
        name=row.get("model_name") or row.get("name") or model_id,
        provider=provider,
        context_length=row.get("context_length"),
        prompt_usd_per_m=(float(prompt) * 1_000_000) if prompt is not None else None,
        completion_usd_per_m=(float(completion) * 1_000_000)
        if completion is not None
        else None,
    )


async def fetch_zdr_models(
    api_key: str, *, client: httpx.AsyncClient | None = None
) -> list[ZdrModel]:
    """Hit OpenRouter's live ZDR endpoint list — UNCACHED. Raises ``httpx.HTTPStatusError`` on a
    non-2xx response."""
    owns_client = client is None
    c = client or _openrouter_client()
    try:
        resp = await c.get(
            "/endpoints/zdr", headers={"Authorization": f"Bearer {api_key}"}
        )
        resp.raise_for_status()
    finally:
        if owns_client:
            await c.aclose()
    body = resp.json()
    rows = body.get("data") if isinstance(body, dict) else None
    if rows is None:
        rows = []
    # One row per PROVIDER endpoint, so a single model appears many times (18 rows for
    # glm-5.3-flash alone). The picker wants models, not routes — keep the first sighting of each.
    models: list[ZdrModel] = []
    seen: set[str] = set()
    for row in rows:
        parsed = _parse_endpoint(row)
        if parsed is not None and parsed.id not in seen:
            seen.add(parsed.id)
            models.append(parsed)
    return models


async def list_zdr_models(
    api_key: str, *, force_refresh: bool = False
) -> list[ZdrModel]:
    """The cached (10-minute, in-process) filtered ZDR model list, fetched with ``api_key`` on a
    cache miss."""
    if not force_refresh:
        cached = _cache.get()
        if cached is not None:
            return cached
    models = await fetch_zdr_models(api_key)
    _cache.set(models)
    return models


async def is_zdr_model(model_id: str, api_key: str) -> bool:
    """Whether ``model_id`` appears in the (cached) ZDR list — the guard behind every model
    choice the app accepts (``PUT /api/me/models``; the review pipeline in Phase 2)."""
    models = await list_zdr_models(api_key)
    return any(m.id == model_id for m in models)
