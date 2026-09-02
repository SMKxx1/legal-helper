"""AI provider layer: the ZDR-pinned OpenRouter gateway.

The engine's whole model-call surface goes through ``gateway.py`` (retry ladder, circuit breaker)
and ``openrouter.py`` (the ZDR-pinned adapter, taking the user's own API key per call — see plan §1).
There is no provider-selection plane here: OpenRouter is the only provider.
"""
