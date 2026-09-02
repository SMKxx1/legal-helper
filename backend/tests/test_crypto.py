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


def test_a_plain_passphrase_is_accepted_as_app_secret_key(monkeypatch):
    """The one-click deploy asks a stranger for APP_SECRET_KEY; a passphrase must work, not crash.

    Fernet only accepts 32 url-safe-base64 bytes, so anything else is stretched into a key.
    """
    monkeypatch.setenv("APP_SECRET_KEY", "just a memorable passphrase")
    s = Settings(_env_file=None)
    monkeypatch.setattr(crypto, "settings", s)
    round_tripped = crypto.decrypt(crypto.encrypt("sk-or-v1-secret"))
    assert round_tripped == "sk-or-v1-secret"


def test_a_real_fernet_key_is_used_verbatim():
    """Back-compat: an existing deployment's key must keep decrypting what it already stored."""
    from cryptography.fernet import Fernet

    real = Fernet.generate_key().decode()
    assert crypto.coerce_fernet_key(real) == real.encode()


def test_a_passphrase_is_stretched_not_passed_through():
    key = crypto.coerce_fernet_key("not a fernet key")
    assert key != b"not a fernet key"
    from cryptography.fernet import Fernet

    Fernet(key)  # must be a valid Fernet key, i.e. this does not raise
