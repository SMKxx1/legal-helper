"""Structured logging and per-request correlation IDs.

structlog renders human-friendly console lines in dev (``LOG_FORMAT=console``) and one JSON object
per line in prod (``LOG_FORMAT=json``) for aggregation. Every log line carries a ``correlation_id``,
read from a contextvar that :class:`CorrelationIdMiddleware` sets per request — so all lines for one
request share an id you can grep/aggregate by, and lines emitted outside a request read ``"-"``.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import time
from typing import Any, TextIO
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, Processor, WrappedLogger

from ..config import Settings

#: HTTP header used to receive and echo the request correlation id.
CORRELATION_ID_HEADER = "X-Correlation-Id"

#: Per-request correlation id. ``"-"`` outside a request (e.g. at boot or in the worker).
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def get_correlation_id() -> str:
    """The correlation id bound to the current context, or ``"-"`` if none."""
    return correlation_id_var.get()


def bind_correlation_id(cid: str) -> contextvars.Token[str]:
    """Bind ``cid`` to the current context; pass the returned token to ``correlation_id_var.reset``."""
    return correlation_id_var.set(cid)


def _add_correlation_id(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: stamp every event with the current correlation id."""
    event_dict.setdefault("correlation_id", correlation_id_var.get())
    return event_dict


def _level_number(name: str) -> int:
    return getattr(logging, name.upper(), logging.INFO)


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Install the structlog pipeline (idempotent — safe to call again).

    ``stream`` defaults to stdout; tests pass a buffer to capture output deterministically. The
    console renderer only colorizes when writing to the default stream (a captured buffer stays clean).
    """
    processors: list[Processor] = [
        structlog.processors.add_log_level,
        _add_correlation_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=stream is None))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            _level_number(settings.log_level)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger. Typed ``Any`` — structlog's bound logger accepts arbitrary kwargs."""
    return structlog.get_logger(name)


class CorrelationIdMiddleware:
    """Mint/propagate a correlation id and emit ``request.start`` / ``request.finish`` logs.

    Reads an inbound ``X-Correlation-Id`` (so an edge or caller can thread its own id through) or
    mints a fresh one, binds it to :data:`correlation_id_var` for the request's lifetime so every log
    line carries it, and echoes it on the response. ``request.finish`` records the status and wall
    duration in milliseconds.

    Implemented as pure ASGI (not Starlette ``BaseHTTPMiddleware``) so the contextvar it sets is
    visible to the route handlers running downstream — ``BaseHTTPMiddleware`` runs the endpoint in a
    detached context where that binding would be lost.
    """

    def __init__(self, app: ASGIApp, header_name: str = CORRELATION_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(self.header_name)
        cid = inbound or uuid4().hex
        token = correlation_id_var.set(cid)
        log = get_logger("nda.request")
        method = scope.get("method", "-")
        path = scope.get("path", "-")
        start = time.perf_counter()
        status_code = 500  # assume failure until a response.start says otherwise

        log.info("request.start", method=method, path=path)

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[self.header_name] = cid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            log.info(
                "request.finish",
                method=method,
                path=path,
                status=status_code,
                duration_ms=duration_ms,
            )
            correlation_id_var.reset(token)
