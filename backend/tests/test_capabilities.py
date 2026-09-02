"""Capability registry state machine: enabled/disabled/unhealthy transitions and liveness."""

from __future__ import annotations

import pytest

from app.capabilities import (
    AIRTABLE,
    DOCUSIGN,
    EMAIL_IN,
    EMAIL_OUT,
    GOOGLE_DRIVE,
    LLM_INFERENCE,
    SLACK,
    TALLY,
    TELEMETRY_EXPORT,
    Capability,
    CapabilityRegistry,
    CapabilityState,
    build_registry,
)
from app.config import Settings


def test_telemetry_disabled_without_config() -> None:
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(TELEMETRY_EXPORT) is CapabilityState.DISABLED
    # A soft-disabled capability never affects liveness.
    assert reg.healthy() is True


def test_llm_inference_disabled_without_key() -> None:
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(LLM_INFERENCE) is CapabilityState.DISABLED
    status = reg.get(LLM_INFERENCE)
    assert "OPENROUTER_API_KEY" in status.reason
    # Non-critical for now: a missing key never fails liveness.
    assert status.critical is False
    assert reg.healthy() is True


def test_llm_inference_enabled_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abc")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(LLM_INFERENCE) is CapabilityState.ENABLED


def test_telemetry_enabled_with_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc"
    )
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(TELEMETRY_EXPORT) is CapabilityState.ENABLED


def test_unhealthy_then_recovered_reflects_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc"
    )
    reg = build_registry(Settings(_env_file=None))

    reg.mark_unhealthy(TELEMETRY_EXPORT, "exporter init failed")
    status = reg.get(TELEMETRY_EXPORT)
    assert status.state is CapabilityState.UNHEALTHY
    assert "exporter init failed" in status.reason

    # Config still present -> recovery lands back on ENABLED.
    reg.mark_recovered(TELEMETRY_EXPORT)
    assert reg.state(TELEMETRY_EXPORT) is CapabilityState.ENABLED


def test_recovery_reflects_config_removal() -> None:
    # Config absent -> recovery lands on DISABLED, not ENABLED.
    reg = build_registry(Settings(_env_file=None))
    reg.mark_unhealthy(TELEMETRY_EXPORT, "boom")
    reg.mark_recovered(TELEMETRY_EXPORT)
    assert reg.state(TELEMETRY_EXPORT) is CapabilityState.DISABLED


def test_critical_capability_flips_liveness() -> None:
    # A capability with no required keys is always enabled; marking a CRITICAL one unhealthy is the
    # only thing that pulls healthy() to False.
    cap = Capability(
        name="datastore",
        required_keys=(),
        summary="primary datastore",
        critical=True,
    )
    reg = CapabilityRegistry([cap], Settings(_env_file=None))
    assert reg.state("datastore") is CapabilityState.ENABLED
    assert reg.healthy() is True

    reg.mark_unhealthy("datastore", "connection refused")
    assert reg.healthy() is False

    reg.mark_recovered("datastore")
    assert reg.healthy() is True


def test_boot_with_no_env_is_healthy_and_reports_default_capabilities() -> None:
    reg = build_registry(Settings(_env_file=None))
    assert reg.healthy() is True
    names = {row["name"] for row in reg.report()}
    # P2 adds the three bot channels; P3 adds forms + docusign; P4 adds google_drive + airtable
    # (all non-critical, disabled bare).
    assert names == {
        TELEMETRY_EXPORT,
        LLM_INFERENCE,
        SLACK,
        EMAIL_IN,
        EMAIL_OUT,
        TALLY,
        DOCUSIGN,
        GOOGLE_DRIVE,
        AIRTABLE,
    }


def test_bot_channels_disabled_without_config_and_never_fail_liveness() -> None:
    reg = build_registry(Settings(_env_file=None))
    for name in (SLACK, EMAIL_IN, EMAIL_OUT, TALLY, DOCUSIGN):
        status = reg.get(name)
        assert status.state is CapabilityState.DISABLED
        assert status.critical is False
    assert reg.healthy() is True


def test_slack_enabled_needs_both_token_and_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One of the two Slack keys is not enough — both are required.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(SLACK) is CapabilityState.DISABLED
    assert "SLACK_SIGNING_SECRET" in reg.get(SLACK).reason

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shhh")
    reg2 = build_registry(Settings(_env_file=None))
    assert reg2.state(SLACK) is CapabilityState.ENABLED


