"""Schema-parity invariant: ORM ``create_all`` == ``alembic upgrade head``.

The app boots with ``alembic upgrade head`` (prod) but tests + a fresh dev DB use ``create_all``. If a
model is added without a migration (or vice versa) the two drift, and the central dev/prod-parity
assumption breaks. This builds the schema both ways into throwaway SQLite DBs and asserts the same
table set. Ported verbatim in intent from the source engine's CI gate (nda-review-cloud).
"""

from __future__ import annotations


def test_create_all_matches_alembic_head(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect

    import app.auth.models  # noqa: F401 - register identity/org tables on Base.metadata
    import app.models  # noqa: F401 - register core engine tables on Base.metadata
    from alembic import command
    from app import db_migrate
    from app.config import settings
    from app.db import Base

    # Schema A: built from the ORM models (the create_all path tests + fresh dev DBs use).
    eng_a = create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    Base.metadata.create_all(eng_a)
    tables_a = set(inspect(eng_a).get_table_names())
    eng_a.dispose()

    # Schema B: built from the migration chain (the prod boot path), pointed at a throwaway DB.
    db_b = tmp_path / "alembic.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_b}")
    command.upgrade(db_migrate._alembic_config(), "head")
    eng_b = create_engine(f"sqlite:///{db_b}")
    # alembic_version is alembic's own bookkeeping; views are excluded from get_table_names().
    tables_b = set(inspect(eng_b).get_table_names()) - {"alembic_version"}
    eng_b.dispose()

    assert tables_a == tables_b, (
        f"schema drift — only in create_all: {sorted(tables_a - tables_b)}; "
        f"only in migrations: {sorted(tables_b - tables_a)}"
    )


def test_retired_tables_absent_from_baseline(tmp_path, monkeypatch):
    """The dropped SIGNED-plane + n8n-doorway tables must never reappear in the schema."""
    from sqlalchemy import create_engine, inspect

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "head.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    command.upgrade(db_migrate._alembic_config(), "head")
    eng = create_engine(f"sqlite:///{db}")
    tables = set(inspect(eng).get_table_names())
    eng.dispose()

    retired = {
        "principal_nonces",
        "allowed_accounts",
        "nda_bot_request",
        "nda_bot_event",
        "nda_bot_envelope",
    }
    assert retired.isdisjoint(tables), (
        f"retired tables present: {sorted(retired & tables)}"
    )
    # The kept idempotency table (generate-nda replay) must be there.
    assert "nda_idempotency_key" in tables
