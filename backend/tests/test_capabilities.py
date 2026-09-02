"""Capability registry state machine: enabled/disabled/unhealthy transitions and liveness."""

from __future__ import annotations

import pytest

from app.capabilities import (
    BUCKET,
    DATABASE,
    OPENROUTER_ZDR_LIST,
    Capability,
    CapabilityRegistry,
    CapabilityState,
    build_registry,
)
from app.config import Settings


def test_database_enabled_by_default_and_critical() -> None:
    # SQLite works with zero config, so `database` is enabled out of the box; it is the only
    # critical capability.
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(DATABASE) is CapabilityState.ENABLED
    assert reg.get(DATABASE).critical is True
    assert reg.healthy() is True


def test_bucket_disabled_without_config() -> None:
    reg = build_registry(Settings(_env_file=None))
    status = reg.get(BUCKET)
    assert status.state is CapabilityState.DISABLED
    assert "S3_ENDPOINT" in status.reason
    assert "S3_BUCKET" in status.reason
    assert status.critical is False
    assert reg.healthy() is True


def test_bucket_enabled_once_all_four_fields_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("S3_BUCKET", "documents")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(BUCKET) is CapabilityState.ENABLED


def test_bucket_missing_one_field_stays_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("S3_BUCKET", "documents")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "id")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(BUCKET) is CapabilityState.DISABLED
    assert "S3_SECRET_ACCESS_KEY" in reg.get(BUCKET).reason


def test_openrouter_zdr_list_is_enabled_now_that_the_live_check_works() -> None:
    # ai/zdr.py checks OpenRouter's real /endpoints/zdr payload, so the capability advertises it.
    reg = build_registry(Settings(_env_file=None))
    status = reg.get(OPENROUTER_ZDR_LIST)
    assert status.state is CapabilityState.ENABLED
    assert status.critical is False  # a model picker outage must never fail /healthz
    assert reg.healthy() is True


def test_openrouter_zdr_list_can_be_switched_off() -> None:
    reg = build_registry(Settings(_env_file=None, openrouter_zdr_list_ready=False))
    assert reg.get(OPENROUTER_ZDR_LIST).state is CapabilityState.DISABLED
    assert reg.healthy() is True


def test_boot_with_no_env_is_healthy_and_reports_exactly_three_capabilities() -> None:
    reg = build_registry(Settings(_env_file=None))
    assert reg.healthy() is True
    names = {row["name"] for row in reg.report()}
    assert names == {DATABASE, BUCKET, OPENROUTER_ZDR_LIST}


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


def test_recovery_reflects_config_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("S3_BUCKET", "documents")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    reg = build_registry(Settings(_env_file=None))
    assert reg.state(BUCKET) is CapabilityState.ENABLED

    reg.mark_unhealthy(BUCKET, "boom")
    assert reg.state(BUCKET) is CapabilityState.UNHEALTHY

    # Config still present -> recovery lands back on ENABLED.
    reg.mark_recovered(BUCKET)
    assert reg.state(BUCKET) is CapabilityState.ENABLED


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
    reg = CapabilityRegistry(
        [Capability("gated", ("s3_bucket",), "needs a bucket name", probe=probe)],
        Settings(_env_file=None),
    )
    await reg.run_probes()

    assert probed is False
    assert reg.state("gated") is CapabilityState.DISABLED