def test_email_channels_enable_on_their_own_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "nda-bot")
    monkeypatch.setenv("IMAP_PASSWORD", "pw")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "nda-bot")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(EMAIL_IN) is CapabilityState.ENABLED
    assert reg.state(EMAIL_OUT) is CapabilityState.ENABLED


def test_google_drive_and_airtable_disabled_bare_and_never_fail_liveness() -> None:
    reg = build_registry(Settings(_env_file=None))
    for name in (GOOGLE_DRIVE, AIRTABLE):
        status = reg.get(name)
        assert status.state is CapabilityState.DISABLED
        assert status.critical is False
    assert reg.healthy() is True


def test_google_drive_needs_full_oauth_trio_and_archive_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The offline-grant OAuth trio alone is not enough — the destination folder id is also required.
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-abc")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-abc")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "refresh-abc")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(GOOGLE_DRIVE) is CapabilityState.DISABLED
    assert "DRIVE_ARCHIVE_FOLDER_ID" in reg.get(GOOGLE_DRIVE).reason

    monkeypatch.setenv("DRIVE_ARCHIVE_FOLDER_ID", "1AbCdEfGhIjK")
    reg2 = build_registry(Settings(_env_file=None))
    assert reg2.state(GOOGLE_DRIVE) is CapabilityState.ENABLED


def test_google_drive_missing_one_oauth_field_stays_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Everything present EXCEPT the refresh token -> still disabled, reason names the exact env var.
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-abc")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-abc")
    monkeypatch.setenv("DRIVE_ARCHIVE_FOLDER_ID", "1AbCdEfGhIjK")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(GOOGLE_DRIVE) is CapabilityState.DISABLED
    assert "GOOGLE_OAUTH_REFRESH_TOKEN" in reg.get(GOOGLE_DRIVE).reason
    # drive_cache_folder_name carries a default, so its absence never blocks the capability.
    assert "DRIVE_CACHE_FOLDER_NAME" not in reg.get(GOOGLE_DRIVE).reason


def test_airtable_needs_all_three_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRTABLE_PAT", "pat-abc")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appXXXXXXXXXXXXXX")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(AIRTABLE) is CapabilityState.DISABLED
    assert "AIRTABLE_TABLE" in reg.get(AIRTABLE).reason

    monkeypatch.setenv("AIRTABLE_TABLE", "Expirations")
    reg2 = build_registry(Settings(_env_file=None))
    assert reg2.state(AIRTABLE) is CapabilityState.ENABLED


def test_duplicate_capability_rejected() -> None:
    cap = Capability(name="dup", required_keys=(), summary="x")
    with pytest.raises(ValueError):
        CapabilityRegistry([cap, cap], Settings(_env_file=None))


async def test_run_probes_marks_unhealthy_on_reason_and_on_raise() -> None:
    async def failing_probe(_settings: Settings) -> str:
        return "backend unreachable"

    async def raising_probe(_settings: Settings) -> str:
        raise RuntimeError("kaboom")

    async def passing_probe(_settings: Settings) -> None:
        return None

    reg = CapabilityRegistry(
        [
            Capability("fails", (), "returns a reason", probe=failing_probe),
            Capability("raises", (), "raises", probe=raising_probe),
            Capability("passes", (), "healthy", probe=passing_probe),
        ],
        Settings(_env_file=None),
    )
    await reg.run_probes()

    assert reg.state("fails") is CapabilityState.UNHEALTHY
    assert reg.get("fails").reason == "backend unreachable"
    assert reg.state("raises") is CapabilityState.UNHEALTHY
    assert "kaboom" in reg.get("raises").reason
    assert reg.state("passes") is CapabilityState.ENABLED


async def test_run_probes_skips_disabled_capabilities() -> None:
    probed = False

    async def probe(_settings: Settings) -> None:
        nonlocal probed
        probed = True

    # Required key is absent -> capability is DISABLED -> its probe must not run.
    # (Keyed on openrouter_api_key, which is empty by default; database_url now carries a
    # non-empty SQLite default so it no longer serves as an "always-absent" gate.)
    reg = CapabilityRegistry(
        [Capability("gated", ("openrouter_api_key",), "needs a key", probe=probe)],
        Settings(_env_file=None),
    )
    await reg.run_probes()

    assert probed is False
    assert reg.state("gated") is CapabilityState.DISABLED
