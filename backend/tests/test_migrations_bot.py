"""Migration 0002 (bot core): the four tables exist at head, and create_all == head still holds.

The wave-1 ``tests/test_migrations.py`` is frozen; this is the NEW file the P2 foundation adds. It
asserts the 0002 bot tables are created by ``alembic upgrade head``, that the ORM ``create_all`` schema
(tests + fresh dev DBs) is still table-set-equivalent to the migration chain (the central dev/prod
parity invariant), and that 0002 is correctly chained on 0001 and reversible.
"""

from __future__ import annotations

BOT_TABLES = {
    "bot_inbox",
    "nda_allowlist",
    "nda_pending_requests",
    "bot_correlation",
}


def _head_tables(tmp_path, monkeypatch) -> set[str]:
    from sqlalchemy import create_engine, inspect

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "head.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    command.upgrade(db_migrate._alembic_config(), "head")
    eng = create_engine(f"sqlite:///{db}")
    tables = set(inspect(eng).get_table_names()) - {"alembic_version"}
    eng.dispose()
    return tables


def test_bot_tables_created_at_head(tmp_path, monkeypatch) -> None:
    tables = _head_tables(tmp_path, monkeypatch)
    missing = BOT_TABLES - tables
    assert not missing, f"0002 did not create: {sorted(missing)}"


def test_create_all_still_matches_alembic_head(tmp_path, monkeypatch) -> None:
    """Re-affirms the parity invariant now that 0002 is in the chain (bot tables on BOTH sides)."""
    from sqlalchemy import create_engine, inspect

    import app.auth.models  # noqa: F401 - register identity/org tables on Base.metadata
    import app.models  # noqa: F401 - register core + bot tables on Base.metadata
    from app.db import Base

    # Schema A: ORM create_all.
    eng_a = create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    Base.metadata.create_all(eng_a)
    tables_a = set(inspect(eng_a).get_table_names())
    eng_a.dispose()

    # Schema B: the migration chain (0001 -> 0002).
    tables_b = _head_tables(tmp_path, monkeypatch)

    assert tables_a >= BOT_TABLES, (
        "bot tables missing from create_all (models not registered?)"
    )
    assert tables_a == tables_b, (
        f"schema drift — only in create_all: {sorted(tables_a - tables_b)}; "
        f"only in migrations: {sorted(tables_b - tables_a)}"
    )


def test_0002_is_chained_on_baseline_and_reversible(tmp_path, monkeypatch) -> None:
    """Upgrading to head then downgrading to the baseline drops exactly the bot tables."""
    from sqlalchemy import create_engine, inspect

    from alembic import command
    from app import db_migrate
    from app.config import settings

    db = tmp_path / "chain.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
    cfg = db_migrate._alembic_config()

    command.upgrade(cfg, "head")
    eng = create_engine(f"sqlite:///{db}")
    at_head = set(inspect(eng).get_table_names())
    eng.dispose()
    assert at_head >= BOT_TABLES

    # 0002.down_revision == '0001_baseline' — downgrading one step reaches the baseline.
    command.downgrade(cfg, "0001_baseline")
    eng = create_engine(f"sqlite:///{db}")
    at_baseline = set(inspect(eng).get_table_names())
    eng.dispose()
    assert BOT_TABLES.isdisjoint(at_baseline), (
        f"downgrade left bot tables behind: {sorted(BOT_TABLES & at_baseline)}"
    )
    # The baseline's own tables are still present (we only rolled back 0002).
    assert "contracts" in at_baseline and "nda_idempotency_key" in at_baseline
