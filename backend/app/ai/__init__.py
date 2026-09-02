"""Pluggable AI provider layer.

TWO provider abstractions coexist here — know which is which:
  • ``gateway.py`` + ``adapters.py`` — the LIVE review path. Every model call in the engine goes here.
  • ``base.py`` + ``factory.py`` + ``anthropic_provider.py`` — reachability ONLY for the admin meta
    endpoints (``/api/health``, ``/api/provider``). The active provider is resolved via
    ``get_provider()`` and callers depend only on the ``AIProvider`` interface.

So: to follow how a review reaches Claude, read ``gateway.py``; the rest just backs the health probes.
"""

from .base import AIProvider
from .factory import get_provider

__all__ = ["AIProvider", "get_provider"]
