"""Telemetry: structured logging and per-request correlation ids.

Public surface re-exported for ``from app.telemetry import ...``.
"""

from __future__ import annotations

from .logging import (
    CORRELATION_ID_HEADER,
    CorrelationIdMiddleware,
    bind_correlation_id,
    configure_logging,
    correlation_id_var,
    get_correlation_id,
    get_logger,
)

__all__ = [
    "CORRELATION_ID_HEADER",
    "CorrelationIdMiddleware",
    "bind_correlation_id",
    "configure_logging",
    "correlation_id_var",
    "get_correlation_id",
    "get_logger",
]
