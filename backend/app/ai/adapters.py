"""Live provider adapter — the only code that calls the Anthropic (Claude) SDK.

The adapter implements ``gateway.ProviderAdapter.complete``: builds the request
with the (unit-tested) builder, calls the SDK, normalizes the response to
``RawResult``, computes cost via ``pricing.cost_for``, and classifies failures
into ``RetryableProviderError`` (timeout/429/5xx) vs ``TerminalProviderError``
(auth/400/refusal) so the gateway's retry/fallback policy works (P1-3).

Verified against anthropic SDK 0.105.2 (``output_config.format`` supported).
"""

from __future__ import annotations

from app.ai.gateway import (
    GatewayRequest,
    RawResult,
    RetryableProviderError,
    SchemaValidationError,
    TerminalProviderError,
    Usage,
    build_anthropic_request,
)
from app.pricing import cost_for


def _anthropic_supports_effort(model: str) -> bool:
    # effort is rejected on Haiku 4.5 and Sonnet 4.5/older; supported on Opus 4.6+, Sonnet 4.6.
    m = model.lower()
    return "haiku" not in m and "sonnet-4-5" not in m


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, cache_ttl: str = "5m") -> None:
        import anthropic

        self.model_id = model
        # Prompt-cache TTL for the stable prefix: "5m" (provider default — request bytes
        # unchanged) or "1h" (extended; the SAME value drives the 2x write-cost accounting
        # below, so cost_usd stays honest either way).
        self.cache_ttl = cache_ttl if cache_ttl == "1h" else "5m"
        self._client = anthropic.Anthropic(api_key=api_key)
        self._anthropic = anthropic

    def complete(self, req: GatewayRequest) -> RawResult:
        a = self._anthropic
        kw = build_anthropic_request(
            req,
            self.model_id,
            include_effort=_anthropic_supports_effort(self.model_id),
            cache_ttl=self.cache_ttl,
        )
        try:
            r = self._client.messages.create(**kw)
        except (
            a.RateLimitError,
            a.APITimeoutError,
            a.InternalServerError,
            a.APIConnectionError,
        ) as e:
            raise RetryableProviderError(str(e)) from e
        except (a.BadRequestError, a.AuthenticationError, a.PermissionDeniedError) as e:
            raise TerminalProviderError(str(e)) from e
        except a.APIStatusError as e:  # any other status
            raise (
                RetryableProviderError
                if e.status_code >= 500
                else TerminalProviderError
            )(str(e)) from e

        if getattr(r, "stop_reason", None) == "refusal":
            raise TerminalProviderError("anthropic refusal")
        text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
        if not text:
            raise SchemaValidationError("anthropic returned no text block")
        u = r.usage
        # Anthropic's input_tokens already EXCLUDES cached reads (reported separately), so
        # pass the cache counts through to be billed at the cached/write multipliers.
        _cr = getattr(u, "cache_read_input_tokens", 0) or 0
        _cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=_cr,
            cache_write_tokens=_cw,
            cost_usd=cost_for(
                self.model_id,
                u.input_tokens,
                u.output_tokens,
                cache_read_tokens=_cr,
                cache_write_tokens=_cw,
                cache_write_ttl=self.cache_ttl,  # a 1h write bills 2x, not 1.25x
            ),
        )
        return RawResult(text=text, usage=usage, model_version=r.model)
