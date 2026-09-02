"""Settings defaults, env-derived defaults, explicit overrides, and the config-presence helpers."""

from __future__ import annotations

import pytest

from app.config import Settings


def test_zero_env_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.log_level == "DEBUG"  # dev default
    assert s.log_format == "console"  # dev default
    assert s.database_url == "sqlite:///./data/app.db"
    assert s.port == 8000


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
    key = "s3_bucket"
    assert s.is_configured(key) is False
    assert s.missing_config(key) == ["S3_BUCKET"]

    monkeypatch.setenv("S3_BUCKET", "documents")
    s2 = Settings(_env_file=None)
    assert s2.is_configured(key) is True
    assert s2.missing_config(key) == []


def test_no_shared_openrouter_key_field() -> None:
    # There is deliberately no global OPENROUTER_API_KEY: keys belong to individual users.
    s = Settings(_env_file=None)
    assert not hasattr(s, "openrouter_api_key")


def test_field_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.app_secret_key == ""
    assert s.addin_id == "7b3f9a42-1c6e-4d2a-9f51-0a1b2c3d4e5f"
    assert s.model_classifier == "z-ai/glm-5.3-flash"
    assert s.model_quick == "z-ai/glm-5.3-flash"
    assert s.model_deep == "z-ai/glm-5.3"
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert s.openrouter_provider_only_deep == ""
    assert s.openrouter_provider_only_deep_list == ()
    assert s.openrouter_zdr_list_ready is True
    assert s.provider_timeout_s == 150.0
    assert s.review_concurrency == 2
    assert s.max_upload_mb == 10
    assert s.max_upload_bytes == 10 * 1024 * 1024
    assert s.max_doc_chars == 120000
    assert s.max_monthly_cost_usd == 5.0
    assert s.max_docs_per_user == 20
    assert s.s3_endpoint == ""
    assert s.s3_bucket == ""
    assert s.s3_access_key_id == ""
    assert s.s3_secret_access_key == ""
    assert s.s3_region == "auto"
    assert s.signup_enabled is True


def test_env_override_of_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_CONCURRENCY", "8")
    monkeypatch.setenv("SIGNUP_ENABLED", "false")
    monkeypatch.setenv("MAX_MONTHLY_COST_USD", "12.5")
    s = Settings(_env_file=None)
    assert s.review_concurrency == 8
    assert s.signup_enabled is False
    assert s.max_monthly_cost_usd == 12.5


def test_upload_cap_clamped_to_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "0")  # would make the bounded read unbounded
    assert Settings(_env_file=None).max_upload_mb == 1


def test_docs_per_user_clamped_to_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_DOCS_PER_USER", "0")
    assert Settings(_env_file=None).max_docs_per_user == 1


def test_monthly_cost_cap_never_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_MONTHLY_COST_USD", "-5")
    assert Settings(_env_file=None).max_monthly_cost_usd == 0.0


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


def test_openrouter_provider_only_deep_list_parses_multiple_and_blank() -> None:
    s = Settings(
        _env_file=None, openrouter_provider_only_deep="google-vertex, anthropic"
    )
    assert s.openrouter_provider_only_deep_list == ("google-vertex", "anthropic")
    assert (
        Settings(
            _env_file=None, openrouter_provider_only_deep=""
        ).openrouter_provider_only_deep_list
        == ()
    )


def test_app_env_accepts_what_a_deployer_actually_types() -> None:
    """The one-click deploy form asks for APP_ENV as free text.

    APP_ENV is a strict Literal, so "production" — the obvious thing to write — used to raise a
    ValidationError at import and crash-loop the service, with nothing to suggest that the only
    problem was the word.
    """
    for typed, expected in (
        ("production", "prod"),
        ("Production", "prod"),
        (" PRODUCTION ", "prod"),
        ("live", "prod"),
        ("prd", "prod"),
        ("development", "dev"),
        ("local", "dev"),
        ("testing", "test"),
        ("prod", "prod"),
        ("dev", "dev"),
    ):
        assert Settings(_env_file=None, app_env=typed).app_env == expected, typed


def test_a_real_typo_in_app_env_still_fails_loudly() -> None:
    """Forgiving the known synonyms must not turn every value into a silent default."""
    from pydantic import ValidationError

    for typo in ("prodd", "banana", ""):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, app_env=typo)
