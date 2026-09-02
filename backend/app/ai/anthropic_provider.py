"""Anthropic (hosted Claude API) provider — reachability for the admin meta endpoints.

The review engine runs through the gateway/adapter layer (``app.ai.gateway``); this provider now backs
ONLY the admin ``/api/health`` + ``/api/provider`` info endpoints. With no key configured it stays
importable and reports ``available: false``, so the app boots and runs offline without credentials.
"""

from __future__ import annotations

import logging

from ..schemas import ProviderInfo, ProviderName
from ..settings_store import EffectiveConfig, effective
from .base import AIProvider

logger = logging.getLogger(__name__)


class AnthropicProvider(AIProvider):
    """Reports Anthropic backend reachability for the meta endpoints."""

    name = "anthropic"

    def __init__(self, cfg: EffectiveConfig | None = None) -> None:
        cfg = cfg or effective()
        self.model = cfg.anthropic_model
        self._api_key = cfg.anthropic_api_key
        self._max_tokens = cfg.anthropic_max_tokens
        self._client = None
        if self._api_key:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)

    async def health(self) -> ProviderInfo:
        available = bool(self._api_key)
        if available:
            detail = f"Anthropic API key configured; model '{self.model}'."
        else:
            detail = (
                "Anthropic API key not set. Configure ANTHROPIC_API_KEY to enable "
                "the hosted provider."
            )
        return ProviderInfo(
            active=ProviderName.anthropic,
            model=self.model,
            available=available,
            detail=detail,
            models=[self.model],
        )

    async def aclose(self) -> None:
        client = self._client
        if client is None:
            return
        # AsyncAnthropic's teardown coroutine is `close` (it has no `aclose`); accept either name so
        # the underlying httpx pool is actually released (a per-Test-mode-review leak otherwise).
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if callable(closer):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                logger.debug("Error closing Anthropic client: %s", exc)
