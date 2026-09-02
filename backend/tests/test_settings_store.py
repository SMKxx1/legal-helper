"""settings_store fail-soft contract (fault isolation).

`load_overrides` / `effective()` are read by the health page, the provider factory and the review
path. On a database where the optional `app_settings` table does not exist (fresh clone, unmigrated
CI runner), the read must DEGRADE to env defaults — never raise. Regression for the CI-only failure
where an unguarded `select(AppSetting)` 500'd /health and failed every review on a fresh checkout.
Writes are the opposite: `set_override` must stay loud so a save that can't persist is never
silently dropped.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app import settings_store


@pytest.fixture()
def tableless_sessions(monkeypatch: pytest.MonkeyPatch):
    """Point the module's SessionLocal at an EMPTY in-memory DB (no tables at all)."""
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(settings_store, "SessionLocal", factory)
    return factory


def test_load_overrides_degrades_to_env_defaults_without_the_table(
    tableless_sessions,
) -> None:
    assert settings_store.load_overrides() == {}


def test_effective_resolves_env_defaults_without_the_table(tableless_sessions) -> None:
    cfg = settings_store.effective()
    # Env defaults come through untouched — the broken overrides table is invisible to readers.
    assert cfg.ai_provider == "anthropic"  # sole provider, hardcoded in effective()
    assert cfg.anthropic_model == settings_store.settings.anthropic_model


def test_set_overrides_stays_loud_without_the_table(tableless_sessions) -> None:
    # The WRITE path must not inherit the fail-soft: a save that can't persist is an error.
    with pytest.raises(SQLAlchemyError):
        settings_store.set_overrides({"ai_provider": "anthropic"})
