"""Telemetry: structured logging, correlation ids, and OTel export.

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
from .otel import configure_tracing, get_tracer

__all__ = [
    "CORRELATION_ID_HEADER",
    "CorrelationIdMiddleware",
    "bind_correlation_id",
    "configure_logging",
    "configure_tracing",
    "correlation_id_var",
    "get_correlation_id",
    "get_logger",
    "get_tracer",
]
