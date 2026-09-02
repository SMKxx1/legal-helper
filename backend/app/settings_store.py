"""Runtime settings: env defaults overridden by user choices stored in SQLite.

`config.Settings` holds the boot-time defaults from the environment. This module
layers user edits (made from the Settings page) on top, so the active provider,
model, output format, and Anthropic API key can change at runtime without a
restart. The provider factory and review service read `effective()` rather than
the static `settings` singleton.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .config import settings
from .db import SessionLocal
from .models import AppSetting

_log = logging.getLogger("nda.settings")

# Keys that may be overridden from the UI (everything else stays env-driven).
OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        "ai_provider",
        "anthropic_api_key",
        "anthropic_model",
        "pdf_extract_strategy",
        # Dashboard-managed bot admin routing (approval notices) — non-secret, plaintext.
        "nda_admin_slack_channel",
        "nda_admin_email",
    }
)

# Override keys whose values are secrets and are encrypted at rest when a
# SETTINGS_ENCRYPTION_KEY is configured (§1.3). Decryption happens inside
# load_overrides, so the rest of the app still sees plaintext.
_ENCRYPTED_KEYS: frozenset[str] = frozenset({"anthropic_api_key"})
_ENC_PREFIX = "enc:v1:"


def _cipher():
    """Return a Fernet cipher if an encryption key is configured, else None."""
    key = settings.settings_encryption_key
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode())


def _encrypt(value: str) -> str:
    cipher = _cipher()
    if cipher is None or not value:
        return value  # no key configured (or empty) -> store as-is (back-compat)
    return _ENC_PREFIX + cipher.encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    if not value.startswith(_ENC_PREFIX):
        return value  # legacy plaintext row -> read as-is
    cipher = _cipher()
    if cipher is None:
        # Key was removed/never set but an encrypted row exists — the secret is unreadable. Warn so an
        # operator notices the key mismatch instead of silently discovering downstream provider-auth
        # failures (the secret reads as unset and the provider falls back).
        _log.warning(
            "encrypted app_setting present but SETTINGS_ENCRYPTION_KEY is unset; "
            "treating the stored secret as empty (rotate/restore the key to recover it)"
        )
        return ""
    from cryptography.fernet import InvalidToken

    try:
        return cipher.decrypt(value[len(_ENC_PREFIX) :].encode()).decode()
    except InvalidToken:
        _log.warning(
            "failed to decrypt an app_setting with the current SETTINGS_ENCRYPTION_KEY "
            "(key rotated/mismatched?); treating the stored secret as empty"
        )
        return ""


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    """Resolved, read-only configuration used by the AI layer."""

    ai_provider: str
    anthropic_api_key: str
    anthropic_model: str
    anthropic_max_tokens: int
    pdf_extract_strategy: str

    @property
    def signature(self) -> tuple:
        """Identity for provider caching (api keys reduced to presence flags)."""
        return (
            self.ai_provider,
            self.anthropic_model,
            bool(self.anthropic_api_key),
        )


@contextmanager
def _session(db=None):
    if db is not None:
        yield db
        return
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def load_overrides(db=None) -> dict[str, str]:
    """Read the stored overrides, FAIL-SOFT: an unavailable/unmigrated settings table degrades to
    the env defaults (empty overrides) with a warning instead of taking down every reader — the
    health page, the provider factory, and the review path all resolve config through here, and a
    broken optional overrides table must not break them (writes in ``set_override`` stay strict, so
    a save that can't persist still errors loudly)."""
    try:
        with _session(db) as s:
            rows = s.scalars(select(AppSetting)).all()
            return {
                r.key: (_decrypt(r.value) if r.key in _ENCRYPTED_KEYS else r.value)
                for r in rows
                if r.key in OVERRIDE_KEYS
            }
    except SQLAlchemyError as exc:
        _log.warning(
            "app_settings unavailable (%s); using env defaults for runtime overrides",
            type(exc).__name__,
        )
        return {}


def effective(db=None) -> EffectiveConfig:
    """Merge env defaults with stored overrides into a resolved config."""
    ov = load_overrides(db)

    def pick(key: str, default: str) -> str:
        # An override wins when the row exists (empty string clears a value).
        return ov.get(key, default)

    # Anthropic is the sole provider; an override/env value is ignored.
    pdf_strategy = (
        pick("pdf_extract_strategy", settings.pdf_extract_strategy).strip().lower()
    )
    if pdf_strategy not in {"local"}:
        pdf_strategy = "local"

    return EffectiveConfig(
        ai_provider="anthropic",
        anthropic_api_key=pick("anthropic_api_key", settings.anthropic_api_key),
        anthropic_model=pick("anthropic_model", settings.anthropic_model).strip()
        or settings.anthropic_model,
        anthropic_max_tokens=settings.anthropic_max_tokens,
        pdf_extract_strategy=pdf_strategy,
    )


def admin_routing(db=None, settings_obj=None) -> tuple[str, str]:
    """Resolve the bot's admin routing as ``(admin_slack_channel, admin_email)``.

    Dashboard override (``app_settings``) wins when present and non-empty; otherwise ``settings_obj``
    (the injected env ``Settings`` — falls back to the process singleton) provides
    ``NDA_ADMIN_SLACK_CHANNEL`` / ``NDA_ADMIN_EMAIL``. This is the single resolver every reader (approvals
    notify, approve-authz, template-admin authz, archive watcher) calls so the value is manageable from
    the dashboard instead of only via env keys. Fail-soft: an override-read error falls back to env.
    """
    env = settings_obj or settings
    try:
        ov = load_overrides(db)
    except Exception:  # noqa: BLE001 — an override-read failure must not break admin routing
        ov = {}
    channel = (
        ov.get("nda_admin_slack_channel") or env.nda_admin_slack_channel or ""
    ).strip()
    email = (ov.get("nda_admin_email") or env.nda_admin_email or "").strip()
    return channel, email


def set_overrides(updates: dict[str, str | None], db=None) -> EffectiveConfig:
    """Upsert override rows for the provided keys; return the new effective config.

    Only keys in OVERRIDE_KEYS are persisted; `None` values are ignored (no
    change). Empty strings ARE stored (explicit clear). Triggers a provider
    rebuild on next use via the changed signature.
    """
    from .models import _now  # local import to avoid surfacing a private helper

    with _session(db) as s:
        for key, value in updates.items():
            if key not in OVERRIDE_KEYS or value is None:
                continue
            stored = _encrypt(value) if key in _ENCRYPTED_KEYS else value
            row = s.get(AppSetting, key)
            if row is None:
                s.add(AppSetting(key=key, value=stored, updated_at=_now()))
            else:
                row.value = stored
                row.updated_at = _now()
        s.commit()
        return effective(s)


def clear_override(key: str, db=None) -> None:
    with _session(db) as s:
        row = s.get(AppSetting, key)
        if row is not None:
            s.delete(row)
            s.commit()
