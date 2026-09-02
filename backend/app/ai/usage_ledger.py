"""Request-scoped token-usage ledger (contextvars) — per-review attribution.

Gateways are lru-cached and SHARED across concurrent reviews (routes_v1._build_gateways),
so their process-wide monotonic counters cannot attribute tokens to a single review:
under concurrency, review A's entry/exit counter delta absorbs review B's calls. This
module is the request-scoped alternative: ``track_usage()`` installs a ``UsageLedger``
in a ContextVar and ``Gateway.run`` adds each REAL provider call's usage to the current
ledger (cache hits and fallback results return zeroed usage and add nothing). The
process-wide gateway counters are untouched — they remain lifetime diagnostics.

ThreadPoolExecutor workers start with an EMPTY context, so a bare ``submit``/``map``
would lose the ledger — wrap the callable with ``ctx_copy`` at every fan-out site:
``ex.submit(ctx_copy(work), arg)`` / ``ex.map(ctx_copy(work), items)``.

Nesting: ``track_usage`` is a plain set/reset — an inner ``track_usage`` SHADOWS the
outer ledger for its duration, so usage inside the inner block is NOT counted into the
outer one. A caller that needs both must fold the inner ledger's totals in explicitly
(the way run_review folds its caller's pre-run router usage in via ``prior_*``).
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, ParamSpec, TypeVar

if TYPE_CHECKING:  # runtime import would be circular — gateway.py imports this module
    from app.ai.gateway import Usage

P = ParamSpec("P")
R = TypeVar("R")


class UsageLedger:
    """Thread-safe token accumulators for ONE tracked scope (typically one review)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0

    def add(
        self,
        usage: Usage | None = None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Accumulate one call's usage — a gateway ``Usage`` dataclass and/or explicit ints."""
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


_LEDGER: contextvars.ContextVar[UsageLedger | None] = contextvars.ContextVar(
    "nda_usage_ledger", default=None
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

    The propagation helper for thread pools: ``ex.submit(ctx_copy(work), arg)`` /
    ``ex.map(ctx_copy(work), items)`` carry the caller's ledger into worker threads.
    Each invocation runs in its OWN copy of the captured context (``Context.run`` is
    not concurrency-safe on a single Context object), but the ``UsageLedger`` inside
    is shared by reference — worker usage lands in the caller's ledger.
    """
    ctx = contextvars.copy_context()

    def _run(*args: P.args, **kwargs: P.kwargs) -> R:
        return ctx.copy().run(fn, *args, **kwargs)

    return _run
