"""Request-scoped usage ledger (contextvars) — per-review attribution (plan §4.2, §4.3).

Gateways are constructed per (agent, model) and called from a ``ThreadPoolExecutor`` fan-out
(classifier -> parallel reviewer/coverage), so a shared, thread-safe, request-scoped ledger is how
one review's usage is attributed correctly under concurrency: ``track_usage()`` installs a
:class:`UsageLedger` in a ``ContextVar`` for the duration of one review; :func:`ctx_copy` carries
that context into worker threads (``ThreadPoolExecutor`` workers otherwise start with an EMPTY
context and the ledger would be lost).

Two things accumulate here, both fed by ``agents.base.run`` (NOT by ``Gateway.run`` — the ledger
doesn't know which *agent* made a call, only the gateway/adapter do):

* the aggregate token counters (``add``) — used for quick sums;
* one :class:`LlmCallRecord` per gateway call (``add_call``) — agent, model, provider, tokens,
  cost, latency, ok/error — which the orchestrator persists as one ``llm_calls`` row each.

Nesting: ``track_usage`` is a plain set/reset — an inner ``track_usage`` SHADOWS the outer ledger
for its duration (not used by the review pipeline today, but preserved from the original design).
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ParamSpec, TypeVar

if TYPE_CHECKING:  # runtime import would be circular — gateway.py imports this module
    from app.ai.gateway import Usage

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True)
class LlmCallRecord:
    """One OpenRouter call, ready to persist as one ``llm_calls`` row (``app.models.LlmCall``)."""

    agent: str  # "classifier" | "reviewer" | "coverage"
    model: str
    provider: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    latency_ms: int
    ok: bool
    error: str | None = None


class UsageLedger:
    """Thread-safe accumulators for ONE tracked scope (one review)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.calls: list[LlmCallRecord] = []

    def add(
        self,
        usage: Usage | None = None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Accumulate one call's token usage — a gateway ``Usage`` dataclass and/or explicit ints."""
        if usage is not None:
            input_tokens += usage.input_tokens or 0
            output_tokens += usage.output_tokens or 0
            cache_read_tokens += usage.cache_read_tokens or 0
            cache_write_tokens += usage.cache_write_tokens or 0
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cache_read_tokens += cache_read_tokens
            self.cache_write_tokens += cache_write_tokens

    def add_call(self, record: LlmCallRecord) -> None:
        """Record one agent's gateway call (success or failure) for later persistence."""
        with self._lock:
            self.calls.append(record)

    @property
    def cost_usd(self) -> float:
        with self._lock:
            return round(sum(c.cost_usd for c in self.calls), 6)


_LEDGER: contextvars.ContextVar[UsageLedger | None] = contextvars.ContextVar(
    "usage_ledger", default=None
)


def current_ledger() -> UsageLedger | None:
    """The ledger installed by the innermost active ``track_usage()``, or None."""
    return _LEDGER.get()


@contextmanager
def track_usage() -> Iterator[UsageLedger]:
    """Install a fresh ledger for the duration of the block (see module docstring on nesting)."""
    ledger = UsageLedger()
    token = _LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER.reset(token)


def ctx_copy(fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap ``fn`` to run inside a copy of the context captured HERE (at wrap time).

    The propagation helper for thread pools: ``ex.submit(ctx_copy(work), arg)`` carries the
    caller's ledger into worker threads. Each invocation runs in its OWN copy of the captured
    context, but the ``UsageLedger`` inside is shared by reference — worker usage lands in the
    caller's ledger.
    """
    ctx = contextvars.copy_context()

    def _run(*args: P.args, **kwargs: P.kwargs) -> R:
        return ctx.copy().run(fn, *args, **kwargs)

    return _run
