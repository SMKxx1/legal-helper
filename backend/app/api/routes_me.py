"""``GET /api/me``, the OpenRouter key endpoints, model preferences, and the ZDR model list
(plan §4.1). The user's key is never returned or logged in plaintext by anything here — every
response carries at most ``key_last4`` + ``key_label``.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from .. import crypto
from ..ai import zdr
from ..auth.deps import get_current_user
from ..config import settings
from ..db import get_db
from ..models import User
from .errors import EngineError

router = APIRouter(prefix="/api", tags=["me"])


def _openrouter_client() -> httpx.AsyncClient:
    """Factory so tests can monkeypatch this to inject an ``httpx.MockTransport``."""
    return httpx.AsyncClient(base_url=settings.openrouter_base_url, timeout=15.0)


def _decrypt_key_or_409(user: User) -> str:
    if not user.openrouter_key_enc:
        raise EngineError(409, "no_openrouter_key", "Add your OpenRouter key first.")
    return crypto.decrypt(user.openrouter_key_enc)


class MeOut(BaseModel):
    username: str
    display_name: str
    role: str
    has_key: bool
    key_last4: str | None
    key_label: str | None
    preferred_model_quick: str | None
    preferred_model_deep: str | None
    default_model_quick: str
    default_model_deep: str


def _me_out(user: User) -> MeOut:
    return MeOut(
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        has_key=bool(user.openrouter_key_enc),
        key_last4=user.openrouter_key_last4,
        key_label=user.openrouter_key_label,
        preferred_model_quick=user.preferred_model_quick,
        preferred_model_deep=user.preferred_model_deep,
        default_model_quick=settings.model_quick,
        default_model_deep=settings.model_deep,
    )


@router.get("/me", response_model=MeOut)
def get_me(user: User = Depends(get_current_user)) -> MeOut:
    return _me_out(user)


class KeyIn(BaseModel):
    api_key: str


class KeyOut(BaseModel):
    key_last4: str
    key_label: str | None
    limit_remaining: float | None


@router.put("/me/openrouter-key", response_model=KeyOut)
async def save_openrouter_key(
    body: KeyIn, user: User = Depends(get_current_user), db: DbSession = Depends(get_db)
) -> KeyOut:
    api_key = body.api_key.strip()
    if not api_key:
        raise EngineError(422, "invalid_openrouter_key", "An API key is required.")

    async with _openrouter_client() as client:
        resp = await client.get("/key", headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code != 200:
        raise EngineError(
            422, "invalid_openrouter_key", "OpenRouter rejected this key."
        )

    info = (resp.json() or {}).get("data") or {}
    label = info.get("label")
    limit, usage = info.get("limit"), info.get("usage")
    limit_remaining = (
        (float(limit) - float(usage))
        if isinstance(limit, int | float) and isinstance(usage, int | float)
        else None
    )

    # Plaintext touches memory only for this request — encrypted immediately, never logged.
    user.openrouter_key_enc = crypto.encrypt(api_key)
    user.openrouter_key_last4 = api_key[-4:]
    user.openrouter_key_label = str(label) if label else None
    db.commit()
    return KeyOut(
        key_last4=user.openrouter_key_last4,
        key_label=user.openrouter_key_label,
        limit_remaining=limit_remaining,
    )


@router.delete("/me/openrouter-key", status_code=204)
def delete_openrouter_key(
    user: User = Depends(get_current_user), db: DbSession = Depends(get_db)
) -> None:
    user.openrouter_key_enc = None
    user.openrouter_key_last4 = None
    user.openrouter_key_label = None
    db.commit()


class ModelsIn(BaseModel):
    quick: str | None = None
    deep: str | None = None


@router.put("/me/models", response_model=MeOut)
async def save_model_preferences(
    body: ModelsIn,
    user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
) -> MeOut:
    if body.quick is not None or body.deep is not None:
        api_key = _decrypt_key_or_409(user)
        if body.quick is not None:
            # blank -> clear the override, falling back to the env default (MODEL_QUICK)
            if body.quick == "":
                user.preferred_model_quick = None
            elif await zdr.is_zdr_model(body.quick, api_key):
                user.preferred_model_quick = body.quick
            else:
                raise EngineError(
                    422, "model_not_zdr", f"{body.quick!r} is not a ZDR-listed model."
                )
        if body.deep is not None:
            if body.deep == "":
                user.preferred_model_deep = None
            elif await zdr.is_zdr_model(body.deep, api_key):
                user.preferred_model_deep = body.deep
            else:
                raise EngineError(
                    422, "model_not_zdr", f"{body.deep!r} is not a ZDR-listed model."
                )
        db.commit()
    return _me_out(user)


class ZdrModelOut(BaseModel):
    id: str
    name: str
    provider: str
    context_length: int | None
    prompt_usd_per_m: float | None
    completion_usd_per_m: float | None


@router.get("/models/zdr", response_model=list[ZdrModelOut])
async def get_zdr_models(user: User = Depends(get_current_user)) -> list[ZdrModelOut]:
    api_key = _decrypt_key_or_409(user)
    models = await zdr.list_zdr_models(api_key)
    return [
        ZdrModelOut(
            id=m.id,
            name=m.name,
            provider=m.provider,
            context_length=m.context_length,
            prompt_usd_per_m=m.prompt_usd_per_m,
            completion_usd_per_m=m.completion_usd_per_m,
        )
        for m in models
    ]
