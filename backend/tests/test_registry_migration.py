"""Migration 0006 (token registry): the ``token_registry_meta`` table exists at head, create_all stays
schema-equivalent for it, and 0006 is chained on 0005 + reversible.

New file (the frozen ``tests/test_migrations*`` are never edited). Parity is asserted SCOPED to the new
table so this test is independent of any sibling P5-wave-A migrations — the full-schema
``create_all == head`` invariant stays owned by ``tests/test_migrations.py``.
"""

from __future__ import annotations

META_TABLE = "token_registry_meta"


def _head_engine(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "head.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    command.upgrade(db_migrate._alembic_config(), "head")
    return create_engine(f"sqlite:///{db}")


def test_meta_table_created_at_head(tmp_path, monkeypatch) -> None:
    from sqlalchemy import inspect

    eng = _head_engine(tmp_path, monkeypatch)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert META_TABLE in tables


def test_create_all_matches_head_for_the_meta_table(tmp_path, monkeypatch) -> None:
    from sqlalchemy import create_engine, inspect

    import app.auth.models  # noqa: F401 - register identity/org tables on Base.metadata
    import app.models  # noqa: F401 - register core + bot + forms + registry tables on Base.metadata
    from app.db import Base

    eng_a = create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    Base.metadata.create_all(eng_a)
    cols_a = {c["name"] for c in inspect(eng_a).get_columns(META_TABLE)}
    eng_a.dispose()

    eng_b = _head_engine(tmp_path, monkeypatch)
    cols_b = {c["name"] for c in inspect(eng_b).get_columns(META_TABLE)}
    eng_b.dispose()

    assert cols_a == cols_b, (
        f"{META_TABLE} column drift — only in create_all: {sorted(cols_a - cols_b)}; "
        f"only in migrations: {sorted(cols_b - cols_a)}"
    )


def test_0006_is_chained_on_0005_and_reversible(tmp_path, monkeypatch) -> None:
    from sqlalchemy import create_engine, inspect

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "chain.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    cfg = db_migrate._alembic_config()

    command.upgrade(cfg, "0006_token_registry")
    eng = create_engine(f"sqlite:///{db}")
    at_head = set(inspect(eng).get_table_names())
    eng.dispose()
    assert META_TABLE in at_head
    # The prior chain (token table from the baseline) is present — the FK target exists.
    assert "token" in at_head

    command.downgrade(cfg, "0005_archive")
    eng = create_engine(f"sqlite:///{db}")
    at_0005 = set(inspect(eng).get_table_names())
    eng.dispose()
    assert META_TABLE not in at_0005
    # Only 0006 rolled back — the archive table (and token) remain.
    assert "nda_cache_processed" in at_0005 and "token" in at_0005
