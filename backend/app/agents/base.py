"""The shape every agent shares (plan §4.2) — one small dataclass plus one ``run()`` function.

Each of ``classifier.py``/``reviewer.py``/``coverage.py`` builds an :class:`Agent`, a task string,
and a list of stable prompt blocks, then calls :func:`run`. ``run`` owns everything generic: the
gateway call, latency measurement, and recording one :class:`~app.ai.ledger.LlmCallRecord` (success
or failure) into the request-scoped usage ledger, if one is active — the orchestrator reads those
records back after the fan-out to persist ``llm_calls`` rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.ai.gateway import Gateway, GatewayRequest, ProviderError, Result, error_code_for
from app.ai.ledger import LlmCallRecord, current_ledger


@dataclass(frozen=True)
class Agent:
    name: str  # "classifier" | "reviewer" | "coverage"
    system: str  # the prompt
    schema: dict  # JSON schema for response_format (portable subset, asserted at import)
    effort: str = "medium"  # "min" | "low" | "medium" | "high" | "max"
    max_tokens: int = 4096


def run(
    agent: Agent,
    gateway: Gateway,
    task: str,
    stable_blocks: list[str],
    *,
    coerce_defaults: dict | None = None,
    cache_key_parts: tuple = (),
) -> Result:
    """Call ``agent`` through ``gateway`` and return the parsed :class:`~app.ai.gateway.Result`.

    Records one :class:`~app.ai.ledger.LlmCallRecord` into the active usage ledger (if any) either
    way — success or a raised :class:`~app.ai.gateway.ProviderError` — then re-raises on failure.
    The orchestrator decides fail-soft vs fail-closed per agent; this function only reports.
    """
    req = GatewayRequest(
        role=agent.name,
        schema=agent.schema,
        system=agent.system,
        task=task,
        stable_blocks=stable_blocks,
        effort=agent.effort,
        max_tokens=agent.max_tokens,
        cache_key_parts=cache_key_parts,
        coerce_defaults=coerce_defaults or {},
    )
    started = time.perf_counter()
    ledger = current_ledger()
    try:
        result = gateway.run(req)
    except ProviderError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        if ledger is not None:
            ledger.add_call(
                LlmCallRecord(
                    agent=agent.name,
                    model=gateway.adapter.model_id,
                    provider=gateway.adapter.name,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cached_tokens=0,
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                    ok=False,
                    error=error_code_for(exc),
                )
            )
        raise
    latency_ms = int((time.perf_counter() - started) * 1000)
    if ledger is not None:
        ledger.add_call(
            LlmCallRecord(
                agent=agent.name,
                model=result.model_version,
                provider=result.provider,
                prompt_tokens=result.usage.input_tokens,
                completion_tokens=result.usage.output_tokens,
                cached_tokens=result.usage.cache_read_tokens,
                cost_usd=result.usage.cost_usd or 0.0,
                latency_ms=latency_ms,
                ok=True,
                error=None,
            )
        )
    return result
