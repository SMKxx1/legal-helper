"""Provider factory.

Selects the active `AIProvider` from the *effective* runtime configuration
(env defaults overridden by user settings) and caches a single instance. The
cache is keyed by the config signature, so changing the provider, model, output
format, or API key from the Settings page transparently rebuilds the provider on
next use. Tests can clear the cache with `reset_provider()`.
"""

from __future__ import annotations

import asyncio
import logging

from ..settings_store import EffectiveConfig, effective
from .base import AIProvider

logger = logging.getLogger(__name__)

# Module-level singleton: (signature, instance).
_provider: tuple[tuple, AIProvider] | None = None

# Strong refs to in-flight provider-teardown tasks (asyncio only weak-refs them).
_close_tasks: set[asyncio.Task] = set()


def _build(cfg: EffectiveConfig) -> AIProvider:
    from .anthropic_provider import AnthropicProvider

    return AnthropicProvider(cfg)


def get_provider() -> AIProvider:
    """Return the provider for the current effective config (cached by signature)."""
    global _provider
    cfg = effective()
    sig = cfg.signature
    if _provider is None or _provider[0] != sig:
        _close_quietly(_provider[1] if _provider else None)
        _provider = (sig, _build(cfg))
    return _provider[1]


def reset_provider() -> None:
    """Clear the cached provider (after a settings change, or for tests)."""
    global _provider
    _close_quietly(_provider[1] if _provider else None)
    _provider = None


def _close_quietly(provider: AIProvider | None) -> None:
    """Best-effort aclose of a superseded provider's network client."""
    if provider is None:
        return
    aclose = getattr(provider, "aclose", None)
    if not callable(aclose):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop and loop.is_running():
            # Retain a strong reference: asyncio only weak-refs the task, so without this the
            # superseded provider's httpx client may be GC'd before aclose() runs, leaking its
            # connection pool on rapid settings changes.
            task = loop.create_task(aclose())
            _close_tasks.add(task)
            task.add_done_callback(_close_tasks.discard)
        else:
            asyncio.run(aclose())
    except Exception:  # pragma: no cover - cleanup best effort
        logger.debug("Failed to close superseded provider", exc_info=True)
