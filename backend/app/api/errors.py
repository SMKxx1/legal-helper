"""Shared HTTP error envelope.

``EngineError`` renders as ``{"error": {"code", "message", "details"}}`` via
``engine_error_handler`` (registered in main on the app). Kept in its own module so any layer
(routes, auth dependencies) can raise it without importing the route modules — avoiding cycles.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


class EngineError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        # Optional response headers (e.g. Retry-After on a 429) — rendered by the handler.
        self.headers = headers or {}
        super().__init__(message)


def engine_error_handler(_: Request, exc: EngineError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": {"code": exc.code, "message": exc.message, "details": exc.details}
        },
        headers=exc.headers or None,
    )
