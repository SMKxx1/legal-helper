"""OpenTelemetry tracing wired to Azure Application Insights — activated only when configured.

The OTel *API* is always available, so :func:`get_tracer` is safe at import and boot: without an
exporter it returns the API's no-op tracer, so spans cost nothing. :func:`configure_tracing`
activates the Azure Monitor exporter and is the only place that touches the azure package — behind an
import guard, so a missing/broken exporter dependency degrades the ``telemetry_export`` capability to
``unhealthy`` rather than breaking boot. A missing connection string is a clean no-op (the capability
stays ``disabled``). This function never raises.
"""

from __future__ import annotations

from opentelemetry import trace

from ..capabilities import TELEMETRY_EXPORT, CapabilityRegistry
from ..config import Settings
from .logging import get_logger


def configure_tracing(settings: Settings, registry: CapabilityRegistry) -> bool:
    """Activate Azure Monitor OTel export iff the connection string is set and the exporter loads.

    Returns True when export is live. On a missing connection string: no-op, returns False. On an
    import/init failure: transitions ``telemetry_export`` to ``unhealthy`` and returns False. Never
    raises — boot proceeds regardless.
    """
    log = get_logger("nda.telemetry")
    conn = settings.applicationinsights_connection_string.strip()
    if not conn:
        log.debug("telemetry.export_disabled", reason="no connection string")
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except Exception as exc:  # noqa: BLE001 - a missing/broken exporter must not break boot
        _mark_unhealthy(registry, f"exporter import failed: {exc!r}")
        log.warning("telemetry.export_unavailable", reason="import", error=repr(exc))
        return False

    try:
        configure_azure_monitor(connection_string=conn)
    except Exception as exc:  # noqa: BLE001 - exporter init must not break boot
        _mark_unhealthy(registry, f"exporter init failed: {exc!r}")
        log.warning("telemetry.export_unavailable", reason="init", error=repr(exc))
        return False

    log.info("telemetry.export_enabled")
    return True


def _mark_unhealthy(registry: CapabilityRegistry, reason: str) -> None:
    # Guarded so this stays a no-crash path even if the capability was not registered.
    if TELEMETRY_EXPORT in registry.names():
        registry.mark_unhealthy(TELEMETRY_EXPORT, reason)


def get_tracer(name: str) -> trace.Tracer:
    """A tracer that emits spans when export is active and is a silent no-op otherwise. Safe to call
    at import time and before :func:`configure_tracing`."""
    return trace.get_tracer(name)
