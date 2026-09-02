"""structlog pipeline: the correlation id is bound into every log line."""

from __future__ import annotations

import io
import json

from app.config import Settings
from app.telemetry.logging import (
    bind_correlation_id,
    configure_logging,
    correlation_id_var,
    get_logger,
)


def _last_json_line(buf: io.StringIO) -> dict[str, object]:
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_correlation_id_bound_in_logs() -> None:
    buf = io.StringIO()
    configure_logging(Settings(_env_file=None, log_format="json"), stream=buf)

    token = bind_correlation_id("corr-xyz")
    try:
        get_logger("test").info("hello", foo="bar")
    finally:
        correlation_id_var.reset(token)

    payload = _last_json_line(buf)
    assert payload["correlation_id"] == "corr-xyz"
    assert payload["event"] == "hello"
    assert payload["foo"] == "bar"
    assert payload["level"] == "info"


def test_default_correlation_id_is_placeholder() -> None:
    buf = io.StringIO()
    configure_logging(Settings(_env_file=None, log_format="json"), stream=buf)

    get_logger("test").info("no-context")

    payload = _last_json_line(buf)
    assert payload["correlation_id"] == "-"
