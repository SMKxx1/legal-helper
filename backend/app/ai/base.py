"""AI provider interface.

A provider reports the backend's reachability (``health``) and releases its network client
(``aclose``). The review engine itself runs through the gateway/adapter layer (``app.ai.gateway``);
this interface now backs only the admin ``/api/health`` + ``/api/provider`` info endpoints.
"""

from __future__ import annotations

import abc

from ..schemas import ProviderInfo


class AIProvider(abc.ABC):
    """Common interface for all AI backends."""

    #: short identifier, e.g. "anthropic"
    name: str = "base"
    #: model identifier currently in use
    model: str = ""

    @abc.abstractmethod
    async def health(self) -> ProviderInfo:
        """Report whether the backend is reachable and which models exist."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any network clients. Default: no-op."""
        return None
