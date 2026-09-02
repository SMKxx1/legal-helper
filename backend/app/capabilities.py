"""Capability registry (PLAN §3.2).

Every integration is a *capability* with one of three states:

* ``enabled``   — required config is present (and any health probe passed).
* ``disabled``  — required config is missing. This is a normal, expected state: the feature is
  politely off. Missing config NEVER crashes boot and NEVER fails liveness.
* ``unhealthy`` — configured but a runtime failure occurred (e.g. an exporter refused to start).

The registry is evaluated once at boot from :class:`~app.config.Settings`, then mutated at runtime via
:meth:`mark_unhealthy` / :meth:`mark_recovered`. It exposes :meth:`report` (detailed, for the future
admin surface) and :meth:`healthy` (a shallow boolean for the public ``/healthz`` liveness probe).

Fail-soft vs fail-closed (PLAN §6): a disabled/unhealthy *capability* degrades a feature but keeps the
process serving. Only a capability explicitly marked ``critical`` can pull ``/healthz`` to 503 — the
seam a future liveness-critical dependency (e.g. the primary datastore) plugs into.
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
        soft). No detail leaks — that lives in :meth:`report` behind admin auth (PLAN §6).
        """
        return not any(
            s.critical and s.state is CapabilityState.UNHEALTHY
            for s in self._status.values()
        )

    def report(self) -> list[dict[str, object]]:
        """Detailed per-capability states — for the future admin surface, NOT the public ``/healthz``."""
        return [self._status[name].as_dict() for name in self._caps]

    def state(self, name: str) -> CapabilityState:
        return self._status[name].state

    def get(self, name: str) -> CapabilityStatus:
        return self._status[name]

    def names(self) -> list[str]:
        return list(self._caps)


TELEMETRY_EXPORT = "telemetry_export"
LLM_INFERENCE = "llm_inference"
# P2 bot-core channels (PLAN §3.3). Each is fail-soft: missing config disables that channel, it never
# fails liveness. GATES layered on top (dedup, signatures, allowlist, DMARC) fail CLOSED — but those
# live in the bot's transactional code, not here; a capability only answers "is this channel wired?".
SLACK = "slack"
EMAIL_IN = "email_in"
EMAIL_OUT = "email_out"
TALLY = "tally"
DOCUSIGN = "docusign"
# P4 archive/watcher/expiration surfaces (PLAN §3.10). Both fail-soft: missing config disables the
# surface (archive/watcher/expiration-upsert quietly turn off), it never fails liveness.
GOOGLE_DRIVE = "google_drive"
AIRTABLE = "airtable"


async def llm_inference_probe(_settings: Settings) -> str | None:
    """Boot-time probe STUB for the OpenRouter gateway — deliberately no network at boot.

    The full check (PLAN §3.8: each configured model alias resolves under the ZDR routing policy —
    provider {data_collection:'deny', zdr:true, allow_fallbacks:false} finds at least one route per
    alias) requires a live OpenRouter call, so it must not run inside boot's probe pass; it lands
    with the P1 eval gate as an on-demand/admin check wired through this same seam. Until then a
    present key is reported healthy.
    """
    return None


def default_capabilities() -> list[Capability]:
    """The capabilities registered at boot. Grows one row per integration in later phases."""
    return [
        Capability(
            name=TELEMETRY_EXPORT,
            required_keys=("applicationinsights_connection_string",),
            summary="Export traces/logs/metrics to Azure Application Insights.",
            critical=False,
        ),
        # LLM inference via the ZDR-pinned OpenRouter adapter (app.ai.openrouter, PLAN §3.8).
        # Non-critical: a missing key disables the feature (reviews degrade politely), it never
        # fails liveness. The direct-Anthropic fallback is a config concern, not a capability.
        Capability(
            name=LLM_INFERENCE,
            required_keys=("openrouter_api_key",),
            summary="LLM inference via the ZDR-pinned OpenRouter gateway.",
            critical=False,
            probe=llm_inference_probe,
        ),
        # Bot channels (PLAN §3.3). Slack needs both the bot token (Web API) and the signing secret
        # (inbound v0 HMAC verification). Email intake needs the IMAP poller creds; email delivery
        # needs the SMTP creds. All non-critical — a missing channel degrades the bot, never boot.
        Capability(
            name=SLACK,
            required_keys=("slack_bot_token", "slack_signing_secret"),
            summary="Slack intake + interactivity (Bolt): events, threaded replies, buttons/modals.",
            critical=False,
        ),
        Capability(
            name=EMAIL_IN,
            required_keys=("imap_host", "imap_user", "imap_password"),
            summary="Email intake via IMAP polling (worker) — normalized into the same envelope.",
            critical=False,
        ),
        Capability(
            name=EMAIL_OUT,
            required_keys=("smtp_host", "smtp_user", "smtp_password"),
            summary="Email delivery via SMTP: threaded replies + document attachments.",
            critical=False,
        ),
        Capability(
            name=TALLY,
            required_keys=("tally_signing_secret",),
            summary="Tally intake: verified webhook → NDA generation + reply (external form).",
            critical=False,
        ),
        Capability(
            name=DOCUSIGN,
            required_keys=(
                "docusign_account_id",
                "docusign_integration_key",
                "docusign_user_id",
                "docusign_private_key",
            ),
            summary="DocuSign e-signature: envelope create + send (JWT grant).",
            critical=False,
        ),
        # Archive / watcher (PLAN §3.10). Google Drive needs the offline-grant OAuth trio (to mint
        # access tokens) PLUS the destination archive folder id. Non-critical: without it the archive
        # intent + cache-folder watcher disable, they never fail liveness. drive_cache_folder_name is
        # NOT required (it carries a working default).
        Capability(
            name=GOOGLE_DRIVE,
            required_keys=(
                "google_oauth_client_id",
                "google_oauth_client_secret",
                "google_oauth_refresh_token",
                "drive_archive_folder_id",
            ),
            summary="Google Drive archive: signed-NDA cache upload + auto-name watcher into the destination folder.",
            critical=False,
        ),
        # Expiration tracker (PLAN §3.10). Airtable upsert needs the PAT + base id + table; absent =>
        # extraction still runs/logs but the upsert is a no-op. Non-critical.
        Capability(
            name=AIRTABLE,
            required_keys=("airtable_pat", "airtable_base_id", "airtable_table"),
            summary="Airtable expiration tracker: upsert extracted NDA expiration dates (minimal fields).",
            critical=False,
        ),
    ]


def build_registry(settings: Settings) -> CapabilityRegistry:
    """Construct the registry from the default capability set and the given settings."""
    return CapabilityRegistry(default_capabilities(), settings)
