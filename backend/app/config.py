"""Application configuration.

Single source of truth for runtime settings. Values come from environment variables (or a local
``.env``), with defaults chosen so the app boots with ZERO configuration on a fresh machine.

The capability registry (``app.capabilities``) asks this object *"is this config group present?"* via
:meth:`Settings.is_configured` / :meth:`Settings.missing_config`. A "config group" is simply the set
of settings keys a capability requires — so later phases add Slack/DocuSign/etc. by declaring their
own keys on a capability and reusing the same two helpers; no new machinery here.

P1 note: the persistence + engine settings ported from ``nda-review-cloud`` land here so the ported
db/engine/ingestion/generation code reads the same field names it always did (import stability). The
SIGNED-principal HMAC key and the n8n-only knobs are intentionally NOT carried over (that plane is
retired); the OpenRouter fields drive the primary ZDR-pinned adapter (``app.ai.openrouter``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "prod", "test"]
LogFormat = Literal["console", "json"]

_VALID_ENVS = ("dev", "prod", "test")
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

    # ---- Telemetry export (capability: telemetry_export) ----------------
    # Azure Application Insights / Monitor connection string. Absent => telemetry export is DISABLED
    # (a clean no-op), never an error. See app/telemetry/otel.py.
    applicationinsights_connection_string: str = ""

    # ---- Persistence -----------------------------------------------------
    # SQLite by default (a single file under DATA_DIR; zero infra for dev/tests). Prod points this at
    # Postgres; a bare ``postgres://`` / ``postgresql://`` URL (what managed providers hand out) is
    # normalized to the pinned ``postgresql+psycopg2://`` driver by _normalize_db_scheme below.
    database_url: str = "sqlite:///./data/app.db"
    data_dir: str = "./data"
    # Hard cap on a single upload (reviews, generate-nda, template upload). See max_upload_bytes.
    max_upload_mb: int = 25

    # ---- AI provider: Anthropic (fallback adapter) ----------------------
    # The ported direct-Anthropic gateway remains a configuration fallback per alias (PLAN §3.8); the
    # primary path becomes the wave-3 OpenRouter adapter. These keep the fallback wired.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 1024

    # ---- AI provider: OpenRouter (primary; ZDR-pinned — PLAN §3.8) --------
    # When the key is present, the engine routes every review/router call through the OpenRouter
    # adapter (``app.ai.openrouter``); when absent, the direct-Anthropic fallback above is used.
    # The ``llm_inference`` capability gates on the key.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Enforce Zero-Data-Retention routing (fail closed — no silent fallback to a non-ZDR route):
    # every request carries provider {data_collection:'deny', zdr:true, allow_fallbacks:false}.
    openrouter_zdr_only: bool = True
    # Optional per-request provider pinning: comma-separated OpenRouter provider slugs passed as
    # ``provider.only`` (e.g. "anthropic"). Blank = any route that satisfies the ZDR policy.
    openrouter_provider_only: str = ""
    # DEEP-tier provider pin. claude-opus-4-8's default ZDR provider (Amazon Bedrock) REJECTS the
    # json_schema ``response_format`` ("output_config.format: Extra inputs are not permitted"), which
    # 400s every deep review. Google Vertex serves opus-4-8 with ZDR AND accepts json_schema, so the
    # deep primary is pinned there. (Quick/router stay on the global pin — they route fine by default.)
    openrouter_provider_only_deep: str = "google-vertex"
    # Per-tier model ids, vendor-namespaced as OpenRouter expects. Tier structure and model choices
    # mirror the direct-Anthropic gateway exactly (routes_v1._build_gateways).
    openrouter_model_review_deep: str = "anthropic/claude-opus-4-8"
    openrouter_model_review_quick: str = "anthropic/claude-sonnet-4-6"
    openrouter_model_router: str = "anthropic/claude-haiku-4-5"

    # ---- Engine (/v1 API) ------------------------------------------------
    # Optional API key: when set, /v1 endpoints require a matching X-API-Key.
    # Paths default to the repo playbook build + standard template when blank.
    engine_api_key: str = ""
    engine_playbook_path: str = ""
    engine_standard_template_path: str = ""

    # ---- Engine service-account principals -------------------------------
    # The machine /v1 path (Word add-in, API callers) authenticates with an X-API-Key that binds to a
    # NAMED service principal, so every engine run is attributable (persisted on
    # EngineReview.actor_user_id) and individually capped. `engine_api_key` (above) is the legacy
    # default key -> principal "svc:default". Additional named keys are comma-separated "name:secret"
    # pairs -> "svc:<name>". A CONFIGURED engine rejects a missing/unknown key with 401 (no fail-open);
    # an UNCONFIGURED engine binds an open, loudly-logged "svc:local" dev principal. The DB-backed
    # ServiceAccountKey table supersedes this env fallback (same resolver seam).
    engine_service_keys: str = ""
    # When True, an engine with NO usable key configured REFUSES to serve /v1 (503) instead of binding
    # the open dev principal — set this in production so a forgotten/blanked key fails CLOSED. Default
    # False keeps local dev + tests serving with svc:local. (A key that is PRESENT but blank/malformed
    # always fails closed regardless of this flag — that is unambiguously a misconfiguration.)
    engine_require_key: bool = False
    # Per-principal sliding-window request cap (requests / 60s); 0 disables. 429 when exceeded.
    engine_rate_limit_per_min: int = 0
    # Per-principal calendar-month engine spend cap in USD; 0 disables. 429 when exceeded. SOFT,
    # eventually-consistent guard (a pre-flight read of already-PERSISTED spend), not a hard ceiling —
    # a concurrent burst can overshoot it. Pair it with engine_rate_limit_per_min to bound bursts.
    engine_monthly_cost_cap_usd: float = 0.0
    # Per-call provider timeout in seconds, applied with client.with_options on each gateway call. The
    # retry ladder can spend up to ~3x this wall-clock while holding a review slot. Gateways are
    # lru-cached at first use, so changing this needs a restart.
    provider_timeout_s: float = 150.0

    # Shared store for the per-principal request-rate cap (PL-6). Blank -> the cap is enforced
    # in-process per replica (fine for single-replica). Set to a Redis URL (e.g. redis://redis:6379/0)
    # in a MULTI-REPLICA deployment so every replica shares one sliding window; if Redis is
    # unreachable the limiter degrades to in-process (it never fails a request).
    redis_url: str = ""

    # ---- Concurrency -----------------------------------------------------
    # How many PAID engine reviews may run at once PER PROCESS (routes_v1 semaphore; the worker's async
    # claimer applies the same value as its own budget). At capacity /v1/reviews returns a typed 429
    # "review_capacity" (with Retry-After). Cache-hit and extract-only paths are never gated.
    review_concurrency: int = 3

    # ---- Prompt-cache TTL (deep/Opus prefix) -----------------------------
    # When True, the DEEP tier's PRIMARY (Opus) gateway marks its stable prompt prefix with a 1-HOUR
    # cache TTL instead of the 5-minute default, so bursty review traffic with gaps keeps its warm
    # prefix. COST: a 1h cache WRITE bills 2x the base input rate (vs 1.25x for 5m). Default OFF.
    prompt_cache_1h_deep: bool = False

    # ---- Document-reuse cache --------------------------------------------
    # When the SAME document is re-submitted (even via a different channel, so the raw bytes — and
    # thus the sha256 idempotency key — differ), serve the stored review instead of paying for a fresh
    # LLM run. Matches on NORMALIZED TEXT (identical content after canonicalizing
    # unicode/whitespace/case/punctuation). Only an identical-text re-submission hits; a "similar"
    # (edited) NDA always gets a fresh review. Set false to force every upload through the engine.
    sim_cache_enabled: bool = True

    # ---- Document conversion (Doc Editor) -------------------------------
    # How the Doc Editor rebuilds an editable rendering of a *PDF* source. "local" -> PyMuPDF span
    # reconstruction (free, offline), currently the only supported strategy. DOCX is parsed natively.
    pdf_extract_strategy: str = "local"

    # ---- Local OCR (scanned / image-only PDFs) --------------------------
    # Fully-local, zero-egress OCR fallback used when a PDF has no text layer. Tesseract is the
    # portable default (bundled in the image); on macOS, Apple Vision (via optional `ocrmac`) is used
    # automatically when available.
    ocr_enabled: bool = True
    ocr_backend: str = "auto"  # auto | tesseract | apple | paddle
    ocr_dpi: int = 300  # page render DPI (300 = sweet spot)
    ocr_lang: str = "eng"  # tesseract language(s), e.g. "eng" or "eng+deu"
    ocr_tessdata_dir: str = (
        ""  # override traineddata dir (e.g. tessdata_best); blank = system default
    )
    ocr_max_pages: int = 60  # safety cap on page count for a single OCR run
    # Quality escalation: when the (cheap) Tesseract backend produces a garbled page, re-OCR just that
    # page with a stronger backend and keep whichever is better.
    ocr_escalate: bool = True
    ocr_min_quality: float = (
        0.70  # word-likeness 0..1; a token-rich page below this is "failed"
    )
    ocr_min_tokens_for_quality: int = (
        25  # need this many tokens before judging quality (skip sparse pages)
    )
    ocr_fallback_backend: str = (
        ""  # escalation engine; blank = auto (apple if available, else paddle)
    )

    # ---- Display rendering (non-PDF uploads) ----------------------------
    # LibreOffice headless renders .docx/.doc/.odt/.rtf/.txt into a faithful display PDF (preserves the
    # original fonts/layout/margins). If `soffice` is missing or conversion fails, we fall back to the
    # pure-Python fpdf2 re-typeset so the app still works with zero system deps.
    soffice_bin: str = "soffice"
    soffice_timeout: float = 90.0

    # ---- Playbook embeddings (escalate-only substrate; default OFF) ------
    # A precomputed, static embedding index used to align an incoming clause against the baseline it
    # most resembles. PRIMITIVE layer only — it may ONLY ever ESCALATE scrutiny, never relax it. With
    # "off" the provider factory returns None and every embed call is a no-op (engine byte-identical
    # to no-embeddings). "voyage" -> Voyage AI (lazy import); "fake" -> deterministic test vectors.
    embeddings_provider: str = "off"  # off | voyage | fake
    voyage_api_key: str | None = None
    embeddings_model: str = "voyage-law-2"
    # The single precomputed index (built offline). Relative to the repo root; IGNORED (logged) if its
    # metadata records a different playbook release than the running process.
    embed_playbook_index_path: str = "playbook/v4/embeddings.npz"
    # Cosine floor for treating an incoming clause as "covered" by its best-match baseline clause.
    embed_cov_threshold: float = 0.60
    # Cosine floor above which an incoming clause is flagged as hitting a walk-away trigger.
    embed_trigger_threshold: float = 0.70

    # ---- Secrets at rest -------------------------------------------------
    # Optional Fernet key (urlsafe base64, 32 bytes; generate with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
    # When set, secrets stored in app_settings (e.g. an API key entered via the Settings UI) are
    # encrypted at rest. When unset, they are stored as-is, so existing installs are unchanged.
    settings_encryption_key: str = ""

    # ---- Bootstrap admin (first run) ------------------------------------
    # On first boot with ZERO admins, an admin account is auto-created with this user id + password and
    # must_change_password=True (so the first action is a forced password change). Leave blank to skip.
    # ROTATE/CLEAR these env vars after the first login.
    admin_bootstrap_user_id: str = ""
    admin_bootstrap_password: str = ""

    # ---- API / CORS ------------------------------------------------------
    # EXACT allowed origins (comma-separated) for CREDENTIALED CORS — wildcards are invalid with
    # cookies. PROD is same-origin so CORS isn't even engaged; set this to the app's exact https origin
    # there. The defaults are local dev origins.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- Trusted edge / forwarded headers --------------------------------
    # Whether to honour ``X-Forwarded-Proto: https`` to mark session cookies ``Secure``. True is
    # correct behind a trusted TLS-terminating edge that overwrites the header. Set False if the API is
    # ever reachable directly over plain HTTP without such an edge, so a client-sent header can't coerce
    # the Secure flag.
    trust_forwarded_proto: bool = True

    # ---- Auth IP throttle (credential-stuffing / reset-spam mitigation) --
    # A PER-IP sliding-window throttle in front of both /api/auth/login and
    # /api/auth/password/reset-request, enforced IN-PROCESS. Master switch below; both endpoints share
    # the same window size.
    auth_ip_throttle_enabled: bool = True
    # Failed LOGIN attempts allowed per IP per window before a 429. Only FAILED attempts count.
    auth_ip_max_attempts: int = 20
    # Reset-request calls allowed per IP per window before a 429. EVERY call counts (anti-enumeration).
    auth_reset_ip_max: int = 5
    # Shared sliding-window size (seconds) for both caps above.
    auth_ip_window_s: int = 300

    # ---- Admin IP allowlist (optional gate on /admin — PLAN §6) ----------
    # Comma/space/semicolon-separated IPs and/or CIDRs permitted to reach the /admin plane. EMPTY (the
    # default) = allow ALL — the gate is a transparent pass-through, so wiring it early costs nothing.
    # When set, a client whose IP is not on the list gets 403 from ``require_admin_ip``. Client-IP trust
    # honours ``X-Forwarded-For`` ONLY behind a trusted edge (``trust_forwarded_proto``); otherwise the
    # direct socket peer is used, so a directly reachable API can't be tricked with a spoofed header.
    # Read by app/auth/admin_ip.py. Seed it via the admin UI during cutover (see docs/RUNBOOK.md).
    admin_ip_allowlist: str = ""

    # ---- Bot: Slack (capability: slack — PLAN §3.3) ----------------------
    # The in-process Slack surface (Bolt): events (app_mention/file_share/message) + interactivity
    # (button/modal callbacks). Both keys are required for the ``slack`` capability; absent => the
    # capability is DISABLED (the Slack channel politely turns off), never a boot error.
    slack_bot_token: str = ""  # xoxb-… bot OAuth token (Slack Web API calls)
    slack_signing_secret: str = (
        ""  # v0 HMAC verification of inbound events/interactivity (fail-closed)
    )
    # The bot's OWN Slack user id — the human-event guard drops the bot's own messages (reference §3.1).
    nda_bot_user_id: str = ""
    # The bot's From address on outbound email (reference §8 default).
    nda_bot_from_email: str = "nda-bot@example.com"

    # ---- Bot: Email intake / IMAP (capability: email_in — PLAN §3.3) -----
    # The worker polls UNSEEN mail here. host+user+password required for the ``email_in`` capability.
    imap_host: str = ""
    imap_port: int = 993  # IMAPS (implicit TLS)
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    # ---- Bot: Email delivery / SMTP (capability: email_out — PLAN §3.3) --
    # Threaded email replies + attachments. host+user+password required for the ``email_out`` capability.
    smtp_host: str = ""
    smtp_port: int = (
        587  # submission (STARTTLS); 465 = implicit TLS (set SMTP_SECURE=true)
    )
    # Implicit TLS on connect. Leave false for STARTTLS on 587; set true for 465 (reference §2.4).
    smtp_secure: bool = False
    smtp_user: str = ""
    smtp_password: str = ""

    # ---- Bot: Admin routing & sender policy (PLAN §3.4, §3.3) ------------
    # Where allowlist-miss approval requests are announced (reference §3.5 "Notify Admin").
    nda_admin_slack_channel: str = ""
    nda_admin_email: str = ""
    # Email hardening (PLAN §3.3, §6): require SPF/DKIM/DMARC alignment before an email sender is
    # treated as VERIFIED (can match the allowlist / trigger envelope+archive actions). Default true —
    # unauthenticated mail stays read-only-helpful. Set false ONLY for a trusted-relay dev setup.
    email_require_dmarc: bool = True
    # How often the worker sweeps ``bot_inbox`` for stuck/failed rows (crash recovery + retry, §3.3).
    bot_inbox_sweep_seconds: int = 30

    # ---- Tally intake (PLAN §3.6): external form + signed webhook ---------
    # The NDA intake form lives on Tally (the in-house /f service was retired). The bot's generate
    # intent hands out a channel-prefilled link to this form; on submit Tally POSTs a signed webhook to
    # /integrations/tally/webhook, which maps the fields and generates the NDA. ``tally_signing_secret``
    # gates the ``tally`` capability (it verifies the webhook HMAC); absent => the webhook is a 503 stub
    # (the generate intent still hands out the link). KV: tally-signing-secret.
    tally_signing_secret: str = ""
    tally_form_id: str = (
        "jagDPJ"  # the "NDA Generator" form id (used in the public link path)
    )
    tally_base_url: str = (
        "https://tally.so"  # public form host; link is {base}/r/{form_id}
    )
    # General public base URL for absolute outbound links (password-reset emails, app.auth.reset_email).
    # Optional — when unset those links degrade to a relative path. No longer tied to any capability.
    form_base_url: str = ""

    # ---- DocuSign (PLAN §3.9): envelope create + send --------------------
    # JWT-Grant service integration (PyJWT RS256). demo host by default; production is a config
    # change. All four below required for the docusign capability; private key lives in KV only.
    docusign_base_uri: str = "https://demo.docusign.net"
    docusign_oauth_host: str = "account-d.docusign.com"  # prod: account.docusign.com
    docusign_account_id: str = ""
    docusign_integration_key: str = ""
    docusign_user_id: str = ""  # the impersonated API user (JWT subject)
    docusign_private_key: str = ""  # PEM (RS256); KV: docusign-private-key

    # ---- Archive storage: Google Drive (capability: google_drive — PLAN §3.10) --
    # The signed-NDA archive lives in Google Drive. Both channels' archive intent PDF-normalizes an
    # attachment and uploads it to the CACHE folder; the worker's watcher then auto-names each drop
    # and files it into the ARCHIVE folder ("Signed Company NDAs"). Auth is a stored offline-grant
    # (installed-app) OAuth trio: the refresh token mints short-lived access tokens on demand — the
    # builders talk to the Google token + Drive REST endpoints with plain httpx (no SDK dep). The
    # three oauth fields + drive_archive_folder_id are required for the ``google_drive`` capability;
    # absent => archive + watcher politely turn off (a disabled capability, never a boot error).
    google_oauth_client_id: str = ""  # OAuth 2.0 client id (Google Cloud credential)
    google_oauth_client_secret: str = (
        ""  # OAuth 2.0 client secret. KV: google-oauth-client-secret
    )
    google_oauth_refresh_token: str = ""  # offline-grant refresh token (mints access tokens). KV: google-oauth-refresh-token
    # DESTINATION folder id for filed, auto-named signed NDAs — the n8n watcher's hard-coded main
    # folder ("Signed Company NDAs") made configurable. Required for the ``google_drive`` capability.
    drive_archive_folder_id: str = ""
    # The CACHE folder the watcher polls, resolved BY NAME (Drive fileFolder query), matching the n8n
    # "Signed Company NDAs Cache" convention. Has a safe default so the watcher resolves it unmodified.
    drive_cache_folder_name: str = "Signed Company NDAs Cache"

    # ---- Expiration tracker: Airtable (capability: airtable — PLAN §3.10) --------
    # The extracted NDA expiration date is upserted into an Airtable base — the expiration tracker
    # (capability-gated, MINIMAL fields per §6). PAT + base id + table are ALL required for the
    # ``airtable`` capability; absent => extraction still runs and logs but the upsert is a clean
    # no-op (never a boot error). The builder talks to the Airtable REST API with plain httpx.
    airtable_pat: str = ""  # Personal Access Token (scoped). KV: airtable-pat
    airtable_base_id: str = ""  # the base id (appXXXXXXXXXXXXXX)
    airtable_table: str = ""  # the expiration-tracker table name or id

    # ---- Expiration extraction alias (PLAN §3.8, §3.10) --------------------------
    # The ``expiration`` model alias is NOT an Anthropic path: the benchmark's winning contract is
    # ``google/gemini-3.5-flash`` pinned to the ``google-vertex`` provider (file-parser plugin,
    # native-PDF vision, strict ``YYYY-MM-DD|ERROR`` output). These pin that contract; the adapter
    # sends expiration_provider_only as ``provider.only`` with allow_fallbacks=false + the ZDR policy.
    openrouter_model_expiration: str = "google/gemini-3.5-flash"
    # Provider pin for the expiration alias (comma-separated OpenRouter provider slugs sent as
    # ``provider.only``). Default "google-vertex" IS the benchmark pin — do NOT relax it without
    # re-running the expiration eval. Blank would let any ZDR-qualifying route serve the alias.
    expiration_provider_only: str = "google-vertex"

    # ---- Watcher & expiration sweep cadence (PLAN §3.10; §5 old-bug fix) ----------
    # How often the worker's cache-folder watcher polls Drive for completed-envelope drops. The OLD
    # n8n watcher's Schedule set field='minutes' with NO minutesInterval, so it effectively ran every
    # 1 MINUTE (while its own name/sticky said 5) — the documented bug (§5). Default 5 = the INTENDED
    # cadence. Clamped to >=1 (a zero/negative interval would busy-loop the worker).
    watcher_interval_minutes: int = 5
    # UTC hour (0-23) at which the nightly expiration sweep runs — re-extract + backfill of NDAs
    # missing an expiration date (doubles as backfill, §3.10). Default 02:00 UTC (a quiet window).
    expiration_sweep_hour_utc: int = 2

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
        # Managed Postgres providers hand out `postgres://` (and SQLAlchemy 2.0 rejects a bare scheme
        # with no driver). Pin the psycopg2 driver so a prod URL works unmodified. SQLite/other URLs
        # pass through untouched. (Default value is not run through validators, so the sqlite:// default
        # is safe.)
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

    @field_validator("engine_rate_limit_per_min")
    @classmethod
    def _non_negative_rate(cls, v: int) -> int:
        return max(0, int(v))  # 0 == disabled; never negative

    @field_validator("engine_monthly_cost_cap_usd")
    @classmethod
    def _non_negative_cost(cls, v: float) -> float:
        return max(0.0, float(v))  # 0 == disabled; never negative

    @field_validator("auth_ip_max_attempts", "auth_reset_ip_max")
    @classmethod
    def _non_negative_ip_cap(cls, v: int) -> int:
        # 0 == disabled for that cap specifically; auth_ip_throttle_enabled is the structural switch.
        return max(0, int(v))

    @field_validator("auth_ip_window_s")
    @classmethod
    def _positive_ip_window(cls, v: int) -> int:
        return max(1, int(v))  # a zero/negative window is meaningless; clamp to >=1s

    @field_validator("imap_port", "smtp_port")
    @classmethod
    def _valid_mail_port(cls, v: int) -> int:
        # A mail port must be a usable TCP port; clamp into range so a fat-fingered 0/negative can't
        # silently disable the connection in a confusing way.
        return min(65535, max(1, int(v)))

    @field_validator("bot_inbox_sweep_seconds")
    @classmethod
    def _positive_sweep(cls, v: int) -> int:
        return max(
            1, int(v)
        )  # a zero/negative sweep interval is meaningless; clamp to >=1s

    @field_validator("watcher_interval_minutes")
    @classmethod
    def _positive_watcher_interval(cls, v: int) -> int:
        # A zero/negative poll interval would busy-loop the worker; clamp to >=1min. (The OLD n8n
        # watcher's misconfigured 1-min cadence is exactly what this default of 5 replaces — §5.)
        return max(1, int(v))

    @field_validator("expiration_sweep_hour_utc")
    @classmethod
    def _valid_utc_hour(cls, v: int) -> int:
        # Must be a real hour-of-day; clamp into 0..23 so a fat-fingered value can't silently move
        # the nightly sweep to a nonsense hour (or disable it).
        return min(23, max(0, int(v)))

    # ---- Config-group presence API (used by the capability registry) ----
    def is_configured(self, *keys: str) -> bool:
        """True iff every named settings key holds a non-empty value.

        ``keys`` are settings field names (e.g. ``"openrouter_api_key"``). This is the "is this config
        group present?" question a capability asks about its required keys.
        """
        return all(self._present(k) for k in keys)

    def missing_config(self, *keys: str) -> list[str]:
        """The ENV-var names of the given keys that are unset/empty — for capability ``reason`` text.

        Field name -> env var is a straight upper-case (no prefix, case-insensitive env matching), so
        ``openrouter_api_key`` reports as ``OPENROUTER_API_KEY``.
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
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def openrouter_provider_only_list(self) -> tuple[str, ...]:
        """``openrouter_provider_only`` parsed to provider slugs (tuple: immutable + hashable)."""
        return tuple(
            s.strip() for s in self.openrouter_provider_only.split(",") if s.strip()
        )

    @property
    def openrouter_provider_only_deep_list(self) -> tuple[str, ...]:
        """The deep-tier provider pin (``google-vertex`` by default — the only ZDR route that serves
        opus-4-8 with json_schema). Falls back to the global pin when blank."""
        pins = tuple(
            s.strip()
            for s in self.openrouter_provider_only_deep.split(",")
            if s.strip()
        )
        return pins or self.openrouter_provider_only_list

    @property
    def expiration_provider_only_list(self) -> tuple[str, ...]:
        """``expiration_provider_only`` as provider slugs — the benchmark pin ``('google-vertex',)``
        by default. Passed as ``provider.only`` (with allow_fallbacks=false) on the expiration alias.
        """
        return tuple(
            s.strip() for s in self.expiration_provider_only.split(",") if s.strip()
        )

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def uploads_path(self) -> Path:
        p = self.data_path / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def exports_path(self) -> Path:
        p = self.data_path / "exports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def templates_path(self) -> Path:
        p = self.data_path / "templates"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (cached). Tests construct ``Settings(_env_file=None)`` directly
    for isolation rather than going through this cache."""
    return Settings()


#: Module-level singleton for the ported db/alembic layer (``from app.config import settings``). Reads
#: the environment/.env once at import; tests that need isolation build ``Settings(_env_file=None)``.
settings = get_settings()
