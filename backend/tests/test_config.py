"""Settings defaults, env-derived defaults, explicit overrides, and the config-presence helpers."""

from __future__ import annotations

import pytest

from app.config import Settings


def test_zero_env_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.log_level == "DEBUG"  # dev default
    assert s.log_format == "console"  # dev default
    # P1: DATABASE_URL now carries the source engine's SQLite default (was "" reserved in P0).
    assert s.database_url == "sqlite:///./data/app.db"
    assert s.applicationinsights_connection_string == ""


def test_prod_derives_info_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    s = Settings(_env_file=None)
    assert s.app_env == "prod"
    assert s.log_level == "INFO"
    assert s.log_format == "json"


def test_explicit_overrides_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "warning")  # normalized to upper
    monkeypatch.setenv("LOG_FORMAT", "console")  # overrides the prod default
    s = Settings(_env_file=None)
    assert s.log_level == "WARNING"
    assert s.log_format == "console"


def test_invalid_log_format_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "xml")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_config_presence_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    key = "applicationinsights_connection_string"
    assert s.is_configured(key) is False
    assert s.missing_config(key) == ["APPLICATIONINSIGHTS_CONNECTION_STRING"]

    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc"
    )
    s2 = Settings(_env_file=None)
    assert s2.is_configured(key) is True
    assert s2.missing_config(key) == []


# --------------------------------------------------------------------------- #
# P1 data-layer / engine settings merged from the source engine.
# --------------------------------------------------------------------------- #
def test_ported_field_defaults() -> None:
    s = Settings(_env_file=None)
    # OpenRouter reserved for wave 3 (unused now).
    assert s.openrouter_api_key == ""
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert s.openrouter_zdr_only is True
    # Anthropic fallback adapter kept.
    assert s.anthropic_model == "claude-sonnet-4-6"
    assert s.anthropic_max_tokens == 1024
    # Engine caps / persistence defaults.
    assert s.review_concurrency == 3
    assert s.provider_timeout_s == 150.0
    assert s.sim_cache_enabled is True
    assert s.max_upload_mb == 25
    assert s.max_upload_bytes == 25 * 1024 * 1024
    assert s.data_dir == "./data"


def test_dropped_signed_field_is_gone() -> None:
    # The retired SIGNED-principal HMAC key must NOT be a settings field anymore.
    s = Settings(_env_file=None)
    assert not hasattr(s, "auth_principal_hmac_key")
    assert not hasattr(s, "ai_provider")


def test_env_override_of_ported_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_CONCURRENCY", "8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("SIM_CACHE_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.review_concurrency == 8
    assert s.openrouter_api_key == "sk-or-test"
    assert s.sim_cache_enabled is False


def test_upload_cap_clamped_to_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "0")  # would make the bounded read unbounded
    assert Settings(_env_file=None).max_upload_mb == 1


def test_postgres_scheme_normalized_to_psycopg2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host:5432/db")
    assert (
        Settings(_env_file=None).database_url
        == "postgresql+psycopg2://u:p@host:5432/db"
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    assert Settings(_env_file=None).database_url == "postgresql+psycopg2://u:p@host/db"
    # SQLite passes through untouched.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    assert Settings(_env_file=None).database_url == "sqlite:///./x.db"


# --------------------------------------------------------------------------- #
# P4 archive / watcher / expiration settings (PLAN §3.10, §3.8).
# --------------------------------------------------------------------------- #
def test_p4_field_defaults() -> None:
    s = Settings(_env_file=None)
    # Google Drive archive group — all secrets empty by default; cache folder name has a default.
    assert s.google_oauth_client_id == ""
    assert s.google_oauth_client_secret == ""
    assert s.google_oauth_refresh_token == ""
    assert s.drive_archive_folder_id == ""
    assert s.drive_cache_folder_name == "Signed Company NDAs Cache"
    # Airtable expiration-tracker group — all empty by default.
    assert s.airtable_pat == ""
    assert s.airtable_base_id == ""
    assert s.airtable_table == ""
    # Expiration alias — the benchmark contract pin (§3.8): gemini-3.5-flash via google-vertex.
    assert s.openrouter_model_expiration == "google/gemini-3.5-flash"
    assert s.expiration_provider_only == "google-vertex"
    assert s.expiration_provider_only_list == ("google-vertex",)
    # Watcher cadence — the OLD n8n bug ran at 1min; 5 is the intended cadence (§5). Sweep at 02:00.
    assert s.watcher_interval_minutes == 5
    assert s.expiration_sweep_hour_utc == 2


def test_expiration_provider_only_list_parses_multiple_and_blank() -> None:
    s = Settings(
        _env_file=None, expiration_provider_only="google-vertex, google-ai-studio"
    )
    assert s.expiration_provider_only_list == ("google-vertex", "google-ai-studio")
    # Blank -> empty tuple (would let any ZDR-qualifying route serve the alias).
    assert (
        Settings(
            _env_file=None, expiration_provider_only=""
        ).expiration_provider_only_list
        == ()
    )


def test_watcher_interval_clamped_to_at_least_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WATCHER_INTERVAL_MINUTES", "0")  # would busy-loop the worker
    assert Settings(_env_file=None).watcher_interval_minutes == 1


def test_expiration_sweep_hour_clamped_into_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXPIRATION_SWEEP_HOUR_UTC", "99")
    assert Settings(_env_file=None).expiration_sweep_hour_utc == 23
    monkeypatch.setenv("EXPIRATION_SWEEP_HOUR_UTC", "-3")
    assert Settings(_env_file=None).expiration_sweep_hour_utc == 0
