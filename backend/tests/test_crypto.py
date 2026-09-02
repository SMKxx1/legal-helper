"""Fernet key encryption (``app.crypto``) and the ``database`` capability's APP_SECRET_KEY probe."""

from __future__ import annotations

import pytest

from app import crypto
from app.capabilities import DATABASE, CapabilityState, build_registry
from app.config import Settings


def test_encrypt_decrypt_roundtrip_with_explicit_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_SECRET_KEY", "zJC5cKz5aM1x4vP9x2c8x0y3v6b1n4m7q0w3e6r9t2y=")
    s = Settings(_env_file=None)
    monkeypatch.setattr(crypto, "settings", s)
    ciphertext = crypto.encrypt("sk-or-secret")
    assert ciphertext != "sk-or-secret"
    assert crypto.decrypt(ciphertext) == "sk-or-secret"


def test_dev_fallback_key_is_stable_across_calls(monkeypatch: pytest.MonkeyPatch):
    s = Settings(
        _env_file=None
    )  # app_env defaults to "dev", app_secret_key defaults to ""
    monkeypatch.setattr(crypto, "settings", s)
    ciphertext = crypto.encrypt("sk-or-secret")
    assert crypto.decrypt(ciphertext) == "sk-or-secret"
    # A second resolution derives the SAME key (fixed seed) so a value encrypted before a restart
    # decrypts fine after one, as long as APP_SECRET_KEY is still unset.
    assert crypto.resolve_fernet_key(s) == crypto.resolve_fernet_key(s)


def test_missing_key_outside_dev_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "prod")
    s = Settings(_env_file=None)
    monkeypatch.setattr(crypto, "settings", s)
    assert crypto.resolve_fernet_key(s) is None
    with pytest.raises(RuntimeError):
        crypto.encrypt("sk-or-secret")


def test_decrypt_garbage_raises_value_error(monkeypatch: pytest.MonkeyPatch):
    s = Settings(_env_file=None)
    monkeypatch.setattr(crypto, "settings", s)
    with pytest.raises(ValueError):
        crypto.decrypt("not-a-real-fernet-token")


async def test_database_capability_unhealthy_when_secret_key_missing_in_prod(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("APP_ENV", "prod")
    reg = build_registry(Settings(_env_file=None))
    assert (
        reg.state(DATABASE) is CapabilityState.ENABLED
    )  # config presence alone is fine
    await reg.run_probes()
    assert reg.state(DATABASE) is CapabilityState.UNHEALTHY
    assert reg.healthy() is False  # critical capability -> /healthz would 503


async def test_database_capability_healthy_in_prod_with_secret_key_set(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_SECRET_KEY", "zJC5cKz5aM1x4vP9x2c8x0y3v6b1n4m7q0w3e6r9t2y=")
    reg = build_registry(Settings(_env_file=None))
    await reg.run_probes()
    assert reg.state(DATABASE) is CapabilityState.ENABLED
    assert reg.healthy() is True


async def test_database_capability_healthy_in_dev_with_no_secret_key(
    monkeypatch: pytest.MonkeyPatch,
):
    reg = build_registry(Settings(_env_file=None))  # app_env defaults to "dev"
    await reg.run_probes()
    assert reg.state(DATABASE) is CapabilityState.ENABLED
