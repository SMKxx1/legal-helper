"""Meta endpoints: health check and active AI provider info."""

from __future__ import annotations

from fastapi import APIRouter

from ..ai import get_provider
from ..schemas import HealthOut, ProviderInfo, ProviderName
from ..settings_store import effective

router = APIRouter(prefix="/api", tags=["meta"])


def _provider_name() -> ProviderName:
    """The active provider. Anthropic is the sole live provider (the ``ai_provider`` config field was
    dropped in the rebuild), so this is a constant — kept as a helper for symmetry with the health DTO."""
    return ProviderName.anthropic


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """Liveness + AI backend reachability. Never 500s; degrades gracefully."""
    try:
        provider = get_provider()
        info = await provider.health()
        status = "ok" if info.available else "degraded"
        return HealthOut(status=status, provider=info)
    except Exception as exc:  # noqa: BLE001 - health must never raise
        cfg = effective()
        model = cfg.anthropic_model
        info = ProviderInfo(
            active=_provider_name(),
            model=model,
            available=False,
            detail=str(exc),
        )
        return HealthOut(status="degraded", provider=info)


@router.get("/provider", response_model=ProviderInfo)
async def provider() -> ProviderInfo:
    """Report the active AI provider and its reachability."""
    try:
        return await get_provider().health()
    except Exception as exc:  # noqa: BLE001 - must never raise
        cfg = effective()
        model = cfg.anthropic_model
        return ProviderInfo(
            active=_provider_name(),
            model=model,
            available=False,
            detail=str(exc),
        )
