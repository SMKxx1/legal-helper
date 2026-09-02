"""Migration 0005 (archive): the ``nda_cache_processed`` table exists at head, create_all stays
schema-equivalent for it, and 0005 is chained on 0004 + reversible.

New file (the frozen ``tests/test_migrations*`` catch-alls are never edited). Parity is asserted SCOPED
to the archive table + its status column so this test is green regardless of the parallel expiration
migration — the full-schema ``create_all == head`` invariant stays owned by ``tests/test_migrations.py``.
"""

from __future__ import annotations

ARCHIVE_TABLE = "nda_cache_processed"
ARCHIVE_COLUMNS = {
    "file_id",
    "file_name",
    "envelope_folder",
    "status",
    "renamed_to",
    "error",
    "created_at",
    "processed_at",
}


def _head_engine(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "head.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    command.upgrade(db_migrate._alembic_config(), "head")
    return create_engine(f"sqlite:///{db}")


def test_archive_table_created_at_head(tmp_path, monkeypatch) -> None:
    from sqlalchemy import inspect

    eng = _head_engine(tmp_path, monkeypatch)
    try:
        insp = inspect(eng)
        assert ARCHIVE_TABLE in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns(ARCHIVE_TABLE)}
        assert cols >= ARCHIVE_COLUMNS, f"missing: {sorted(ARCHIVE_COLUMNS - cols)}"
        # file_id is the primary key (the fail-closed dedup).
        pk = insp.get_pk_constraint(ARCHIVE_TABLE)
        assert pk["constrained_columns"] == ["file_id"]
        # The status/processed_at scan index exists.
        idx_names = {ix["name"] for ix in insp.get_indexes(ARCHIVE_TABLE)}
        assert "ix_nda_cache_processed_status_processed" in idx_names
    finally:
        eng.dispose()


def test_archive_table_matches_create_all(tmp_path, monkeypatch) -> None:
    """The ORM ``create_all`` and the migration head agree on the archive table + its columns (scoped)."""
    from sqlalchemy import create_engine, inspect

    import app.models  # noqa: F401 — registers nda_cache_processed on Base.metadata
    from app.db import Base

    eng_a = create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    Base.metadata.create_all(eng_a)
    cols_a = {c["name"] for c in inspect(eng_a).get_columns(ARCHIVE_TABLE)}
    eng_a.dispose()

    eng_b = _head_engine(tmp_path, monkeypatch)
    cols_b = {c["name"] for c in inspect(eng_b).get_columns(ARCHIVE_TABLE)}
    eng_b.dispose()

    assert cols_a == cols_b == ARCHIVE_COLUMNS


def test_0005_chained_on_0004_and_reversible(tmp_path, monkeypatch) -> None:
    """0005 downgrades cleanly back to 0004 (the archive table drops)."""
    from sqlalchemy import create_engine, inspect

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "chain.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    cfg = db_migrate._alembic_config()
    command.upgrade(cfg, "0005_archive")
    eng = create_engine(f"sqlite:///{db}")
    assert ARCHIVE_TABLE in inspect(eng).get_table_names()
    eng.dispose()

    command.downgrade(cfg, "0004_envelopes")
    eng2 = create_engine(f"sqlite:///{db}")
    assert ARCHIVE_TABLE not in inspect(eng2).get_table_names()
    eng2.dispose()
