"""Runtime settings: read effective config, apply user edits."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import pricing
from ..ai.factory import reset_provider
from ..schemas import (
    ModelRate,
    PricingOut,
    PricingUpdate,
    ProviderName,
    SettingsOut,
    SettingsUpdate,
)
from ..settings_store import effective, set_overrides

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _mask(key: str) -> str:
    if not key:
        return ""
    tail = key[-4:] if len(key) >= 4 else key
    return f"…{tail}"


def _to_out() -> SettingsOut:
    cfg = effective()
    return SettingsOut(
        # Anthropic is the sole live provider (the ``ai_provider`` config field was dropped in the
        # rebuild) — report it as a constant rather than reading a config value that no longer exists.
        ai_provider=ProviderName.anthropic,
        anthropic_model=cfg.anthropic_model,
        anthropic_key_set=bool(cfg.anthropic_api_key),
        anthropic_key_hint=_mask(cfg.anthropic_api_key),
        pdf_extract_strategy=cfg.pdf_extract_strategy,
    )


@router.get("", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    return _to_out()


@router.put("", response_model=SettingsOut)
async def update_settings(payload: SettingsUpdate) -> SettingsOut:
    updates: dict[str, str | None] = {}

    if payload.anthropic_model is not None:
        updates["anthropic_model"] = payload.anthropic_model.strip()
    if payload.anthropic_api_key is not None:
        # Empty string explicitly clears the stored key.
        updates["anthropic_api_key"] = payload.anthropic_api_key.strip()
    if payload.pdf_extract_strategy is not None:
        strat = payload.pdf_extract_strategy.strip().lower()
        if strat not in {"local"}:
            raise HTTPException(400, "pdf_extract_strategy must be 'local'")
        updates["pdf_extract_strategy"] = strat

    set_overrides(updates)
    reset_provider()  # next get_provider() picks up the change
    return _to_out()


# --------------------------------------------------------------------------- #
# Anthropic pricing (used to compute review/test cost). Editable so the rates
# can be corrected without a code change. See app/pricing.py.
# --------------------------------------------------------------------------- #
def _pricing_out(table: dict) -> PricingOut:
    return PricingOut(
        pricing={k: ModelRate(**v) for k, v in table.items()},
        defaults={k: ModelRate(**v) for k, v in pricing.DEFAULT_PRICING.items()},
    )


@router.get("/pricing", response_model=PricingOut)
async def get_pricing() -> PricingOut:
    return _pricing_out(pricing.load_pricing())


@router.put("/pricing", response_model=PricingOut)
async def update_pricing(payload: PricingUpdate) -> PricingOut:
    table = {
        k: {"input": v.input, "output": v.output} for k, v in payload.pricing.items()
    }
    merged = pricing.save_pricing(table)
    return _pricing_out(merged)
