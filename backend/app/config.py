"""Application configuration.

Single source of truth for runtime settings. Values come from environment variables (or a local
``.env``), with defaults chosen so the app boots with ZERO configuration on a fresh machine.

The capability registry (``app.capabilities``) asks this object *"is this config group present?"* via
:meth:`Settings.is_configured` / :meth:`Settings.missing_config`. A "config group" is simply the set
of settings keys a capability requires.

There is deliberately no ``OPENROUTER_API_KEY`` here: OpenRouter keys belong to individual users
(entered once in the add-in, encrypted at rest — see ``app.crypto``, Phase 1) and are read per
request, never from process-wide settings.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "prod", "test"]
LogFormat = Literal["console", "json"]

_VALID_FORMATS = ("console", "json")


class Settings(BaseSettings):
    """Typed runtime settings, populated from the environment / ``.env``.

    Contract: constructing ``Settings()`` never requires any variable to be set. Absent optional
    configuration disables a *capability* (a feature politely turns off); it is never a boot error.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Environment & observability ------------------------------------
    # APP_ENV drives the defaults below (dev = chatty console logs; anything else = INFO/JSON).
    app_env: AppEnv = "dev"
    # Empty string means "derive from APP_ENV" (see _derive_and_validate). An explicit value wins.
    log_level: str = ""
    log_format: str = ""
    port: int = 8000

    # ---- Persistence -----------------------------------------------------
    # SQLite by default (a single file under ./data; zero infra for dev/tests). Prod points this at
    # Postgres; a bare ``postgres://`` / ``postgresql://`` URL (what Railway hands out) is normalized
    # to the pinned ``postgresql+psycopg2://`` driver by _normalize_db_scheme below.
    database_url: str = "sqlite:///./data/app.db"

    # ---- Secrets at rest ---------------------------------------------------
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt each user's OpenRouter API key at rest
    # (app.crypto, Phase 1). Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # REQUIRED in prod (a missing key there marks the ``database`` capability unhealthy); in dev an
    # unset key derives a fixed, clearly-logged dev key so the app still boots with zero config.
    app_secret_key: str = ""

    # ---- Word add-in manifest ---------------------------------------------
    # Stable GUID for the Office manifest's <Id> — change once per deployment, never per deploy.
    addin_id: str = "7b3f9a42-1c6e-4d2a-9f51-0a1b2c3d4e5f"

    # ---- LLM models (OpenRouter model ids, per review tier) ---------------
    # OpenRouter slugs, NOT Anthropic-style ids: minor versions are dotted here (`glm-5.3`,
    # `claude-opus-4.8`), and a dashed id like `anthropic/claude-opus-4-8` does not resolve at all.
    # Verify any change against https://openrouter.ai/api/v1/models before shipping it.
    model_classifier: str = "z-ai/glm-5.3-flash"
    model_quick: str = "z-ai/glm-5.3-flash"
    model_deep: str = "z-ai/glm-5.3"

    # ---- OpenRouter (ZDR-pinned — every request carries provider {data_collection:'deny',
    # zdr:true}; no compliant route -> error, never a silent downgrade) ---------------------------
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Deep-tier provider pin, empty by default. It existed to force Anthropic Opus onto
    # google-vertex (its default ZDR route rejects json_schema) — and with a non-Anthropic deep
    # model that pin is actively harmful: `only: [google-vertex]` disables fallbacks and
    # leaves a z-ai model with no route at all. Set it only to pin a provider that actually
    # serves the configured model.
    openrouter_provider_only_deep: str = ""
    # The live check against OpenRouter's GET /api/v1/endpoints/zdr is wired (ai/zdr.py) and
    # verified against the real payload, so the capability reports enabled. Set False to make
    # /api/status advertise the model picker as unavailable.
    openrouter_zdr_list_ready: bool = True
    provider_timeout_s: float = 150.0

    # ---- Review engine -----------------------------------------------------
    # How many reviews may run at once, per process (in-process background task + semaphore).
    review_concurrency: int = 2
    max_upload_mb: int = 10
    max_doc_chars: int = 120000
    max_monthly_cost_usd: float = (
        5.0  # per user; a review is refused with 402 beyond this
    )

    # ---- Document bucket (capability: bucket) -------------------------------
    # A Railway bucket storing the original .docx of every review. Blank -> the capability is
    # disabled and reviews simply skip document storage.
    max_docs_per_user: int = (
        20  # retention cap; oldest stored document is dropped beyond this
    )
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "auto"

    # ---- Sign-up -------------------------------------------------------------
    # Students create their own account from the add-in, supplying their own OpenRouter key.
    # Set SIGNUP_ENABLED=false to close registration once the workshop is over, leaving the
    # already-created accounts working.
    signup_enabled: bool = True

    # ---- Normalization / derivation -------------------------------------
    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_env(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("log_format", mode="before")
    @classmethod
    def _normalize_format(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_db_scheme(cls, v: object) -> object:
        # Managed Postgres providers (Railway included) hand out `postgres://`, and SQLAlchemy 2.0
        # rejects a bare scheme with no driver. Pin the psycopg2 driver so a prod URL works
        # unmodified. SQLite/other URLs pass through untouched.
        if isinstance(v, str):
            if v.startswith("postgres://"):
                return "postgresql+psycopg2://" + v[len("postgres://") :]
            if v.startswith("postgresql://"):
                return "postgresql+psycopg2://" + v[len("postgresql://") :]
        return v

    @model_validator(mode="after")
    def _derive_and_validate(self) -> Settings:
        # LOG_LEVEL: explicit value (upper-cased) wins; otherwise DEBUG in dev, INFO elsewhere.
        self.log_level = (
            self.log_level or ("DEBUG" if self.app_env == "dev" else "INFO")
        ).upper()
        # LOG_FORMAT: explicit value wins; otherwise console in dev, JSON elsewhere.
        if not self.log_format:
            self.log_format = "console" if self.app_env == "dev" else "json"
        if self.log_format not in _VALID_FORMATS:
            raise ValueError(
                f"LOG_FORMAT must be one of {list(_VALID_FORMATS)}, got {self.log_format!r}"
            )
        return self

    @field_validator("max_upload_mb")
    @classmethod
    def _positive_upload_cap(cls, v: int) -> int:
        # A 0/negative cap makes the bounded `file.read(max_bytes+1)` read unbounded -> clamp to >=1.
        return max(1, int(v))

    @field_validator("max_monthly_cost_usd")
    @classmethod
    def _non_negative_cost(cls, v: float) -> float:
        return max(0.0, float(v))  # 0 == disabled; never negative

    @field_validator("max_docs_per_user")
    @classmethod
    def _positive_doc_cap(cls, v: int) -> int:
        return max(1, int(v))

    # ---- Config-group presence API (used by the capability registry) ----
    def is_configured(self, *keys: str) -> bool:
        """True iff every named settings key holds a non-empty value.

        ``keys`` are settings field names (e.g. ``"s3_bucket"``). This is the "is this config group
        present?" question a capability asks about its required keys.
        """
        return all(self._present(k) for k in keys)

    def missing_config(self, *keys: str) -> list[str]:
        """The ENV-var names of the given keys that are unset/empty — for capability ``reason`` text.

        Field name -> env var is a straight upper-case (no prefix, case-insensitive env matching), so
        ``s3_bucket`` reports as ``S3_BUCKET``.
        """
        return [k.upper() for k in keys if not self._present(k)]

    def _present(self, key: str) -> bool:
        value = getattr(self, key, None)
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip() != ""
        return bool(value)

    # ---- Derived helpers -------------------------------------------------
    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def openrouter_provider_only_deep_list(self) -> tuple[str, ...]:
        """The deep-tier provider pin, parsed to slugs (e.g. ``("google-vertex",)``)."""
        return tuple(
            s.strip()
            for s in self.openrouter_provider_only_deep.split(",")
            if s.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (cached). Tests construct ``Settings(_env_file=None)`` directly
    for isolation rather than going through this cache."""
    return Settings()


#: Module-level singleton (``from app.config import settings``). Reads the environment/.env once at
#: import; tests that need isolation build ``Settings(_env_file=None)``.
settings = get_settings()
