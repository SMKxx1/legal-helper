"""Capability registry.

Every integration is a *capability* with one of three states:

* ``enabled``   — required config is present (and any health probe passed).
* ``disabled``  — required config is missing. This is a normal, expected state: the feature is
  politely off. Missing config NEVER crashes boot and NEVER fails liveness.
* ``unhealthy`` — configured but a runtime failure occurred (e.g. the database is unreachable).

The registry is evaluated once at boot from :class:`~app.config.Settings`, then mutated at runtime via
:meth:`mark_unhealthy` / :meth:`mark_recovered`. It exposes :meth:`report` (detailed — the public
``GET /api/status`` in a later phase) and :meth:`healthy` (a shallow boolean for ``/healthz``).

Fail-soft vs fail-closed: a disabled/unhealthy *capability* degrades a feature but keeps the process
serving. Only a capability explicitly marked ``critical`` can pull ``/healthz`` to 503 — today that is
only ``database``.

Legal Helper has three capabilities:

* ``database``           — critical. The primary Postgres/SQLite datastore.
* ``bucket``              — the Railway bucket storing each review's original .docx (Phase 4).
* ``openrouter_zdr_list`` — validation of OpenRouter's Zero-Data-Retention model list (Phase 2).

``bucket`` reports ``disabled`` until its S3 settings are present — an expected state, not a
fault: reviews still run, the document simply is not archived.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from .config import Settings

#: A probe returns ``None`` when healthy, or a human-readable reason string when unhealthy. It must
#: not raise for expected failures; a raised exception is caught and treated as an unhealthy reason.
HealthProbe = Callable[[Settings], Awaitable[str | None]]


class CapabilityState(str, Enum):
    """The three states a capability can hold. ``str`` base keeps values stable for serialization."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class Capability:
    """Static declaration of an integration.

    ``required_keys`` are :class:`~app.config.Settings` field names; all must be present for the
    capability to be enabled (an empty tuple means "no config required" -> always enabled).
    ``critical`` capabilities gate liveness; ``probe`` is an optional boot-time health check.
    """

    name: str
    required_keys: tuple[str, ...]
    summary: str
    critical: bool = False
    probe: HealthProbe | None = None


@dataclass
class CapabilityStatus:
    """The current, mutable state of a capability."""

    name: str
    state: CapabilityState
    reason: str
    summary: str
    critical: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state.value,
            "reason": self.reason,
            "summary": self.summary,
            "critical": self.critical,
        }


class CapabilityRegistry:
    """Holds capability specs and their evaluated status; the single boot-time source of truth."""

    def __init__(self, capabilities: Sequence[Capability], settings: Settings) -> None:
        self._settings = settings
        self._caps: dict[str, Capability] = {}
        self._status: dict[str, CapabilityStatus] = {}
        for cap in capabilities:
            if cap.name in self._caps:
                raise ValueError(f"duplicate capability: {cap.name!r}")
            self._caps[cap.name] = cap
            self._status[cap.name] = self._evaluate_config(cap)

    def _evaluate_config(self, cap: Capability) -> CapabilityStatus:
        missing = self._settings.missing_config(*cap.required_keys)
        if missing:
            return CapabilityStatus(
                cap.name,
                CapabilityState.DISABLED,
                f"missing config: {', '.join(missing)}",
                cap.summary,
                cap.critical,
            )
        return CapabilityStatus(
            cap.name,
            CapabilityState.ENABLED,
            "config present",
            cap.summary,
            cap.critical,
        )

    async def run_probes(self) -> None:
        """Run each enabled capability's optional health probe, transitioning failures to unhealthy.

        A probe that raises is caught here — a health check must never break boot.
        """
        for name, cap in self._caps.items():
            if (
                cap.probe is None
                or self._status[name].state is not CapabilityState.ENABLED
            ):
                continue
            try:
                reason = await cap.probe(self._settings)
            except Exception as exc:  # noqa: BLE001 - a probe must never break boot
                self.mark_unhealthy(name, f"probe raised: {exc!r}")
                continue
            if reason:
                self.mark_unhealthy(name, reason)

    def mark_unhealthy(self, name: str, reason: str) -> None:
        """Runtime transition: a configured capability hit a failure. Raises on an unknown name (a
        programming error, not a config one)."""
        status = self._status[name]
        status.state = CapabilityState.UNHEALTHY
        status.reason = reason

    def mark_recovered(self, name: str) -> None:
        """Runtime transition: re-derive state from current config. A recovered capability returns to
        ``enabled`` if its config is still present, or ``disabled`` if it was since removed."""
        self._status[name] = self._evaluate_config(self._caps[name])

    def healthy(self) -> bool:
        """Shallow liveness for ``/healthz``: True unless a *critical* capability is unhealthy.

        Disabled and non-critical-unhealthy capabilities never affect liveness (capabilities fail
        soft). No detail leaks here — that lives in :meth:`report`.
        """
        return not any(
            s.critical and s.state is CapabilityState.UNHEALTHY
            for s in self._status.values()
        )

    def report(self) -> list[dict[str, object]]:
        """Detailed per-capability states — for ``GET /api/status`` (Phase 3), NOT ``/healthz``."""
        return [self._status[name].as_dict() for name in self._caps]

    def state(self, name: str) -> CapabilityState:
        return self._status[name].state

    def get(self, name: str) -> CapabilityStatus:
        return self._status[name]

    def names(self) -> list[str]:
        return list(self._caps)


DATABASE = "database"
BUCKET = "bucket"
OPENROUTER_ZDR_LIST = "openrouter_zdr_list"


async def _database_probe(s: Settings) -> str | None:
    """In prod, ``APP_SECRET_KEY`` is required (it encrypts every user's OpenRouter key at rest —
    ``app.crypto``). Its absence there fails this probe, which pulls the critical ``database``
    capability to ``unhealthy`` and ``/healthz`` to 503. In dev a missing key is fine — a fixed
    dev-only key is derived instead (loudly logged, never used once a real key is set)."""
    if s.app_env != "dev" and not s.app_secret_key.strip():
        return "APP_SECRET_KEY is required outside APP_ENV=dev (encrypts user OpenRouter keys)"
    return None


def default_capabilities() -> list[Capability]:
    """The three capabilities registered at boot."""
    return [
        # Critical: the primary datastore. No config is required to boot (SQLite works out of the
        # box); the probe above fails this when APP_SECRET_KEY is missing outside dev.
        Capability(
            name=DATABASE,
            required_keys=(),
            summary="Primary Postgres/SQLite datastore.",
            critical=True,
            probe=_database_probe,
        ),
        # A Railway bucket storing each review's original .docx (Phase 4). Disabled until all four
        # S3-compatible fields are configured.
        Capability(
            name=BUCKET,
            required_keys=(
                "s3_endpoint",
                "s3_bucket",
                "s3_access_key_id",
                "s3_secret_access_key",
            ),
            summary="Railway bucket storing each review's original .docx.",
            critical=False,
        ),
        # Validation of OpenRouter's Zero-Data-Retention model list (Phase 2 wires the real check
        # against openrouter_zdr_list_ready; it defaults False, so this reports disabled today).
        Capability(
            name=OPENROUTER_ZDR_LIST,
            required_keys=("openrouter_zdr_list_ready",),
            summary="OpenRouter Zero-Data-Retention model list validation.",
            critical=False,
        ),
    ]


def build_registry(settings: Settings) -> CapabilityRegistry:
    """Construct the registry from the default capability set and the given settings."""
    return CapabilityRegistry(default_capabilities(), settings)
